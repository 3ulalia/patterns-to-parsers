"""Model of today's PTP-style structure (the baseline to beat).

Structure (per packet, flit ``t`` = flit counter):

* In every flit ``t`` there is a comparator bank with ONE comparator per atom
  at that ``t`` -- i.e. one per ``(t, lane, mask, value)``. Nothing is shared
  across flits (the same ``lane==08`` check in flit 1 and in flit 3 is two
  comparators).
* Per row and per flit in which the row has atoms, a *partial-match latch*
  ``m_r<row>_t<t>`` captures ``AND(atoms of the row at t)`` in flit ``t``.
  Latches reset to 0 at SOP, so a flit that never arrives leaves its latch 0
  (this is the golden "short packet" rule).
* At EOP a priority if/else chain over rows (file order, first wins) tests
  ``AND(latches of the row)``. A row with no atoms always matches.

Everything is a dataclass; ``counts()`` gives the structural numbers.
The executable per-cycle model lives in ``cycle_model.run_baseline``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .atoms import AtomizedPir, cmp_suffix


@dataclass(frozen=True)
class BaselineCmp:
    id: str       # e.g. cmp_t1_l4_08
    atom_id: int
    t: int
    lane: int
    mask: int
    value: int


@dataclass(frozen=True)
class BaselineLatch:
    id: str                 # m_r<row>_t<t>
    row_index: int
    t: int
    inputs: Tuple[str, ...]  # comparator ids ANDed into this latch in flit t


@dataclass(frozen=True)
class BaselineRow:
    row_index: int
    path_id: int
    priority: int
    id_width: int
    latches: Tuple[str, ...]  # latch ids; empty => wildcard row (always matches)


@dataclass
class BaselinePlan:
    ap: AtomizedPir
    comparators: List[BaselineCmp]
    latches: List[BaselineLatch]
    rows: List[BaselineRow]  # priority order

    @property
    def config(self):
        return self.ap.config

    def cmps_at(self, t: int) -> List[BaselineCmp]:
        return [c for c in self.comparators if c.t == t]

    def latches_at(self, t: int) -> List[BaselineLatch]:
        return [l for l in self.latches if l.t == t]

    def counts(self) -> Dict[str, int]:
        latch_and = sum(len(l.inputs) for l in self.latches)
        eop_and = sum(len(r.latches) for r in self.rows)
        return {
            "comparators": len(self.comparators),
            "and_gates": sum(1 for l in self.latches if len(l.inputs) >= 2)
                         + sum(1 for r in self.rows if len(r.latches) >= 2),
            "and_inputs": latch_and + eop_and,
            "latch_and_inputs": latch_and,
            "eop_and_inputs": eop_and,
            "latches": len(self.latches),
            "ffs": len(self.latches),
            "evals_per_packet": len(self.comparators) + len(self.latches),
            "eop_chain": len(self.rows),
            "loops": 0,
        }


def _cmp_id(t: int, lane: int, mask: int, value: int) -> str:
    suffix = cmp_suffix(mask, value)
    return f"cmp_t{t}_l{lane}_{suffix}"


def build_baseline(ap: AtomizedPir, rows=None) -> BaselinePlan:
    """Build the PTP-style baseline structure from an atomized PIR.

    ``rows`` (optional list of ``PirRow``) only supplies the id literal widths.
    """
    widths = {r.index: r.id_width for r in rows} if rows else {}
    comps = [BaselineCmp(_cmp_id(a.t, a.lane, a.mask, a.value), i, a.t, a.lane, a.mask, a.value)
             for i, a in enumerate(ap.atoms)]
    cid = {c.atom_id: c.id for c in comps}
    latches: List[BaselineLatch] = []
    brows: List[BaselineRow] = []
    for r in ap.rows:
        by_t: Dict[int, List[int]] = {}
        for aid in r.atom_ids:
            by_t.setdefault(ap.atoms[aid].t, []).append(aid)
        lids = []
        for t in sorted(by_t):
            lat = BaselineLatch(f"m_r{r.index}_t{t}", r.index, t,
                                tuple(cid[a] for a in sorted(by_t[t], key=lambda a: ap.atoms[a].lane)))
            latches.append(lat)
            lids.append(lat.id)
        width = widths.get(r.index) or max(1, r.path_id.bit_length())
        brows.append(BaselineRow(r.index, r.path_id, r.priority, width, tuple(lids)))
    return BaselinePlan(ap, comps, latches, brows)
