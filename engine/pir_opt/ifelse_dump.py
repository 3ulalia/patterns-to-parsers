"""Readable pseudo-SystemVerilog dumps of the per-cycle if/else flow.

* ``format_static_plan`` / ``format_static_baseline``: shared units declared
  once, then a block per cycle ``t`` listing exactly the logic active in that
  cycle, then the EOP priority if/else. Identical consecutive cycles are
  grouped (``t=4..5``) unless ``expand=True``.
* ``format_summary``: baseline vs optimized structural table.
* ``format_dynamic``: per-cycle trace for one packet (plan, baseline, golden).

Notation: ``valid[l]`` = lane l holds a valid byte (tkeep), ``byte[l]`` = lane l
data, ``flit_idx`` = flit counter t, ``eop_t`` = flit index of the EOP flit.
Pseudo-code only; it is not meant to be synthesizable as written.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from .atoms import compress_ranges
from .baseline import BaselinePlan
from .cycle_model import RunResult, run_baseline, run_plan
from .golden import eval_pir
from .plan import HwPlan, Ref, describe_schedule


def sv_cmp(lane: int, mask: int, value: int) -> str:
    if mask == 0xFF:
        return f"valid[{lane}] && (byte[{lane}] == 8'h{value:02x})"
    return f"valid[{lane}] && ((byte[{lane}] & 8'h{mask:02x}) == 8'h{value:02x})"


def _ref_name(r: Ref) -> str:
    return r.unit if r.delay == 0 else f"{r.unit}_d{r.delay}"


def _group(cycles: Sequence[Tuple[int, tuple]], expand: bool) -> List[Tuple[int, int, tuple]]:
    """Merge consecutive cycles with identical content -> [(t_first, t_last, content)]."""
    out: List[Tuple[int, int, tuple]] = []
    for t, content in cycles:
        if not expand and out and out[-1][2] == content and out[-1][1] == t - 1:
            out[-1] = (out[-1][0], t, content)
        else:
            out.append((t, t, content))
    return out


def _rows_label(idx: Sequence[int]) -> str:
    """``[3,4,5,9]`` -> ``r3..r5, r9``."""
    vals = sorted(idx)
    parts, i = [], 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[j] + 1:
            j += 1
        parts.append(f"r{vals[i]}" if i == j else f"r{vals[i]}..r{vals[j]}")
        i = j + 1
    return ", ".join(parts)


def _tlabel(a: int, b: int) -> str:
    return f"t={a}" if a == b else f"t={a}..{b}"


# ------------------------------------------------------------ static: plan
def _inline(plan: HwPlan, src: str) -> str:
    """Expand an AND-consumer source into its comparator terms (readability)."""
    if src in plan.and_units:
        return " && ".join(_ref_name(r) for r in plan.and_units[src].inputs)
    return src


def _plan_cycle(plan: HwPlan, t: int) -> tuple:
    stmts: List[str] = []
    ncmp = sum(1 for u in plan.cmp_units.values() if t in u.schedule)
    nand = sum(1 for u in plan.and_units.values() if t in u.schedule)
    stmts.append(f"// units evaluated: {ncmp} cmp + {nand} and")
    sop_only = []
    for a in plan.alive.values():
        srcs = a.terms.get(t)
        if srcs is None:
            if t == 0:
                sop_only.append(a.id)
            continue
        cond = " && ".join(_inline(plan, s) for s in srcs)
        base = "1'b1" if t == 0 else a.id
        stmts.append(f"if ({cond}) {a.id} <= {base}; else {a.id} <= 1'b0;")
    if sop_only:
        stmts.append(f"{', '.join(sop_only)} <= 1'b1;   // SOP: no atoms in flit 0")
    for s in plan.stickies.values():
        if t in s.schedule:
            terms = []
            for c in s.consumers:
                if t in plan.and_units[c].schedule:
                    ins = [_ref_name(r) for r in plan.and_units[c].inputs]
                    terms.append(ins[0] if len(ins) == 1 else "(" + " && ".join(ins) + ")")
            base = "" if t == 0 else f"{s.id} | "
            stmts.append(f"{s.id} <= {base}{' | '.join(terms)};")
        elif t == 0:
            stmts.append(f"{s.id} <= 1'b0;   // SOP clear")
    for r in plan.delay_regs.values():
        if t in r.capture_cycles:
            prev = r.src if r.k == 1 else f"{r.src}_d{r.k - 1}"
            stmts.append(f"{r.id} <= {prev};   // carry")
    return tuple(stmts)


def format_static_plan(plan: HwPlan, expand: bool = False) -> str:
    cfg = plan.config
    L: List[str] = []
    L.append(f"// ===== OPTIMIZED plan: {cfg.bytes_per_flit} B flits, T={cfg.num_flits} flits =====")
    L.append("// comparator units: one instance per (lane, mask, value), whatever t.  sched = flits where consumed")
    for u in plan.cmp_units.values():
        L.append(f"wire {u.id} = {sv_cmp(u.lane, u.mask, u.value)};"
                 f"   // sched {describe_schedule(u.schedule)}")
    if plan.delay_regs:
        L.append("// carry registers (delay edges d>=1): written every cycle, read by later flits")
        for r in plan.delay_regs.values():
            L.append(f"reg  {r.id};   // = {r.src} delayed {r.k} flit(s)")
    L.append("// AND consumer units: evaluated only when flit_idx is in the schedule (derived enable)")
    for u in plan.and_units.values():
        L.append(f"wire {u.id} = {' && '.join(_ref_name(r) for r in u.inputs)};"
                 f"   // en: flit_idx in {describe_schedule(u.schedule)}")
    fams = {f.sticky: f for f in plan.families}
    for s in plan.stickies.values():
        f = fams[s.id]
        wid = next(o.id_width for o in plan.output if o.ref == s.id)
        L.append(f"reg {s.id};  // family {f.name} = rows {_rows_label(f.row_indices)}, "
                 f"path_id {wid}'h{f.path_id:x}, sticky, loop (self edge d=1), cleared at SOP; "
                 f"changes in {describe_schedule(s.schedule)}")
    for a in plan.alive.values():
        L.append(f"reg  {a.id};   // one-hot alive, set at SOP, loop (d=1); last constrained flit t={a.reach}")
    L.append("")
    L.append("// ----- per cycle (state updates are nonblocking <=) -----")
    cycles = [(t, _plan_cycle(plan, t)) for t in range(cfg.num_flits)]
    for a, b, stmts in _group(cycles, expand):
        L.append(f"// {_tlabel(a, b)}:")
        for s in stmts:
            L.append(s if s.startswith("//") else "  " + s)
    L.append("")
    L.extend(_plan_eop(plan))
    for n in plan.notes:
        L.append(f"// note: {n}")
    return "\n".join(L)


def _plan_eop(plan: HwPlan) -> List[str]:
    L = ["// ----- EOP: priority if/else (first match wins); uses state AFTER the EOP flit's update -----"]
    first = True
    for o in plan.output:
        if o.kind == "always":
            cond = "1'b1"
        elif o.kind == "family":
            cond = o.ref
        else:
            cond = o.ref + (f" && eop_t >= {o.reach}" if o.reach else "")
        L.append(f"{'if' if first else 'else if'} ({cond}) path_id = {o.id_width}'h{o.path_id:x};"
                 f"   // {o.kind}, prio {o.priority}")
        first = False
    L.append(f"else path_id = '0;" if not first else "path_id = '0;")
    return L


# -------------------------------------------------------- static: baseline
def _base_cycle(bp: BaselinePlan, t: int) -> tuple:
    stmts = [f"// {len(bp.cmps_at(t))} comparators evaluated, {len(bp.latches_at(t))} latches captured"]
    for l in bp.latches_at(t):
        stmts.append(f"{l.id} <= {' && '.join(l.inputs)};")
    return tuple(stmts)


def format_static_baseline(bp: BaselinePlan, expand: bool = False) -> str:
    cfg = bp.config
    L = [f"// ===== BASELINE (PTP-style): {cfg.bytes_per_flit} B flits, T={cfg.num_flits} flits =====",
         "// comparator bank per flit: one comparator per (t, lane, mask, value)"]
    for c in bp.comparators:
        L.append(f"wire {c.id} = {sv_cmp(c.lane, c.mask, c.value)};   // only meaningful in flit {c.t}")
    L.append("reg " + ", ".join(l.id for l in bp.latches[:4]) +
             (f", ... ({len(bp.latches)} partial-match latches, reset 0 at SOP)" if len(bp.latches) > 4
              else "   // partial-match latches, reset 0 at SOP"))
    L.append("")
    L.append("// ----- per cycle -----")
    cycles = [(t, _base_cycle(bp, t)) for t in range(cfg.num_flits)]
    for a, b, stmts in _group(cycles, expand):
        L.append(f"// {_tlabel(a, b)}:")
        for s in stmts:
            L.append(s if s.startswith("//") else "  " + s)
    L.append("")
    L.append("// ----- EOP: priority if/else over rows (first match wins) -----")
    for i, r in enumerate(bp.rows):
        cond = " && ".join(r.latches) or "1'b1"
        L.append(f"{'if' if i == 0 else 'else if'} ({cond}) path_id = {r.id_width}'h{r.path_id:x};"
                 f"   // r{r.row_index}")
    L.append("else path_id = '0;")
    return "\n".join(L)


# ----------------------------------------------------------------- summary
def format_summary(plan: HwPlan, bp: BaselinePlan) -> str:
    p, b = plan.counts(), bp.counts()
    rows = [
        ("comparators", b["comparators"], p["comparators"]),
        ("AND consumer units / gates", b["and_gates"], f"{p['and_units']} units / {p['and_gates']} gates"),
        ("AND gate inputs", b["and_inputs"], p["and_inputs"]),
        ("OR inputs (sticky)", 0, p["or_inputs"]),
        ("FFs total", b["ffs"], p["ffs"]),
        ("  partial-match latches", b["latches"], 0),
        ("  alive bits (one-hot)", 0, p["alive_ffs"]),
        ("  sticky bits", 0, p["sticky_ffs"]),
        ("  carry (delay) regs", 0, p["carry_ffs"]),
        ("unit evals / full packet (cmp+AND)", b["evals_per_packet"], p["evals_per_packet"]),
        ("EOP if/else chain length", b["eop_chain"], p["eop_chain"]),
        ("loops (self edges d=1)", b["loops"], p["loops"]),
        ("carry (d>=1) cmp edges", 0, p["carry_edges"]),
    ]
    w = max(len(r[0]) for r in rows)
    L = [f"{'metric':<{w}}  {'baseline':>10}  {'optimized':>22}", "-" * (w + 38)]
    for name, x, y in rows:
        L.append(f"{name:<{w}}  {str(x):>10}  {str(y):>22}")
    return "\n".join(L)


# ----------------------------------------------------------------- dynamic
def _hexflit(data: bytes, nvalid: int) -> str:
    return " ".join(f"{b:02x}" if i < nvalid else "--" for i, b in enumerate(data))


def format_dynamic(plan: HwPlan, bp: BaselinePlan, rows, packet: bytes) -> str:
    cfg = plan.config
    gold = eval_pir(rows, cfg, packet)
    rp = run_plan(plan, packet)
    rb = run_baseline(bp, packet)
    ok = rp.path_id == gold == rb.path_id
    L = [f"packet ({len(packet)} B, window {cfg.max_bytes} B): {packet.hex() or '(empty)'}",
         f"path_id: golden={gold}  plan={rp.path_id}  baseline={rb.path_id}   "
         f"{'OK' if ok else 'MISMATCH'}", ""]
    for i, (tp, tb) in enumerate(zip(rp.trace, rb.trace)):
        L.append(f"=== t={tp.t}   flit bytes [{_hexflit(tp.data, tp.nvalid)}]   "
                 f"(valid {tp.nvalid}/{cfg.bytes_per_flit}) ===")
        fired = [c for c, v in tp.consumers if v]
        L.append(f"  PLAN     comparators evaluated {len(tp.cmp_evals)}, true: "
                 f"{', '.join(tp.cmp_true) or '-'}")
        L.append(f"           consumers enabled {len(tp.consumers)}, fired: {', '.join(fired) or '-'}"
                 f"   (unit evals this cycle: {rp.evals_per_cycle[i]}, max per unit: 1)")
        for a in tp.actions:
            L.append(f"           branch  {a}")
        L.append("           regs    " + " ".join(f"{k}={v}" for k, v in tp.state.items()))
        fb = [c for c, v in tb.consumers if v]
        L.append(f"  BASELINE comparators evaluated {len(tb.cmp_evals)}, true: "
                 f"{', '.join(tb.cmp_true) or '-'}")
        L.append(f"           latches captured {len(tb.consumers)}, set: {', '.join(fb) or '-'}")
        ones = [k for k, v in tb.state.items() if v]
        L.append(f"           latches currently 1: {', '.join(ones) or '-'}")
    if not rp.trace:
        L.append("(no flits)")
    L.append("")
    L.append("EOP  PLAN priority chain:")
    L.extend("       " + x for x in rp.eop_actions)
    L.append("EOP  BASELINE priority chain (rows tested until first match):")
    shown = rb.eop_actions
    L.extend("       " + x for x in (shown if len(shown) <= 8 else shown[:3] + ["  ..."] + shown[-2:]))
    L.append(f"RESULT golden={gold}  plan={rp.path_id}  baseline={rb.path_id}   {'OK' if ok else 'MISMATCH'}")
    return "\n".join(L)
