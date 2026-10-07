"""Golden (reference) PIR evaluator.

Priority semantics (see NOTES.md for the evidence): FIRST ROW IN FILE ORDER
WINS. Every row whose constrained nibbles all match is a candidate; the
earliest candidate (lowest ``PirRow.index``) determines the output path id.
Extraction (form-B) rows never match. Output 0 means no match.

Packet semantics:
* Only the first ``max_width/8`` bytes are considered.
* A row that constrains any nibble at/after the end of the packet does NOT
  match (the flit never arrived, so its latch stays at reset value 0).
  A row with no constrained nibbles (all ``x``) matches any packet.
* Byte 0 is the first byte on the wire; pattern nibble ``2k``/``2k+1`` are the
  high/low nibbles of byte ``k``.

``eval_pir`` works directly on bytes; ``eval_pir_via_atoms`` evaluates the
atomized form (atoms.py) and must agree with it.
"""
from __future__ import annotations

from typing import Sequence

from .atoms import AtomizedPir
from .pir import PirConfig, PirRow


def row_matches(row: PirRow, window: bytes) -> bool:
    """True if ``row`` matches ``window`` (packet truncated to max_width)."""
    pat = row.pattern
    n = len(window)
    for nib, ch in enumerate(pat):
        if ch == "x":
            continue
        byte = nib >> 1
        if byte >= n:
            return False
        b = window[byte]
        got = (b >> 4) if nib % 2 == 0 else (b & 0xF)
        if got != int(ch, 16):
            return False
    return True


def eval_pir(rows: Sequence[PirRow], config: PirConfig, packet: bytes) -> int:
    window = bytes(packet[: config.max_bytes])
    for row in rows:
        if row.is_extraction:
            continue
        if row_matches(row, window):
            return row.id_value
    return 0


def eval_pir_via_atoms(ap: AtomizedPir, packet: bytes) -> int:
    """Cross-check evaluator working on atoms (space-time predicates)."""
    cfg = ap.config
    window = bytes(packet[: cfg.max_bytes])
    n = len(window)
    B = cfg.bytes_per_flit
    sat = set()
    for aid, atom in enumerate(ap.atoms):
        byte = atom.t * B + atom.lane
        if byte < n and (window[byte] & atom.mask) == atom.value:
            sat.add(aid)
    for row in ap.rows:  # already in priority order
        if all(a in sat for a in row.atom_ids):
            return row.path_id
    return 0
