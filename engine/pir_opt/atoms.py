"""Space-time atomization of PIR rows (byte granularity).

Coordinates: byte ``k`` of the packet lives at ``(t, lane) = (k // B, k % B)``
where ``B = data_width/8`` bytes per flit and ``t`` is the flit index.

Data model (all plain dataclasses, hashable where useful):

``Atom(t, lane, mask, value)``
    A pure predicate "(byte at (t, lane) & mask) == value". 8-bit mask/value,
    nibble-aware: pattern byte ``6x`` -> mask 0xF0, value 0x60. Atoms carry NO
    path id and are deduplicated across all rows (``AtomizedPir.atoms`` is the
    atom table; ``atom_id`` is the index into it, assigned in first-use order).

``AtomRow``
    One matchable PIR row = AND of atoms: sorted tuple of atom ids, plus
    ``path_id`` and ``priority`` (smaller = wins; equals the file-order index).
    Also carries its ``shape`` and ``anchor`` (below). Rows are byte-granular,
    so a pattern straddling a flit boundary simply has atoms at t and t+1.

``shape``
    The row's atoms re-based to its first constrained byte: a tuple of
    ``(byte_offset, mask, value)`` sorted by offset, flit-agnostic. ``anchor``
    is the absolute byte position of that first constrained byte.

``Family``
    Rows grouped by ``(path_id, shape)``; ``anchors`` is the sorted list of
    anchor byte positions. A "hi anywhere in bytes 24..47" rule is one family
    with 23 anchors, instead of 23 unrelated rows.

Only bytes with at least one constrained nibble yield atoms. Extraction rows
are not atomized. A fully-wildcard row has no atoms, shape ``()`` and
``anchor None`` (it matches every packet).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from .pir import PirConfig, PirRow

Shape = Tuple[Tuple[int, int, int], ...]  # ((byte_offset, mask, value), ...)


class Atom(NamedTuple):
    t: int      # flit index
    lane: int   # byte lane within flit
    mask: int   # 8-bit
    value: int  # 8-bit, value & ~mask == 0


@dataclass(frozen=True)
class AtomRow:
    index: int                 # PirRow.index (file order)
    path_id: int
    priority: int              # smaller wins (== file order)
    atom_ids: Tuple[int, ...]  # sorted
    shape: Shape
    anchor: Optional[int]      # absolute byte position of first constrained byte


@dataclass(frozen=True)
class Family:
    path_id: int
    shape: Shape
    anchors: Tuple[int, ...]   # sorted byte positions
    row_indices: Tuple[int, ...]


@dataclass
class AtomizedPir:
    config: PirConfig
    atoms: List[Atom] = field(default_factory=list)       # atom_id -> Atom
    rows: List[AtomRow] = field(default_factory=list)     # priority order
    families: List[Family] = field(default_factory=list)  # first-appearance order

    def atom_byte(self, atom_id: int) -> int:
        a = self.atoms[atom_id]
        return a.t * self.config.bytes_per_flit + a.lane


def pattern_to_bytes(pattern: str) -> List[Tuple[int, int, int]]:
    """Return ``[(byte_pos, mask, value)]`` for bytes with a constrained nibble."""
    if len(pattern) % 2:
        raise ValueError("pattern must have an even number of nibbles")
    out = []
    for k in range(len(pattern) // 2):
        hi, lo = pattern[2 * k], pattern[2 * k + 1]
        mask = value = 0
        if hi != "x":
            mask |= 0xF0
            value |= int(hi, 16) << 4
        if lo != "x":
            mask |= 0x0F
            value |= int(lo, 16)
        if mask:
            out.append((k, mask, value))
    return out


def atomize(rows: Sequence[PirRow], config: PirConfig) -> AtomizedPir:
    """Convert PIR rows to atoms, rows-as-atom-id-tuples and families."""
    B = config.bytes_per_flit
    ap = AtomizedPir(config=config)
    table: Dict[Atom, int] = {}
    fam_rows: Dict[Tuple[int, Shape], List[AtomRow]] = {}

    for row in rows:
        if row.is_extraction:
            continue
        bytes_ = pattern_to_bytes(row.pattern)
        ids = []
        for k, mask, value in bytes_:
            atom = Atom(k // B, k % B, mask, value)
            if atom not in table:
                table[atom] = len(ap.atoms)
                ap.atoms.append(atom)
            ids.append(table[atom])
        if bytes_:
            anchor: Optional[int] = bytes_[0][0]
            shape: Shape = tuple((k - anchor, m, v) for k, m, v in bytes_)
        else:
            anchor, shape = None, ()
        arow = AtomRow(row.index, row.id_value, row.index, tuple(sorted(ids)), shape, anchor)
        ap.rows.append(arow)
        fam_rows.setdefault((arow.path_id, shape), []).append(arow)

    for (path_id, shape), members in fam_rows.items():
        anchors = tuple(sorted(r.anchor for r in members if r.anchor is not None))
        ap.families.append(Family(path_id, shape, anchors, tuple(r.index for r in members)))
    return ap


def compress_ranges(values: Sequence[int]) -> str:
    """``[24,25,26,30]`` -> ``'24..26, 30'``."""
    vals = sorted(set(values))
    parts: List[str] = []
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[j] + 1:
            j += 1
        parts.append(str(vals[i]) if i == j else f"{vals[i]}..{vals[j]}")
        i = j + 1
    return ", ".join(parts)


def format_shape(shape: Shape) -> str:
    if not shape:
        return "(empty)"
    return " ".join(f"+{o}:{v:02x}/{m:02x}" for o, m, v in shape)


def format_atoms(ap: AtomizedPir) -> str:
    lines = ["atoms (id: t lane mask value):"]
    for i, a in enumerate(ap.atoms):
        lines.append(f"  a{i:<3} t={a.t} lane={a.lane} byte={ap.atom_byte(i):<3} "
                     f"mask={a.mask:02x} value={a.value:02x}")
    return "\n".join(lines)


def format_rows(ap: AtomizedPir) -> str:
    lines = ["rows (priority: path_id = AND of atoms):"]
    for r in ap.rows:
        ids = " ".join(f"a{i}" for i in r.atom_ids) or "(always)"
        lines.append(f"  r{r.index:<3} prio={r.priority:<3} id={r.path_id:<3} [{ids}]")
    return "\n".join(lines)


def format_families(ap: AtomizedPir) -> str:
    lines = ["families (path_id, shape -> anchor byte positions):"]
    for f in ap.families:
        lines.append(f"  id={f.path_id:<3} shape={format_shape(f.shape)}  "
                     f"anchors={compress_ranges(f.anchors) or '-'}  ({len(f.row_indices)} rows)")
    lines.append("  (shape: +byte_offset:value/mask, relative to first constrained byte)")
    return "\n".join(lines)


def format_all(ap: AtomizedPir) -> str:
    return "\n\n".join([format_atoms(ap), format_rows(ap), format_families(ap)])


def cmp_suffix(mask: int, value: int) -> str:
    """Structure-derived comparator suffix: ``68`` (full mask), ``6x`` / ``x5`` (nibble masks),
    ``<value>m<mask>`` for any other mask."""
    if mask == 0xFF:
        return f"{value:02x}"
    hi = f"{value >> 4:x}" if mask & 0xF0 == 0xF0 else ("x" if mask & 0xF0 == 0 else None)
    lo = f"{value & 15:x}" if mask & 0x0F == 0x0F else ("x" if mask & 0x0F == 0 else None)
    if hi is None or lo is None:
        return f"{value:02x}m{mask:02x}"
    return hi + lo
