"""Common software metrics for baseline / hand plan / solver machine.

CLI (run from ``engine/``)::

    python -m pir_opt.solver.compare <hex> <json> [--check N] [--seed S] [--detail] [--no-fold]

All columns use the SAME definitions (see ``SOLVER.md``):

* distinct comparators / comparator input bits: baseline = one per atom (popcount of mask); hand plan = one per
  ``cmp_l<lane>_<pred>`` unit; solver = class indicators that survive in the minimized next-state logic (input bits =
  bits the indicator truly depends on); the solver is also given in *predicate form* (smallest predicate comparator
  set from which all used class indicators can be derived).
* state bits after flit t: information-carrying register bits (a bit that is still its reset constant is not counted:
  hand plan alive bit before its first update, sticky bit before its first schedule flit; baseline latches written
  later; solver bits of the Moore state).  ``FFs allocated`` = registers the structure instantiates (solver: the
  register is reused across t, so max over t).
* per-cycle logic = Espresso-minimized SOP literals of every live next-state bit in cycle t as a function of that
  cycle's comparator outputs and state bits (hold = ``bit' = bit`` costs 1 literal for everyone; the flit counter /
  enables are not counted).  Hand plan / baseline functions are written down from their structure (a single product
  term per latch / alive, a sum for sticky) and pushed through the same minimizer.
* EOP output logic after flit t = Espresso SOP literals of the path-id bits as functions of the state bits live after
  flit t (hand plan / baseline: the priority chain, adjacent rows with equal id OR-merged; solver: state -> id).
* mismatches vs golden over directed + N random vectors (vectors.py), incl. short and over-long packets.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..atoms import atomize
from ..baseline import BaselinePlan, build_baseline
from ..cycle_model import run_baseline, run_plan
from ..golden import eval_pir
from ..pir import load_pir
from ..plan import HwPlan, build_plan
from ..vectors import bench_directed, directed_vectors, random_vectors
from . import logic
from .classes import min_predicate_form, support_bits
from .machine import (LogicSim, Machine, MachineLogic, assign_encodings, derive_machine, fold_report,
                      minimize_machine, run_table)
from .tdd import DDBlowup, TDD, build_bdd_states, build_tdd, have_dd

SOLVER_KINDS = [("binary", "solver(binary)"), ("onehot", "solver(one-hot)"), ("onehot0", "solver(1hot-0)")]


@dataclass
class Metrics:
    name: str
    n_cmp: int = 0
    cmp_bits: int = 0
    cmp_pred: Optional[int] = None            # solver only: predicate-form comparator count
    cmp_pred_bits: Optional[int] = None
    cmp_names: List[List[str]] = field(default_factory=list)   # per cycle
    state_bits: List[int] = field(default_factory=list)        # live bits after flit t, t = 0..T-1
    ffs_alloc: int = 0
    ns_lits: List[int] = field(default_factory=list)
    ns_terms: List[int] = field(default_factory=list)
    out_lits: List[Optional[int]] = field(default_factory=list)
    mismatches: int = 0
    exact: bool = True
    notes: List[str] = field(default_factory=list)

    @property
    def ns_total(self) -> int:
        return sum(self.ns_lits)

    @property
    def out_max(self) -> Optional[int]:
        v = [x for x in self.out_lits if x is not None]
        return max(v) if v else None


def _min_all(covers: Dict[str, logic.Cover]) -> Tuple[Dict[str, logic.Minimized], bool]:
    res = {k: logic.minimize(c) for k, c in covers.items()}
    return res, all(r.exact for r in res.values())


def _row_lits(res: Dict[str, logic.Minimized]) -> Tuple[int, int]:
    return sum(r.literals for r in res.values()), sum(r.terms for r in res.values())


def _out_lits(terms, idw: int) -> Tuple[Optional[int], bool]:
    try:
        with logic.time_limit(20):
            bits = logic.chain_cover(terms, idw)
    except logic.StepTimeout:
        bits = None
    if bits is None:
        return None, False
    mins = [logic.minimize(c) for c in bits]
    return sum(m.literals for m in mins), all(m.exact for m in mins)


# ------------------------------------------------------------------ baseline
def baseline_metrics(bp: BaselinePlan, idw: int) -> Metrics:
    T = bp.config.num_flits
    M = Metrics("baseline")
    M.n_cmp = len(bp.comparators)
    M.cmp_bits = sum(bin(c.mask).count("1") for c in bp.comparators)
    M.ffs_alloc = len(bp.latches)
    for t in range(T):
        fn: Dict[str, logic.Cover] = {}
        for l in bp.latches:
            if l.t == t:
                fn[l.id] = [frozenset((c, True) for c in l.inputs)]
            elif l.t < t:
                fn[l.id] = [frozenset([(l.id, True)])]
        res, ex = _min_all(fn)
        M.exact &= ex
        lits, terms = _row_lits(res)
        M.ns_lits.append(lits)
        M.ns_terms.append(terms)
        M.state_bits.append(len(fn))
        sup = sorted({v for r in res.values() for v in r.support() if v.startswith("cmp_")})
        M.cmp_names.append(sup)
        lt = {l.id: l.t for l in bp.latches}
        terms_ = []
        for r in bp.rows:
            if not r.latches:
                terms_.append((r.path_id, [frozenset()]))
            elif all(lt[x] <= t for x in r.latches):
                terms_.append((r.path_id, [frozenset((x, True) for x in r.latches)]))
        v, ex = _out_lits(terms_, idw)
        M.out_lits.append(v)
        M.exact &= ex
    return M


# ------------------------------------------------------------------ hand plan
def _plan_comp_lits(plan: HwPlan, srcs) -> List[Tuple[str, bool]]:
    lits = []
    for s in srcs:
        if s in plan.cmp_units:
            lits.append((s, True))
        else:
            for r in plan.and_units[s].inputs:
                lits.append((r.unit if r.delay == 0 else f"{r.unit}_d{r.delay}", True))
    return lits


def plan_metrics(plan: HwPlan, idw: int) -> Metrics:
    T = plan.config.num_flits
    M = Metrics("hand plan")
    M.n_cmp = len(plan.cmp_units)
    M.cmp_bits = sum(bin(u.mask).count("1") for u in plan.cmp_units.values())
    M.ffs_alloc = plan.counts()["ffs"]
    for t in range(T):
        fn: Dict[str, logic.Cover] = {}
        for a in plan.alive.values():
            t0 = min(a.terms)
            if t < t0:
                continue
            if t in a.terms:
                lits = _plan_comp_lits(plan, a.terms[t])
                if t > t0:
                    lits.append((a.id, True))
                fn[a.id] = [frozenset(lits)]
            else:
                fn[a.id] = [frozenset([(a.id, True)])]
        for s in plan.stickies.values():
            ts = min(s.schedule)
            if t < ts:
                continue
            if t in s.schedule:
                cubes = [frozenset([(s.id, True)])] if t > ts else []
                for c in s.consumers:
                    u = plan.and_units[c]
                    if t in u.schedule:
                        cubes.append(frozenset((r.unit if r.delay == 0 else f"{r.unit}_d{r.delay}", True)
                                               for r in u.inputs))
                fn[s.id] = cubes
            else:
                fn[s.id] = [frozenset([(s.id, True)])]
        for r in plan.delay_regs.values():
            if t in r.capture_cycles:
                src = r.src if r.k == 1 else f"{r.src}_d{r.k - 1}"
                fn[r.id] = [frozenset([(src, True)])]
        res, ex = _min_all(fn)
        M.exact &= ex
        lits, terms = _row_lits(res)
        M.ns_lits.append(lits)
        M.ns_terms.append(terms)
        M.state_bits.append(len(fn))
        M.cmp_names.append(sorted({v for r in res.values() for v in r.support() if v in plan.cmp_units}))
        terms_ = []
        for o in plan.output:
            if o.kind == "always":
                terms_.append((o.path_id, [frozenset()]))
            elif o.kind == "family":
                if t >= min(plan.stickies[o.ref].schedule):
                    terms_.append((o.path_id, [frozenset([(o.ref, True)])]))
            elif t >= o.reach:
                terms_.append((o.path_id, [frozenset([(o.ref, True)])]))
        v, ex = _out_lits(terms_, idw)
        M.out_lits.append(v)
        M.exact &= ex
    return M


# ------------------------------------------------------------------ solver
def solver_metrics(m: Machine, kind: str, label: str, ml: MachineLogic) -> Metrics:
    T = m.T
    enc = m.enc[kind]
    M = Metrics(label)
    used: Set[Tuple[int, int]] = set()
    for t in range(T):
        names = []
        for v in sorted(ml.support[t], key=lambda s: tuple(int(x) for x in s[1:].split("_"))):
            lane, cid = (int(x) for x in v[1:].split("_"))
            used.add((lane, cid))
            names.append(f"l{lane}:{m.tdd.lanes[lane].classes[cid].label}")
        M.cmp_names.append(names)
        M.ns_lits.append(sum(r.literals for r in ml.ns[t]))
        M.ns_terms.append(sum(r.terms for r in ml.ns[t]))
        M.out_lits.append(sum(r.literals for r in ml.out[t]))
        M.state_bits.append(enc.bits[t + 1])
    M.exact = ml.exact
    M.n_cmp = len(used)
    M.cmp_bits = sum(support_bits(m.tdd.lanes[l].classes[c].values) for l, c in used)
    M.ffs_alloc = max(M.state_bits) if M.state_bits else 0
    pc = pb = 0
    for lane in sorted({l for l, _ in used}):
        preds, bits = min_predicate_form(m.tdd.lanes[lane], sorted(c for l, c in used if l == lane))
        pc += len(preds)
        pb += bits
    M.cmp_pred, M.cmp_pred_bits = pc, pb
    return M


# ------------------------------------------------------------------ driver
@dataclass
class BenchResult:
    name: str
    cfg: object
    metrics: List[Metrics]
    states_mdd: List[int]
    states_bdd: Optional[List[int]]
    dd_info: Dict[str, int]
    machine: Machine
    folding: Optional[Dict[Tuple[int, int], str]]
    packets: int
    table_mismatches: int
    lane_classes: List[str]
    chain: Tuple[int, int] = (0, 0)


def run_bench(hex_path: str, json_path: str, n_check: int = 3000, seed: int = 0, fold: bool = True,
              name: Optional[str] = None) -> BenchResult:
    rows, cfg = load_pir(hex_path, json_path)
    ap = atomize(rows, cfg)
    idw = max([1] + [r.id_width for r in rows if not r.is_extraction])
    plan, bp = build_plan(ap, rows), build_baseline(ap, rows)
    tdd = build_tdd(ap, idw)
    m = derive_machine(tdd)
    assign_encodings(m)
    mls = {k: minimize_machine(m, k) for k, _ in SOLVER_KINDS}
    metrics = [baseline_metrics(bp, idw), plan_metrics(plan, idw)]
    metrics += [solver_metrics(m, k, lab, mls[k]) for k, lab in SOLVER_KINDS]

    vecs = bench_directed(cfg.max_bytes) + directed_vectors(ap, seed + 1) + random_vectors(ap, n_check, seed)
    gold = [eval_pir(rows, cfg, p) for p in vecs]
    metrics[0].mismatches = sum(run_baseline(bp, p, trace=False).path_id != g for p, g in zip(vecs, gold))
    metrics[1].mismatches = sum(run_plan(plan, p, trace=False).path_id != g for p, g in zip(vecs, gold))
    for i, (k, _) in enumerate(SOLVER_KINDS):
        sim = LogicSim(m, mls[k])
        metrics[2 + i].mismatches = sum(sim.run(p) != g for p, g in zip(vecs, gold))
    tbl = sum(run_table(m, p) != g for p, g in zip(vecs, gold))

    bdd_states, dd_info = None, {"mdd_nodes": tdd.mdd.size(tdd.root), "x_vars": len(tdd.var_pos)}
    if have_dd():
        r = build_bdd_states(ap, idw)
        bdd_states = r.states
        dd_info.update(bdd_nodes=r.f_size, bdd_vars=r.nvars)
    folding = None
    if fold:
        try:
            with logic.time_limit(30):
                folding = fold_report(m)
        except logic.StepTimeout:
            folding = None
    lanes = [f"lane {lc.lane}: " + " | ".join(f"{c.label}" for c in lc.classes) for lc in tdd.lanes]
    return BenchResult(name or os.path.splitext(os.path.basename(hex_path))[0], cfg, metrics, m.n_states(),
                       bdd_states, dd_info, m, folding, len(vecs), tbl, lanes, (len(bp.rows), len(plan.output)))


def _fmt_list(xs) -> str:
    return "[" + ",".join("skipped(too large)" if x is None else str(x) for x in xs) + "]"


def format_table(br: BenchResult) -> str:
    cols = [m.name for m in br.metrics]
    rows: List[Tuple[str, List[str]]] = []

    def add(label, f):
        rows.append((label, [f(m) for m in br.metrics]))
    add("distinct comparators", lambda m: str(m.n_cmp))
    add("comparator input bits", lambda m: str(m.cmp_bits))
    add("  predicate-form comparators", lambda m: str(m.cmp_pred if m.cmp_pred is not None else m.n_cmp))
    add("  predicate-form input bits", lambda m: str(m.cmp_pred_bits if m.cmp_pred_bits is not None else m.cmp_bits))
    add("state bits after flit t", lambda m: _fmt_list(m.state_bits))
    add("FFs allocated", lambda m: str(m.ffs_alloc))
    add("comparators read per cycle", lambda m: _fmt_list([len(x) for x in m.cmp_names]))
    add("next-state literals per cycle", lambda m: _fmt_list(m.ns_lits))
    add("next-state literals total", lambda m: str(m.ns_total))
    add("next-state terms total", lambda m: str(sum(m.ns_terms)))
    add("EOP output literals per t", lambda m: _fmt_list(m.out_lits))
    rows.append(("EOP priority-chain entries (structural)", [str(br.chain[0]), str(br.chain[1]), "0", "0", "0"]))
    add("EOP output literals max", lambda m: "-" if m.out_max is None else str(m.out_max))
    add(f"mismatches vs golden ({br.packets})", lambda m: str(m.mismatches))
    add("all covers Espresso-minimized", lambda m: "yes" if m.exact else "NO (size cap)")
    w0 = max(len(r[0]) for r in rows)
    ws = [max(len(c), *(len(r[1][i]) for r in rows)) for i, c in enumerate(cols)]
    lines = [" " * w0 + "  " + "  ".join(c.ljust(ws[i]) for i, c in enumerate(cols))]
    lines.append("-" * len(lines[0]))
    for lab, vals in rows:
        lines.append(lab.ljust(w0) + "  " + "  ".join(v.ljust(ws[i]) for i, v in enumerate(vals)))
    return "\n".join(lines)


def format_states(br: BenchResult) -> str:
    T = br.cfg.num_flits
    lines = ["states at the cut after flit t (Moore, one flit per cycle; last column = final / output states)",
             "  t           " + " ".join(f"{t:>4}" for t in range(T)),
             "  MDD states  " + " ".join(f"{s:>4}" for s in br.states_mdd),
             "  BDD states  " + (" ".join(f"{s:>4}" for s in br.states_bdd) if br.states_bdd else "  (dd library not available)"),
             "  min bits    " + " ".join(f"{(s - 1).bit_length():>4}" for s in br.states_mdd)]
    ok = "" if br.states_bdd is None else ("   BDD == MDD: " + ("yes" if br.states_bdd == br.states_mdd else "NO"))
    lines[-1] += ok
    return "\n".join(lines)


def format_detail(br: BenchResult) -> str:
    out = [format_states(br), "", "DD sizes: " + ", ".join(f"{k}={v}" for k, v in br.dd_info.items()),
           "lane classes (class 0 = rest/absent):"] + ["  " + s for s in br.lane_classes]
    out.append("")
    out.append("comparators read per cycle:")
    for mt in br.metrics:
        out.append(f"  {mt.name}:")
        for t, names in enumerate(mt.cmp_names):
            if names:
                short = names if len(names) <= 24 else names[:24] + [f"... (+{len(names) - 24})"]
                out.append(f"    t={t}: {' '.join(short)}")
    if br.folding is not None:
        out.append("")
        out.append("folding (g_t1 vs g_t2 up to state renaming; 'iso' = identical, 'embeds' = g_t1 is a restriction of g_t2):")
        triv = [all(len(set(d.id for _, d in br.machine.transitions[t][s.id])) == 1
                    for s in br.machine.boundary[t]) for t in range(br.machine.T)]
        out.append("  trivial cycles (every state ignores the flit): " + str([t for t, v in enumerate(triv) if v]))
        nt = [t for t in range(br.machine.T) if not triv[t]]
        par = {t: t for t in nt}

        def find(x):
            while par[x] != x:
                x = par[x]
            return x
        for (x, y), r in br.folding.items():
            if r == "iso" and x in par and y in par:
                par[find(y)] = find(x)
        classes: Dict[int, List[int]] = {}
        for t in nt:
            classes.setdefault(find(t), []).append(t)
        multi = [v for v in classes.values() if len(v) > 1]
        out.append("  identical up to renaming (iso classes of non-trivial cycles): "
                   + (", ".join("{" + ",".join(map(str, v)) + "}" for v in multi) if multi else "none"))
        emb = [f"g{a}<g{b}" if r == "embeds" else f"g{b}<g{a}" for (a, b), r in sorted(br.folding.items())
               if r in ("embeds", "embedded") and a in par and b in par and find(a) != find(b)]
        out.append("  strict restrictions (g_x < g_y: g_x is a sub-machine of g_y): "
                   + (", ".join(emb[:16]) + (" ..." if len(emb) > 16 else "") if emb else "none"))
    elif br.machine is not None:
        out.append("")
        out.append("folding: skipped (step timeout)")
    out.append(f"explicit table walk mismatches: {br.table_mismatches} / {br.packets}")
    return "\n".join(out)


def main(argv: "Optional[List[str]]" = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("hex")
    p.add_argument("json")
    p.add_argument("--check", type=int, default=3000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--detail", action="store_true")
    p.add_argument("--no-fold", action="store_true")
    a = p.parse_args(argv)
    if not logic.have_pyeda():
        print("pyeda not available: run with engine/pir_opt/.venv/bin/python", file=sys.stderr)
        return 2
    br = run_bench(a.hex, a.json, a.check, a.seed, not a.no_fold)
    cfg = br.cfg
    print(f"== {br.name}: {cfg.data_width}-bit flits ({cfg.bytes_per_flit} B), window {cfg.max_bytes} B, T={cfg.num_flits}")
    print(format_table(br))
    print()
    print(format_detail(br) if a.detail else format_states(br))
    bad = sum(m.mismatches for m in br.metrics) + br.table_mismatches
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
