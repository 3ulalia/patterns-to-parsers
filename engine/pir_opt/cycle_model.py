"""Flit-by-flit executable models of the optimized plan and of the baseline.

``run_plan``  executes ``plan.HwPlan``; ``run_baseline`` executes
``baseline.BaselinePlan``. Both consume a packet as a sequence of flits (the
packet is truncated to ``max_width/8`` bytes; each flit carries ``nvalid`` valid
bytes like ``tkeep``; only the last flit may be partial) and return a
``RunResult`` with ``path_id`` and an optional per-cycle trace.

Plan model, per cycle ``t``:
  1. Evaluate every comparator UNIT whose schedule contains ``t`` -- at most
     once (``_Evaluator`` raises ``ModelError`` on a second evaluation; the
     per-cycle counter is the proof that comparator reuse is real). Output is
     gated by byte-valid.
  2. Evaluate every AND consumer UNIT whose schedule (enable) contains ``t``,
     reading d=0 inputs from this cycle's values and d>=1 inputs from carry
     registers written in earlier cycles. Reading a value that was not
     scheduled (``KeyError``-like) or a register never written is a
     ``ModelError`` -> proves schedules are complete.
  3. Update sticky bits / alive bits (cleared / set at SOP, i.e. ``t == 0``).
  4. Shift the carry registers.
At EOP the priority chain reads the post-update state of the last flit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .baseline import BaselinePlan
from .pir import PirConfig
from .plan import HwPlan


class ModelError(AssertionError):
    """The plan violated a model invariant (double evaluation, unscheduled read...)."""


@dataclass
class Flit:
    t: int
    data: bytes   # B bytes, zero padded
    nvalid: int   # number of valid bytes (lane < nvalid)


def packet_to_flits(packet: bytes, cfg: PirConfig) -> List[Flit]:
    """Cut the (window-truncated) packet into flits. Empty packet -> no flits."""
    window = bytes(packet[: cfg.max_bytes])
    B = cfg.bytes_per_flit
    out = []
    for t in range(0, -(-len(window) // B)):
        chunk = window[t * B:(t + 1) * B]
        out.append(Flit(t, chunk + bytes(B - len(chunk)), len(chunk)))
    return out


@dataclass
class CycleTrace:
    t: int
    data: bytes
    nvalid: int
    cmp_evals: List[str] = field(default_factory=list)      # comparators evaluated
    cmp_true: List[str] = field(default_factory=list)       # ... that were true
    consumers: List[Tuple[str, bool]] = field(default_factory=list)  # enabled AND units, value
    actions: List[str] = field(default_factory=list)        # branch / state-update descriptions
    state: Dict[str, int] = field(default_factory=dict)     # registers after the cycle


@dataclass
class RunResult:
    path_id: int
    trace: List[CycleTrace]
    evals_per_cycle: List[int]       # unit evaluations (cmp + and/latch) per cycle
    max_evals_per_unit: int          # must be <= 1
    total_evals: int
    eop_actions: List[str] = field(default_factory=list)


class _Evaluator:
    """Per-cycle evaluation bookkeeping: each unit may be evaluated at most once."""

    def __init__(self) -> None:
        self.count: Dict[str, int] = {}
        self.values: Dict[str, bool] = {}

    def evaluate(self, uid: str, value: bool) -> bool:
        self.count[uid] = self.count.get(uid, 0) + 1
        if self.count[uid] > 1:
            raise ModelError(f"unit {uid} evaluated more than once in one cycle")
        self.values[uid] = value
        return value

    def read(self, uid: str) -> bool:
        if uid not in self.values:
            raise ModelError(f"read of {uid} in a cycle where it is not scheduled")
        return self.values[uid]


def _cmp_value(lane: int, mask: int, value: int, flit: Flit) -> bool:
    return lane < flit.nvalid and (flit.data[lane] & mask) == value


# ------------------------------------------------------------ plan model
def run_plan(plan: HwPlan, packet: bytes, trace: bool = True) -> RunResult:
    cfg = plan.config
    flits = packet_to_flits(packet, cfg)
    found = {s: 0 for s in plan.stickies}
    alive = {a: 1 for a in plan.alive}
    regs: Dict[str, Optional[bool]] = {r: None for r in plan.delay_regs}
    traces: List[CycleTrace] = []
    evals_pc: List[int] = []
    max_unit = 0
    total = 0

    for flit in flits:
        t = flit.t
        ev = _Evaluator()
        tr = CycleTrace(t, flit.data, flit.nvalid)
        if t == 0:  # SOP: clear sticky, set alive
            found = {s: 0 for s in plan.stickies}
            alive = {a: 1 for a in plan.alive}
        # 1. comparators
        for u in plan.cmp_units.values():
            if t in u.schedule:
                v = ev.evaluate(u.id, _cmp_value(u.lane, u.mask, u.value, flit))
                tr.cmp_evals.append(u.id)
                if v:
                    tr.cmp_true.append(u.id)
        # 2. AND consumers (enable = t in schedule)
        for u in plan.and_units.values():
            if t not in u.schedule:
                continue
            ok = True
            for ref in u.inputs:
                if ref.delay == 0:
                    ok &= ev.read(ref.unit)
                else:
                    rv = regs[f"{ref.unit}_d{ref.delay}"]
                    if rv is None:
                        raise ModelError(f"{u.id}@t={t}: carry {ref.unit}_d{ref.delay} never written")
                    ok &= rv
            ev.evaluate(u.id, ok)
            tr.consumers.append((u.id, ok))
        # 3. state updates
        for s in plan.stickies.values():
            if t in s.schedule:
                fired = [c for c in s.consumers if t in plan.and_units[c].schedule and ev.read(c)]
                new = int(bool(found[s.id]) or bool(fired))
                tr.actions.append(f"{s.id}: {found[s.id]} | OR({', '.join(fired) or '-'}) -> {new}")
                found[s.id] = new
        for a in plan.alive.values():
            srcs = a.terms.get(t)
            if srcs is None:
                continue
            vals = [ev.read(x) for x in srcs]
            new = int(bool(alive[a.id]) and all(vals))
            tr.actions.append(f"{a.id}: if ({' && '.join(srcs)})={int(all(vals))} "
                              f"-> alive={new}" + ("" if new == alive[a.id] else "  (cleared)"))
            alive[a.id] = new
        # 4. carry registers shift (write at end of cycle, read in later cycles)
        new_regs = {}
        for r in plan.delay_regs.values():
            if r.k == 1:
                new_regs[r.id] = ev.values.get(r.src)
            else:
                new_regs[r.id] = regs[f"{r.src}_d{r.k - 1}"]
        regs = new_regs
        n_ev = sum(ev.count.values())
        evals_pc.append(n_ev)
        total += n_ev
        max_unit = max([max_unit] + list(ev.count.values()))
        if trace:
            tr.state = {**{k: v for k, v in alive.items()}, **{k: v for k, v in found.items()},
                        **{k: int(bool(v)) for k, v in regs.items() if v is not None}}
            traces.append(tr)

    last_t = flits[-1].t if flits else -1
    path_id, eop = _plan_output(plan, alive, found, last_t)
    return RunResult(path_id, traces, evals_pc, max_unit, total, eop)


def _plan_output(plan: HwPlan, alive, found, last_t: int) -> Tuple[int, List[str]]:
    lines: List[str] = []
    result = 0
    for o in plan.output:
        if o.kind == "always":
            hit, why = True, "always"
        elif o.kind == "family":
            hit, why = bool(found[o.ref]), f"{o.ref}={found[o.ref]}"
        else:
            hit = bool(alive[o.ref]) and last_t >= o.reach
            why = f"{o.ref}={alive[o.ref]}" + (f" && eop_t({last_t})>={o.reach}" if o.reach else "")
        lines.append(f"{'TAKEN ' if hit else 'no    '}{why} -> {o.id_width}'h{o.path_id:x}")
        if hit:
            result = o.path_id
            lines.append("  (lower priority terms skipped)")
            break
    return result, lines


# --------------------------------------------------------- baseline model
def run_baseline(bp: BaselinePlan, packet: bytes, trace: bool = True) -> RunResult:
    cfg = bp.config
    flits = packet_to_flits(packet, cfg)
    latch = {l.id: 0 for l in bp.latches}
    traces: List[CycleTrace] = []
    evals_pc: List[int] = []
    max_unit = 0
    total = 0
    for flit in flits:
        t = flit.t
        ev = _Evaluator()
        tr = CycleTrace(t, flit.data, flit.nvalid)
        if t == 0:
            latch = {l.id: 0 for l in bp.latches}
        for c in bp.cmps_at(t):
            v = ev.evaluate(c.id, _cmp_value(c.lane, c.mask, c.value, flit))
            tr.cmp_evals.append(c.id)
            if v:
                tr.cmp_true.append(c.id)
        for l in bp.latches_at(t):
            v = all(ev.read(i) for i in l.inputs)
            ev.evaluate(l.id, v)
            latch[l.id] = int(v)
            tr.consumers.append((l.id, v))
            tr.actions.append(f"{l.id} <= {' && '.join(l.inputs)} = {int(v)}")
        n_ev = sum(ev.count.values())
        evals_pc.append(n_ev)
        total += n_ev
        max_unit = max([max_unit] + list(ev.count.values()))
        if trace:
            tr.state = dict(latch)
            traces.append(tr)
    result = 0
    lines: List[str] = []
    for r in bp.rows:
        hit = all(latch[l] for l in r.latches)
        lines.append(f"{'TAKEN ' if hit else 'no    '}row r{r.row_index}: "
                     f"{' && '.join(r.latches) or 'always'} -> {r.id_width}'h{r.path_id:x}")
        if hit:
            result = r.path_id
            break
    return RunResult(result, traces, evals_pc, max_unit, total, lines)
