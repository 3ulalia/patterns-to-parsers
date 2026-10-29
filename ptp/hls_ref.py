"""
AI-generated (Claude, Anthropic); review before relying on it.

Table-level reference evaluator for the HLS backend: evaluates a packet using only the tables
returned by emit_hls.hls_tables(), with kernel K1 semantics (lookup = row compares, priority =
explicit masks). Tests compare it against golden.evaluate, so table bugs are caught without Vitis.
"""

from collections.abc import Sequence

from .design import Design
from .emit_hls import hls_tables


def _nibble(tables: dict, flit: int, nibble_position: int) -> int:
    # MSB-first, as golden.nibble / model._nib
    return (flit >> (tables["DATA_WIDTH"] - 4 - 4 * nibble_position)) & 0xF


def _comparator_bits(tables: dict, flit: int) -> list[bool]:
    return [
        _nibble(tables, flit, nibble_position) == nibble_value
        for nibble_position, nibble_value in zip(tables["CMP_J"], tables["CMP_V"])
    ]


def _segment_hits(tables: dict, comparator_bits: list[bool]) -> list[bool]:
    term_offsets = tables["SEGMENT_TERM_OFFSETS"]
    segment_terms = tables["ALL_SEGMENT_TERMS"]
    return [
        all(
            comparator_bits[comparator_index]
            for comparator_index in segment_terms[
                term_offsets[segment] : term_offsets[segment + 1]
            ]
        )
        for segment in range(tables["N_SEGMENTS"])
    ]


def _encode_column(tables: dict, column: int, segment_hits: list[bool]) -> int:
    """Pseudo-ID of one column from the segment hits of the current flit."""
    first_segment = tables["COLUMN_SEGMENT_OFFSETS"][column]
    end_segment = tables["COLUMN_SEGMENT_OFFSETS"][column + 1]
    hit_segment_ids = [
        tables["SEGMENT_IDS"][segment]
        for segment in range(first_segment, end_segment)
        if segment_hits[segment]
    ]

    if tables["COLUMN_IS_ONEHOT"][column]:
        pseudo_id = 0
        for segment_id in hit_segment_ids:
            pseudo_id |= 1 << (segment_id - 1)
        return pseudo_id

    # exact column: segments are pairwise conflicting, so at most one can hit
    assert len(hit_segment_ids) <= 1, f"column {column}: several segments hit"
    return hit_segment_ids[0] if hit_segment_ids else 0


def _field(tables: dict, packed_pseudo_ids: int, column: int) -> int:
    offset = tables["COLUMN_PSID_OFFSETS"][column]
    width = tables["COLUMN_PSID_WIDTHS"][column]
    return (packed_pseudo_ids >> offset) & ((1 << width) - 1)


def _term_holds(tables: dict, packed_pseudo_ids: int, term: int) -> bool:
    column = tables["ROW_TERM_COLUMNS"][term]
    segment_id = tables["ROW_TERM_SEGMENT_IDS"][term]
    pseudo_id = _field(tables, packed_pseudo_ids, column)

    if tables["COLUMN_IS_ONEHOT"][column]:
        return bool((pseudo_id >> (segment_id - 1)) & 1)
    return pseudo_id == segment_id


def evaluate(tables: dict, packet: Sequence[int]) -> int:
    """Form A id of the first matching row (0 = no match).

    `packet` is a list of DATA_WIDTH-bit flits in arrival order; its length is the packet length.
    Flits beyond N_FLITS are ignored, and columns whose flit never arrives keep pseudo-ID 0.
    """
    # ----- per flit: comparators -> segment hits -> capture the pseudo-IDs of this flit's columns
    packed_pseudo_ids = 0

    for flit_index, flit in enumerate(packet[: tables["N_FLITS"]]):
        segment_hits = _segment_hits(tables, _comparator_bits(tables, flit))

        for column in range(tables["N_COLUMNS"]):
            if tables["COLUMN_FLIT_INDICES"][column] == flit_index:
                pseudo_id = _encode_column(tables, column, segment_hits)
                packed_pseudo_ids |= pseudo_id << tables["COLUMN_PSID_OFFSETS"][column]

    # ----- end of packet: row match against the latched pseudo-IDs
    term_offsets = tables["ROW_TERM_OFFSETS"]
    row_matches = [
        all(
            _term_holds(tables, packed_pseudo_ids, term)
            for term in range(term_offsets[row], term_offsets[row + 1])
        )
        for row in range(tables["N_ROWS"])
    ]

    # ----- first row wins: a row loses if any of its masking (earlier) rows matched
    mask_offsets = tables["ROW_MASK_OFFSETS"]
    mask_indices = tables["ROW_MASK_INDICES"]
    winning_groups = set()

    for row in range(tables["N_ROWS"]):
        group = tables["ROW_GROUPS"][row]

        # mask-only rows never produce an output
        if group < 0 or not row_matches[row]:
            continue

        masking_rows = mask_indices[mask_offsets[row] : mask_offsets[row + 1]]
        if any(row_matches[masking_row] for masking_row in masking_rows):
            continue

        winning_groups.add(group)

    # ----- group -> output id
    assert len(winning_groups) <= 1, f"several groups won: {sorted(winning_groups)}"
    return tables["GROUP_IDS"][winning_groups.pop()] if winning_groups else 0


def evaluate_design(d: Design, packet: Sequence[int]) -> int:
    return evaluate(hls_tables(d), packet)
