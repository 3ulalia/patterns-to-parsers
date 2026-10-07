"""Lane value classes (formulation step 1).

For every byte lane, collect every ``(mask, value)`` predicate used at that lane in ANY flit (comparators are
shared across time) and partition the 256 byte values by their *predicate signature* (the set of predicates
that hold).  The partition cells are the lane's **classes**; they are the values of the multi-valued input
variable "byte at (t, lane)" -- for every ``t``.  Example (``mixed_hi_overlap`` lane 0, predicates ``68``,
``69``, ``6x``): classes ``{68}``, ``{69}``, ``{6x} minus 68,69``, and ``rest``.

Class 0 is always the all-false signature class: ``rest`` (some byte value matches no predicate) or, if every
byte value is covered by some predicate, a synthetic ``absent`` class with no values.  A byte past the end of
the packet is mapped to class 0 as well ("absent"): golden.py semantics are byte-granular and a constrained
byte at/after the end never matches, which is exactly "every predicate is false".  So short packets need NO
extra length variable: the valid-gated comparator outputs of an absent byte are all 0.

Indicator ``z(lane, c)`` (c >= 1) is a valid-gated comparator output "byte is in class c"; class 0 has no
indicator (it is the NOR of the others).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from ..atoms import AtomizedPir, cmp_suffix

Pred = Tuple[int, int]  # (mask, value)


@dataclass(frozen=True)
class ByteClass:
    cid: int
    sig: Tuple[int, ...]          # indices (into LaneClasses.preds) of predicates that hold
    values: Tuple[int, ...]       # byte values in the class (empty only for the synthetic absent class)
    label: str


@dataclass(frozen=True)
class LaneClasses:
    lane: int
    preds: Tuple[Pred, ...]
    classes: Tuple[ByteClass, ...]       # classes[0] = rest / absent
    class_of: Tuple[int, ...]            # 256 entries: byte value -> class id

    @property
    def n(self) -> int:
        return len(self.classes)

    @property
    def nbits(self) -> int:
        return (self.n - 1).bit_length()

    def pred_names(self) -> List[str]:
        return [cmp_suffix(m, v) for m, v in self.preds]


def _label(lane_preds: Sequence[Pred], sig: Tuple[int, ...], values: Tuple[int, ...]) -> str:
    if not sig:
        return "rest" if values else "absent"
    if len(values) == 1:
        return f"{values[0]:02x}"
    names = [cmp_suffix(*lane_preds[i]) for i in sig]
    full = set(range(256))
    for i in sig:
        m, v = lane_preds[i]
        full &= {b for b in range(256) if b & m == v}
    return "&".join(names) + ("" if set(values) == full else "*")   # '*': other predicates carved out


def lane_predicates(ap: AtomizedPir) -> Dict[int, List[Pred]]:
    """lane -> sorted distinct (mask, value) over all flits."""
    out: Dict[int, set] = {}
    for a in ap.atoms:
        out.setdefault(a.lane, set()).add((a.mask, a.value))
    return {l: sorted(s) for l, s in out.items()}


def build_lane_classes(ap: AtomizedPir) -> List[LaneClasses]:
    B = ap.config.bytes_per_flit
    lp = lane_predicates(ap)
    res: List[LaneClasses] = []
    for lane in range(B):
        preds = tuple(lp.get(lane, ()))
        by_sig: Dict[Tuple[int, ...], List[int]] = {}
        for b in range(256):
            sig = tuple(i for i, (m, v) in enumerate(preds) if b & m == v)
            by_sig.setdefault(sig, []).append(b)
        rest_vals = tuple(by_sig.pop((), ()))
        others = sorted(by_sig.items(), key=lambda kv: kv[1][0])
        cls = [ByteClass(0, (), rest_vals, _label(preds, (), rest_vals))]
        for sig, vals in others:
            cls.append(ByteClass(len(cls), sig, tuple(vals), _label(preds, sig, tuple(vals))))
        class_of = [0] * 256
        for c in cls:
            for b in c.values:
                class_of[b] = c.cid
        res.append(LaneClasses(lane, preds, tuple(cls), tuple(class_of)))
    return res


def atom_classes(lc: LaneClasses, mask: int, value: int) -> Tuple[int, ...]:
    """Class ids whose bytes satisfy predicate (mask, value) (class 0 never does)."""
    i = lc.preds.index((mask, value))
    return tuple(c.cid for c in lc.classes if i in c.sig)


def support_bits(values: Sequence[int]) -> int:
    """Number of byte bits the indicator 'byte in values' truly depends on."""
    s = set(values)
    n = 0
    for i in range(8):
        if any((b in s) != ((b ^ (1 << i)) in s) for b in range(256)):
            n += 1
    return n


def min_predicate_form(lc: LaneClasses, used: Sequence[int]) -> Tuple[Tuple[Pred, ...], int]:
    """Smallest set of the lane's predicates (tie: fewest mask bits) from which every used class indicator is a
    Boolean function.  Returns (predicates, total mask popcount)."""
    if not used:
        return (), 0
    np_ = len(lc.preds)
    best = None
    for r in range(np_ + 1):
        import itertools
        for sub in itertools.combinations(range(np_), r):
            seen: Dict[Tuple[bool, ...], Tuple[bool, ...]] = {}
            ok = True
            for b in range(256):
                key = tuple((b & lc.preds[i][0]) == lc.preds[i][1] for i in sub)
                val = tuple(lc.class_of[b] == c for c in used)
                if seen.setdefault(key, val) != val:
                    ok = False
                    break
            if ok:
                cost = sum(bin(lc.preds[i][0]).count("1") for i in sub)
                if best is None or cost < best[0]:
                    best = (cost, sub)
        if best is not None:
            break
    cost, sub = best
    return tuple(lc.preds[i] for i in sub), cost
