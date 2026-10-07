"""Sequential BLIF export of the baseline model and of the optimized plan.

Interface (identical for both netlists, so they can be compared / mitered):

  inputs   d<lane>_<bit>   data bit ``bit`` (0 = LSB) of byte ``lane`` of the flit (8*B)
           v<lane>         byte-valid of lane ``lane`` (tkeep)
           sop, eop        start / end of packet (flit marker)
  outputs  path_id_<j>     combinational EOP priority result (sample when eop = 1);
                           it is NOT gated by eop (eop is a declared but unused input)
  one flit per clock; every clock between sop and eop carries a flit.

Flit counter (``counter=`` option):
  "binary"  cnt register of W = bit_length(T) bits holding min(#flits since SOP, T)
            (value after processing flit t is t+1, saturating at T so over-long packets
            behave like the Python model, which truncates to the window).
            t_cur = sop ? 0 : cnt.   is_t[0] = sop,  is_t[k>0] = !sop & cnt == k.
  "onehot"  oh_1..oh_T, oh_k <=> cnt == k (T FFs, no decode).
Reset: all latches init 0. Every state element is (re)initialised by ``sop`` inside its
next-state function, so no reset is needed beyond the power-up value.

Faithfulness to ``cycle_model``: the EOP stage reads post-update (next-state) values of
the last flit; a comparator is ``valid[lane] & (byte & mask) == value``; baseline latches
``m_r<row>_t<t> <= is_t[t] ? AND(atoms) : (sop ? 0 : m)``; plan alive bits
``alive <= (sop | alive) & AND_t(!is_t[t] | term_t)``; sticky
``found <= (!sop & found) | OR_c(en_c & and_c)``; carry regs shift every cycle.
"""
from __future__ import annotations

from typing import Dict, List

from ..baseline import BaselinePlan
from ..plan import HwPlan
from ..pir import PirConfig
from .netlist import Netlist


def _frame(nl: Netlist, cfg: PirConfig, counter: str):
    """Declare PIs, build the flit counter. Returns (is_t, ge) where ge(reach) is a net."""
    B, T = cfg.bytes_per_flit, cfg.num_flits
    for l in range(B):
        for b in range(8):
            nl.add_input(f"d{l}_{b}")
    for l in range(B):
        nl.add_input(f"v{l}")
    sop = nl.add_input("sop")
    nl.add_input("eop")
    is_t: List[str] = [sop]
    ge_cache: Dict[int, str] = {}
    if counter == "binary":
        W = max(1, T.bit_length())
        cnt = [f"cnt_{i}" for i in range(W)]
        eq_cache: Dict[int, str] = {}

        def eq(v: int) -> str:
            if v not in eq_cache:
                eq_cache[v] = nl.cube(cnt, "".join("1" if (v >> i) & 1 else "0" for i in range(W)))
            return eq_cache[v]

        for i in range(W):
            terms = [eq(v) for v in range(1, T + 1) if (min(v + 1, T) >> i) & 1]
            x = nl.OR(terms)
            d = nl.node([sop, x], ["1-", "-1"]) if i == 0 else nl.node([sop, x], ["01"])
            nl.latch(d, cnt[i], 0)
        for k in range(1, T):
            is_t.append(nl.node([sop] + cnt, ["0" + "".join("1" if (k >> i) & 1 else "0" for i in range(W))]))

        def ge(reach: int) -> str:
            if reach not in ge_cache:
                x = nl.OR([eq(v) for v in range(reach, T + 1)])
                ge_cache[reach] = nl.node([sop, x], ["01"])
            return ge_cache[reach]
        nl.meta["counter_latches"] = list(cnt)
    elif counter == "onehot":
        oh = {k: f"oh_{k}" for k in range(1, T + 1)}
        for k in range(1, T + 1):
            if T == 1:
                d = nl.node([sop, oh[1]], ["1-", "-1"])
            elif k == 1:
                d = sop
            elif k < T:
                d = nl.node([oh[k - 1], sop], ["10"])
            else:
                d = nl.node([oh[T - 1], oh[T], sop], ["1-0", "-10"])
            nl.latch(d, oh[k], 0)
        for k in range(1, T):
            is_t.append(nl.node([sop, oh[k]], ["01"]))

        def ge(reach: int) -> str:
            if reach not in ge_cache:
                x = nl.OR([oh[k] for k in range(reach, T + 1)])
                ge_cache[reach] = nl.node([sop, x], ["01"])
            return ge_cache[reach]
        nl.meta["counter_latches"] = list(oh.values())
    else:
        raise ValueError(counter)
    nl.meta["is_t"] = list(is_t)
    return is_t, ge


