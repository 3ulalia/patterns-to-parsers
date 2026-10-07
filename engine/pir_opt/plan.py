"""Optimized hardware plan (PIR_OPTIMIZER_PLAN.md section 4.1 / Phase 2).

Time model recap: byte ``k`` is at ``(t, lane) = (k // B, k % B)``; ``t`` is the
flit index. The plan keeps *units* (hardware, no time), *schedules* (flits in
which a unit's output is consumed) and *edges* with delays in flits.

What ``build_plan`` does
------------------------
1. **Comparator binding across time.** One ``CmpUnit`` per ``(lane, mask,
   value)`` whatever ``t``; its schedule is every flit in which some consumer
   reads (or captures into a carry register) its output. A comparator output
   is always gated by byte-valid (``lane < nvalid`` of the flit).
2. **Folded families.** A family ``(path_id, shape)`` with >= 2 distinct
   anchors becomes, per anchor lane ``b``, ONE AND consumer unit
   ``and_f<k>_l<b>``. Its inputs are the comparators for the shape bytes at
   lanes ``(b+off) % B``. If the shape's last byte lands ``k`` flits after an
   earlier byte, that input has delay edge ``d = (b+last)//B - (b+off)//B``
   (``d`` registers = a "carry"). The consumer's schedule is the set of flits
   ``t = (anchor+last)//B`` in which its last byte arrives, over the anchors with
   lane ``b``; its enable is ``flit_idx in schedule`` (derived, not written).
   The family result is a **sticky bit** ``found_f<k> <= found | OR(enabled
   consumers)`` (self edge d=1, a loop), cleared at SOP.
3. **Anchored rows** (everything not folded): one-hot ``alive_r<row>`` bit, set
   at SOP and, in each flit ``t`` where the row has atoms, ``alive &= AND(those
   comparators)``. The per-``t`` AND terms are consumer units shared by
   structure (CSE), selected per cycle by their schedule. At EOP the row matches
   iff alive and the packet reached the flit of its last constrained byte
   (``reach``; a flit that never arrived cannot clear ``alive`` by itself).
   State encoding is one-hot behind ``AliveBit`` so it can be swapped later.
4. **Output stage**: EOP priority if/else over folded families (priority = the
   family segment's best row) and alive bits, in original priority order.

Priority soundness (non-contiguous families)
--------------------------------------------
Giving a family the priority of its best row is only sound if no row with a
*different* path id lies strictly between its best and worst row in priority
(it could win over a later family row but lose to an earlier one). So a family
is first **split into maximal contiguous segments** (separated by foreign-id
rows). Each segment with >= 2 distinct anchors is folded (its own sticky bit,
named ``f<k>``, k = priority rank); a segment with one anchor stays an anchored
row. Comparator units are still shared across all of them. Exact duplicate rows
(same id, same shape, same anchor) are dropped from the plan (a later duplicate
can never change the result).

Short packets: every comparator output is gated by byte-valid, and anchored
rows additionally require ``reach``. The EOP stage reads the *next-state*
(post-update) alive/found values of the EOP flit (combinational tail).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from .atoms import AtomizedPir, AtomRow, Shape, cmp_suffix, compress_ranges
from .pir import PirConfig, PirRow


class Ref(NamedTuple):
    """Input edge: output of ``unit`` delayed by ``delay`` flits (registers)."""
    unit: str
    delay: int = 0


@dataclass
class CmpUnit:
    """Comparator unit: ``valid[lane] && (byte[lane] & mask) == value``."""
    id: str
    lane: int
    mask: int
    value: int
    schedule: Tuple[int, ...] = ()
    kind: str = "cmp"


@dataclass
class AndUnit:
    """AND consumer unit; evaluated only in flits of its schedule (= its enable)."""
    id: str
    inputs: Tuple[Ref, ...]
    schedule: Tuple[int, ...] = ()
    role: str = "row"                 # "family" | "row"
    family: Optional[str] = None
    lane: Optional[int] = None        # anchor lane (family consumers)
    kind: str = "and"


@dataclass
class DelayReg:
    """Carry register ``<src>_d<k>``: value of ``src`` k flits ago."""
    id: str
    src: str
    k: int
    capture_cycles: Tuple[int, ...]   # cycles in which the captured value is meaningful


@dataclass
class StickyBit:
    id: str                           # found_f<k>
    family: str
    consumers: Tuple[str, ...]
    schedule: Tuple[int, ...]         # flits where it can change
    loop: bool = True                 # self edge d=1


@dataclass
class AliveBit:
    id: str                           # alive_r<row>
    row_index: int
    terms: Dict[int, Tuple[str, ...]]  # t -> unit ids ANDed in flit t (cmp or and units)
    reach: int                         # flit of the last constrained byte
    loop: bool = True


@dataclass
class FamilyPlan:
    name: str
    path_id: int
    priority: int
    shape: Shape
    anchors: Tuple[int, ...]
    row_indices: Tuple[int, ...]
    consumers: Tuple[str, ...]        # and unit ids
    sticky: str
    carry_bytes: int                  # shape length - 1 in bytes (max over bytes)


@dataclass
class OutputTerm:
    kind: str                         # "family" | "row" | "always"
    ref: str                          # sticky id / alive id / "" (always)
    path_id: int
    id_width: int
    priority: int
    reach: int = 0


@dataclass
class Edge:
    src: str
    dst: str
    delay: int
    kind: str                         # "data" | "carry" | "state"
    loop: bool = False


@dataclass
class HwPlan:
    ap: AtomizedPir
    cmp_units: Dict[str, CmpUnit]
    and_units: Dict[str, AndUnit]
    delay_regs: Dict[str, DelayReg]
    stickies: Dict[str, StickyBit]
    alive: Dict[str, AliveBit]
    families: List[FamilyPlan]
    output: List[OutputTerm]          # priority order
    atom_unit: Dict[int, str]         # atom id -> cmp unit id
    edges: List[Edge] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def config(self) -> PirConfig:
        return self.ap.config

    def unit(self, uid: str):
        return self.cmp_units[uid] if uid in self.cmp_units else self.and_units[uid]

    def counts(self) -> Dict[str, int]:
        """Structural counts (see ``baseline.BaselinePlan.counts`` for the baseline side)."""
        multi = [u for u in self.and_units.values() if len(u.inputs) >= 2]
        and_in = sum(len(u.inputs) for u in multi)
        # per-(alive,t) update gate: alive & terms (alive input absent at t=0)
        upd_in = 0
        n_upd = 0
        for a in self.alive.values():
            for t, srcs in a.terms.items():
                n = len(srcs) + (1 if t > 0 else 0)
                if n >= 2:
                    n_upd += 1
                    upd_in += n
        out_and = sum(1 for o in self.output if o.kind == "row" and o.reach > 0)
        or_in = sum(1 + len(s.consumers) for s in self.stickies.values())
        evals = (sum(len(u.schedule) for u in self.cmp_units.values())
                 + sum(len(u.schedule) for u in self.and_units.values()))
        ffs_alive, ffs_sticky, ffs_carry = len(self.alive), len(self.stickies), len(self.delay_regs)
        return {
            "comparators": len(self.cmp_units),
            "baseline_comparators": len(self.ap.atoms),
            "and_units": len(self.and_units),
            "and_gates": len(multi) + n_upd + out_and,
            "and_inputs": and_in + upd_in + 2 * out_and,
            "or_inputs": or_in,
            "alive_ffs": ffs_alive,
            "sticky_ffs": ffs_sticky,
            "carry_ffs": ffs_carry,
            "ffs": ffs_alive + ffs_sticky + ffs_carry,
            "evals_per_packet": evals,
            "eop_chain": len(self.output),
            "loops": sum(1 for e in self.edges if e.loop),
            "carry_edges": sum(1 for e in self.edges if e.kind == "carry"),
            "folded_families": len(self.families),
            "family_consumers": sum(len(f.consumers) for f in self.families),
        }


# ---------------------------------------------------------------- helpers
def cmp_unit_id(lane: int, mask: int, value: int) -> str:
    return f"cmp_l{lane}_{cmp_suffix(mask, value)}"


def _segments(ap: AtomizedPir) -> List[Tuple[object, List[AtomRow]]]:
    """Split every family into maximal priority-contiguous segments (see module doc)."""
    by_index = {r.index: r for r in ap.rows}
    all_prios = sorted(r.priority for r in ap.rows)
    path_prios: Dict[int, List[int]] = {}
    for r in ap.rows:
        path_prios.setdefault(r.path_id, []).append(r.priority)
    for v in path_prios.values():
        v.sort()

    def count(lst: List[int], lo: int, hi: int) -> int:  # priorities strictly in (lo, hi)
        return bisect.bisect_left(lst, hi) - bisect.bisect_right(lst, lo)

    segs: List[Tuple[object, List[AtomRow]]] = []
    for fam in ap.families:
        members = sorted((by_index[i] for i in fam.row_indices), key=lambda r: r.priority)
        uniq, seen = [], set()
        for r in members:                      # drop exact duplicates (same id/shape/anchor)
            if r.anchor in seen:
                continue
            seen.add(r.anchor)
            uniq.append(r)
        cur: List[AtomRow] = []
        for r in uniq:
            if cur:
                lo, hi = cur[-1].priority, r.priority
                foreign = count(all_prios, lo, hi) - count(path_prios[fam.path_id], lo, hi)
                if foreign > 0:
                    segs.append((fam, cur))
                    cur = []
            cur.append(r)
        if cur:
            segs.append((fam, cur))
    return segs


# ------------------------------------------------------------------ build
def build_plan(ap: AtomizedPir, rows: Optional[Sequence[PirRow]] = None) -> HwPlan:
    """Build the optimized plan. ``rows`` only supplies id literal widths."""
    cfg = ap.config
    B = cfg.bytes_per_flit
    widths = {r.index: r.id_width for r in rows} if rows else {}

    def id_width(row: AtomRow) -> int:
        return widths.get(row.index) or max(1, row.path_id.bit_length())

    cmp_sched: Dict[Tuple[int, int, int], Set[int]] = {}
    atom_unit: Dict[int, str] = {}
    for aid, a in enumerate(ap.atoms):
        atom_unit[aid] = cmp_unit_id(a.lane, a.mask, a.value)

    def use(lane: int, mask: int, value: int, t: int) -> str:
        cmp_sched.setdefault((lane, mask, value), set()).add(t)
        return cmp_unit_id(lane, mask, value)

    and_units: Dict[str, AndUnit] = {}
    and_sched: Dict[str, Set[int]] = {}
    delay_cap: Dict[Tuple[str, int], Set[int]] = {}
    stickies: Dict[str, StickyBit] = {}
    alive: Dict[str, AliveBit] = {}
    fam_plans: List[FamilyPlan] = []
    output: List[OutputTerm] = []
    notes: List[str] = []

    folded_rows: Set[int] = set()
    anchored: List[AtomRow] = []

    segments = _segments(ap)
    # family names are structural: f<k>, k = rank of the segment's best row in priority order
    folded = sorted((s for s in segments if len(s[1]) >= 2 and s[0].shape),
                    key=lambda s: s[1][0].priority)
    fam_name = {id(s[1]): f"f{k}" for k, s in enumerate(folded)}

    for fam, seg in segments:
        if len(seg) >= 2 and fam.shape:
            name = fam_name[id(seg)]
            last = fam.shape[-1][0]
            by_lane: Dict[int, List[int]] = {}
            for r in seg:
                by_lane.setdefault(r.anchor % B, []).append(r.anchor)
            consumers: List[str] = []
            for b in sorted(by_lane):
                refs = []
                S = sorted({(a + last) // B for a in by_lane[b]})
                for off, m, v in fam.shape:
                    pos = b + off
                    d = (b + last) // B - pos // B
                    uid = cmp_unit_id(pos % B, m, v)
                    for t in S:
                        use(pos % B, m, v, t - d)
                        if d:
                            for k in range(1, d + 1):
                                delay_cap.setdefault((uid, k), set()).add(t - d + k - 1)
                    refs.append(Ref(uid, d))
                uid_and = f"and_{name}_l{b}"
                and_units[uid_and] = AndUnit(uid_and, tuple(refs), (), "family", name, b)
                and_sched[uid_and] = set(S)
                consumers.append(uid_and)
            sticky = f"found_{name}"
            sched = sorted(set().union(*(and_sched[c] for c in consumers)))
            stickies[sticky] = StickyBit(sticky, name, tuple(consumers), tuple(sched))
            prio = seg[0].priority
            fam_plans.append(FamilyPlan(name, fam.path_id, prio, fam.shape,
                                        tuple(sorted(r.anchor for r in seg)),
                                        tuple(r.index for r in seg), tuple(consumers),
                                        sticky, last))
            output.append(OutputTerm("family", sticky, fam.path_id, id_width(seg[0]), prio))
            folded_rows.update(r.index for r in seg)
            if len(seg) != len(fam.row_indices):
                notes.append(f"family {name}: {len(fam.row_indices)} rows -> segment of {len(seg)} "
                             f"(priority-contiguous split / duplicate drop)")
        else:
            anchored.extend(seg)

    # anchored rows -> alive bits with per-flit AND terms (CSE across rows)
    and_by_key: Dict[Tuple[str, ...], str] = {}
    for r in sorted(anchored, key=lambda r: r.priority):
        by_t: Dict[int, List[int]] = {}
        for aid in r.atom_ids:
            by_t.setdefault(ap.atoms[aid].t, []).append(aid)
        if not by_t:
            output.append(OutputTerm("always", "", r.path_id, id_width(r), r.priority))
            continue
        terms: Dict[int, Tuple[str, ...]] = {}
        for t in sorted(by_t):
            ins = sorted((ap.atoms[a] for a in by_t[t]), key=lambda a: (a.lane, a.mask, a.value))
            cids = tuple(use(a.lane, a.mask, a.value, t) for a in ins)
            if len(cids) == 1:
                terms[t] = cids
            else:
                key = cids
                if key not in and_by_key:
                    uid = f"and_r{r.index}_t{t}"
                    and_by_key[key] = uid
                    and_units[uid] = AndUnit(uid, tuple(Ref(c, 0) for c in cids), (), "row")
                    and_sched[uid] = set()
                and_sched[and_by_key[key]].add(t)
                terms[t] = (and_by_key[key],)
        aid_ = f"alive_r{r.index}"
        reach = max(by_t)
        alive[aid_] = AliveBit(aid_, r.index, terms, reach)
        output.append(OutputTerm("row", aid_, r.path_id, id_width(r), r.priority, reach))

    output.sort(key=lambda o: o.priority)

    cmp_units = {}
    for (lane, mask, value), s in sorted(cmp_sched.items()):
        uid = cmp_unit_id(lane, mask, value)
        cmp_units[uid] = CmpUnit(uid, lane, mask, value, tuple(sorted(s)))
    for uid, s in and_sched.items():
        and_units[uid].schedule = tuple(sorted(s))
    delay_regs = {f"{src}_d{k}": DelayReg(f"{src}_d{k}", src, k, tuple(sorted(c)))
                  for (src, k), c in sorted(delay_cap.items())}

    plan = HwPlan(ap, cmp_units, and_units, delay_regs, stickies, alive, fam_plans,
                  output, atom_unit, notes=notes)
    plan.edges = _build_edges(plan)
    _check_plan(plan)
    return plan


def _build_edges(plan: HwPlan) -> List[Edge]:
    edges: List[Edge] = []
    for u in plan.and_units.values():
        for ref in u.inputs:
            edges.append(Edge(ref.unit, u.id, ref.delay, "carry" if ref.delay else "data"))
    for s in plan.stickies.values():
        for c in s.consumers:
            edges.append(Edge(c, s.id, 0, "data"))
        edges.append(Edge(s.id, s.id, 1, "state", True))
    for a in plan.alive.values():
        for srcs in a.terms.values():
            for src in srcs:
                e = Edge(src, a.id, 0, "data")
                if e not in edges:
                    edges.append(e)
        edges.append(Edge(a.id, a.id, 1, "state", True))
    for o in plan.output:
        if o.ref:
            edges.append(Edge(o.ref, "path_id", 0, "data"))
    return edges


def _check_plan(plan: HwPlan) -> None:
    """Internal consistency: every consumer read is covered by the producer schedule."""
    for u in plan.and_units.values():
        for t in u.schedule:
            for ref in u.inputs:
                assert t - ref.delay in plan.cmp_units[ref.unit].schedule, (u.id, t, ref)
                assert t - ref.delay >= 0, (u.id, t, ref)
    for a in plan.alive.values():
        for t, srcs in a.terms.items():
            for s in srcs:
                assert t in plan.unit(s).schedule, (a.id, t, s)


# ------------------------------------------------------------------- JSON
def plan_to_json(plan: HwPlan) -> dict:
    """Emit a subset of the IR of PIR_OPTIMIZER_PLAN.md section 5."""
    cfg = plan.config
    units, schedule = [], {}
    for u in plan.cmp_units.values():
        units.append({"id": u.id, "kind": "cmp", "lane": u.lane,
                      "lane_bits": [u.lane * 8, u.lane * 8 + 7],
                      "pred": {"op": "EQ" if u.mask == 0xFF else "MASKED_EQ",
                               "mask": f"{u.mask:02x}", "value": f"{u.value:02x}"},
                      "valid_gated": True})
        schedule[u.id] = list(u.schedule)
    for u in plan.and_units.values():
        units.append({"id": u.id, "kind": "and", "role": u.role,
                      "inputs": [{"src": r.unit, "delay": r.delay} for r in u.inputs],
                      "family": u.family, "anchor_lane": u.lane})
        schedule[u.id] = list(u.schedule)
    for s in plan.stickies.values():
        units.append({"id": s.id, "kind": "or_sticky", "inputs": list(s.consumers) + [s.id],
                      "clear": "sop"})
        schedule[s.id] = list(s.schedule)
    for a in plan.alive.values():
        units.append({"id": a.id, "kind": "alive_onehot", "row": a.row_index,
                      "set": "sop", "reach": a.reach})
        schedule[a.id] = sorted(a.terms)
    banks = [{"id": f"B_{f.name}", "kind": "shared", "lanes": cfg.bytes_per_flit,
              "carry_bytes": f.carry_bytes, "families": [f.name],
              "enable": {"kind": "flit_mask",
                         "per_consumer": {c: list(plan.and_units[c].schedule) for c in f.consumers}}}
             for f in plan.families]
    banks += [{"id": f"S_{a.id}", "kind": "per_flit", "rows": [f"r{a.row_index}"],
               "flits": sorted(a.terms)} for a in plan.alive.values()]
    cnt = plan.counts()
    return {
        "config": {"data_width": cfg.data_width, "max_width": cfg.max_width,
                   "num_flits": cfg.num_flits},
        "units": units,
        "schedule": schedule,
        "edges": [{"src": e.src, "dst": e.dst, "delay": e.delay, "kind": e.kind,
                   **({"loop": True} if e.loop else {})} for e in plan.edges],
        "delay_regs": [{"id": r.id, "src": r.src, "k": r.k} for r in plan.delay_regs.values()],
        "atoms": [{"id": f"a{i}", "t": a.t, "lane": a.lane,
                   "pred": {"mask": f"{a.mask:02x}", "value": f"{a.value:02x}"},
                   "unit": plan.atom_unit[i]} for i, a in enumerate(plan.ap.atoms)],
        "banks": banks,
        "state": ([{"id": a.id, "encoding": "onehot", "width": 1} for a in plan.alive.values()]
                  + [{"id": s.id, "kind": "sticky", "width": 1} for s in plan.stickies.values()]),
        "output": {"mode": "priority_id",
                   "rows_or_terms": [{"kind": o.kind, "ref": o.ref, "path_id": o.path_id,
                                      "reach": o.reach} for o in plan.output]},
        "cost": {"comparators": cnt["comparators"], "baseline_comparators": cnt["baseline_comparators"],
                 "ff": cnt["ffs"], "breakdown": {k: cnt[k] for k in
                  ("alive_ffs", "sticky_ffs", "carry_ffs", "and_units", "loops")}},
        "notes": plan.notes,
    }


def describe_schedule(sched: Sequence[int]) -> str:
    return "{" + compress_ranges(sched) + "}"