def _cmp(nl: Netlist, lane: int, mask: int, value: int, out: str) -> str:
    ins = [f"v{lane}"]
    pat = "1"
    for b in range(8):
        if (mask >> b) & 1:
            ins.append(f"d{lane}_{b}")
            pat += "1" if (value >> b) & 1 else "0"
    return nl.cube(ins, pat, out)


def _priority_output(nl: Netlist, hits: List[str], ids: List[int], width: int) -> None:
    """First hit wins. ``hits`` in priority order, no match -> 0."""
    sel: List[str] = []
    taken = None
    for h in hits:
        if taken is None:
            sel.append(h)
            taken = h
        else:
            sel.append(nl.node([h, taken], ["10"]))
            taken = nl.node([h, taken], ["1-", "-1"])
    for j in range(width):
        nl.add_output(nl.OR([s for s, i in zip(sel, ids) if (i >> j) & 1]), f"path_id_{j}")


def build_baseline_netlist(bp: BaselinePlan, counter: str = "binary", frame_only: bool = False) -> Netlist:
    cfg = bp.config
    nl = Netlist("baseline")
    is_t, ge = _frame(nl, cfg, counter)
    if frame_only:
        for k, n in enumerate(is_t):
            nl.add_output(n, f"is_t_{k}")
        return nl
    sop = "sop"
    cmp_net = {c.id: _cmp(nl, c.lane, c.mask, c.value, c.id) for c in bp.comparators}
    latch_d: Dict[str, str] = {}
    for l in bp.latches:
        v = nl.AND([cmp_net[i] for i in l.inputs])
        keep = nl.node([sop, l.id], ["01"])
        latch_d[l.id] = nl.node([is_t[l.t], v, keep], ["11-", "0-1"], f"{l.id}__d")
        nl.latch(latch_d[l.id], l.id, 0)
    hits, ids = [], []
    for r in bp.rows:
        hits.append(nl.AND([latch_d[x] for x in r.latches]))
        ids.append(r.path_id)
    width = max((r.id_width for r in bp.rows), default=1)
    _priority_output(nl, hits, ids, width)
    nl.meta["state_latches"] = [l.id for l in bp.latches]
    return nl


def build_plan_netlist(plan: HwPlan, counter: str = "binary", frame_only: bool = False) -> Netlist:
    cfg = plan.config
    nl = Netlist("plan")
    is_t, ge = _frame(nl, cfg, counter)
    if frame_only:
        for k, n in enumerate(is_t):
            nl.add_output(n, f"is_t_{k}")
        return nl
    sop = "sop"
    unit: Dict[str, str] = {}
    for u in plan.cmp_units.values():
        unit[u.id] = _cmp(nl, u.lane, u.mask, u.value, u.id)
    reg_q: Dict[str, str] = {}
    for r in sorted(plan.delay_regs.values(), key=lambda r: (r.src, r.k)):
        d = unit[r.src] if r.k == 1 else reg_q[f"{r.src}_d{r.k - 1}"]
        reg_q[r.id] = nl.latch(d, r.id, 0)
    for u in plan.and_units.values():
        ins = [unit[x.unit] if x.delay == 0 else reg_q[f"{x.unit}_d{x.delay}"] for x in u.inputs]
        unit[u.id] = nl.AND(ins, u.id)
    state: List[str] = list(reg_q)
    next_net: Dict[str, str] = {}
    for s in plan.stickies.values():
        fired = []
        for c in s.consumers:
            en = nl.OR([is_t[t] for t in plan.and_units[c].schedule])
            fired.append(nl.node([en, unit[c]], ["11"]))
        base = nl.node([sop, s.id], ["01"])
        next_net[s.id] = nl.OR([base] + fired, f"{s.id}__d")
        nl.latch(next_net[s.id], s.id, 0)
        state.append(s.id)
    for a in plan.alive.values():
        base = nl.node([sop, a.id], ["1-", "-1"])
        conds = [base]
        for t in sorted(a.terms):
            term = nl.AND([unit[x] for x in a.terms[t]])
            conds.append(nl.node([is_t[t], term], ["0-", "-1"]))
        next_net[a.id] = nl.AND(conds, f"{a.id}__d")
        nl.latch(next_net[a.id], a.id, 0)
        state.append(a.id)
    hits, ids = [], []
    for o in plan.output:
        if o.kind == "always":
            hits.append(nl.const(1))
        elif o.kind == "family":
            hits.append(next_net[o.ref])
        else:
            hits.append(nl.AND([next_net[o.ref], ge(o.reach)]) if o.reach > 0 else next_net[o.ref])
        ids.append(o.path_id)
    width = max((o.id_width for o in plan.output), default=1)
    _priority_output(nl, hits, ids, width)
    nl.meta["state_latches"] = state
    return nl


# ----------------------------------------------------------------- validation
def packets_to_cycles(packets, cfg: PirConfig):
    """Bit-parallel stimulus: lane i = packet i. Returns (cycles, eop_cycle_per_packet)."""
    B = cfg.bytes_per_flit
    nfl = [-(-len(p) // B) for p in packets]
    ncyc = max(nfl + [1])
    cycles = []
    for c in range(ncyc):
        inp: Dict[str, int] = {"sop": 0, "eop": 0}
        for i, p in enumerate(packets):
            if c >= nfl[i]:
                continue
            bit = 1 << i
            if c == 0:
                inp["sop"] |= bit
            if c == nfl[i] - 1:
                inp["eop"] |= bit
            for l in range(B):
                k = c * B + l
                if k < len(p):
                    inp[f"v{l}"] = inp.get(f"v{l}", 0) | bit
                    for b in range(8):
                        if (p[k] >> b) & 1:
                            inp[f"d{l}_{b}"] = inp.get(f"d{l}_{b}", 0) | bit
        cycles.append(inp)
    return cycles, [n - 1 for n in nfl]


def simulate_packets(nl: Netlist, packets, cfg: PirConfig):
    """Export ``nl`` to BLIF text, re-parse it, simulate; returns path_id per packet (None for empty)."""
    from .blif_sim import BlifModel
    model = BlifModel(nl.to_blif())
    cycles, eop_c = packets_to_cycles(packets, cfg)
    outs = model.simulate(cycles, len(packets))
    pj = sorted((o for o in model.outputs if o.startswith("path_id_")), key=lambda s: int(s.rsplit("_", 1)[1]))
    res = []
    for i, c in enumerate(eop_c):
        if c < 0:
            res.append(None)
            continue
        res.append(sum(((outs[c][o] >> i) & 1) << j for j, o in enumerate(pj)))
    return res


def validate_export(rows, cfg, ap, plan, bp, packets, counters=("binary", "onehot")) -> Dict[str, object]:
    """BLIF simulation vs cycle models vs golden on ``packets`` (empty packets skipped)."""
    from ..cycle_model import run_baseline, run_plan
    from ..golden import eval_pir
    pk = [p for p in packets if len(p)]
    gold = [eval_pir(rows, cfg, p) for p in pk]
    pyp = [run_plan(plan, p, trace=False).path_id for p in pk]
    pyb = [run_baseline(bp, p, trace=False).path_id for p in pk]
    res: Dict[str, object] = {"packets": len(pk), "python_plan_vs_golden": sum(a != b for a, b in zip(gold, pyp)),
                              "python_baseline_vs_golden": sum(a != b for a, b in zip(gold, pyb))}
    for ctr in counters:
        for name, nl, ref in (("baseline", build_baseline_netlist(bp, ctr), pyb),
                              ("plan", build_plan_netlist(plan, ctr), pyp)):
            sim = simulate_packets(nl, pk, cfg)
            res[f"{name}/{ctr}_mismatch"] = sum(a != b for a, b in zip(sim, ref))
    return res
