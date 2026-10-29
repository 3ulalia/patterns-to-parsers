"""
HLS file emitter: generates Vitis C++ header and implementation files based on input Design.
"""

from . import tokens as tk
from .design import Design, has_token
from .golden import field_order


def idw_of(d: Design) -> int:
    return max([r.id_width for r in d.rows] + [1])


def out_width(d: Design) -> int:
    """Width of the packed expected value / output bus used by the testbench."""
    if d.form == "A":
        return idw_of(d)
    return sum(d.config.field_widths[n] for n in field_order(d.rows))


def pack_expected(d: Design, value) -> int:
    if d.form == "A":
        return value
    off, acc = 0, 0
    for n, v in zip(field_order(d.rows), value):
        acc |= v << off
        off += d.config.field_widths[n]
    return acc


def _bits(n: int) -> int:
    return max(1, n.bit_length())


def _or_vec(width, items):
    """items: [(id_value, wire)] -> '{b_{w-1}, ..., b_0}' where bit b = OR of wires whose id has bit b set."""
    bits = []
    for b in range(width - 1, -1, -1):
        ws = [wire for k, wire in items if (k >> b) & 1]
        # wrap long OR chains: Verilator rejects lines with > 40000 preprocessor tokens
        bits.append(
            " |\n\t\t".join(" | ".join(ws[i : i + 16]) for i in range(0, len(ws), 16))
            if ws
            else "1'b0"
        )
    return "{" + ", ".join(bits) + "}"


def _or_chain(ws):
    return " |\n\t".join(" | ".join(ws[i : i + 16]) for i in range(0, len(ws), 16))


def port_decls(d: Design):
    """Static configuration inputs of the port tokens: [(name, width)]."""
    return (
        sorted(tk.port_widths(d.config.tokens).items())
        if any(t.is_port for t in d.config.tokens.values())
        else []
    )


def _used_ports(d: Design):
    used = {}
    for _j, t in d.tok_comps:
        for n in t.port_names():
            used[n] = t.bits
    return sorted(used.items())


def _tok_expr(t: tk.Token, hi: int, lo: int) -> str:
    sl, B = f"data_in[{hi}:{lo}]", t.bits
    if t.is_port:
        p = t.port_names()
        if t.opcode == "RANGE":
            return f"({sl} >= {p[0]}) && ({sl} < {p[1]})"
        if t.opcode == "NE":
            return f"({sl} != {p[0]})"
        return " || ".join(f"({sl} == {n})" for n in p)
    v = t.values
    if t.opcode == "RANGE":
        return f"({sl} >= {B}'h{v[0]:X}) && ({sl} < {B}'h{v[1]:X})"
    if t.opcode == "NE":
        return f"({sl} != {B}'h{v[0]:X})"
    return " || ".join(f"({sl} == {B}'h{x:X})" for x in v)


# ------------------------------------------------------------------ core


def hls_tables(d: Design) -> dict:
    if len(d.tok_comps) != 0:
        raise NotImplementedError("we're getting to it")

    if d.form == "B":
        raise NotImplementedError("also getting to it")

    n_pid_bits = sum([c.width for c in d.columns])
    n_comparators = len(d.comparators) + len(d.tok_comps)
    data_width = d.config.data_width

    # determine the indices of the rows we need (non-live rows have -1 groups_of)
    need = [row.index for row in d.rows if d.group_of[row.index] >= 0]

    # uniquify need, then assemble the rows: every row we need directly and indirectly
    raw = sorted(set(need) | {e for row_index in need for e in d.masks[row_index]})

    master_dict = {
        "DATA_WIDTH": data_width,  # width of the data
        "N_FLITS": d.config.num_flits,  # number of flits per window
        "MAX_WIDTH": d.config.max_width,  # max data width
        "N_NIBBLE_COMPARATORS": len(
            d.comparators
        ),  # number of nibble-level comparators
        "N_TOKEN_COMPARATORS": len(d.tok_comps),  # number of token-level comparators
        "N_COMPARATORS": n_comparators,  # overall number of comparators
        "N_COLUMNS": len(d.columns),  # number of columns
        "N_SEGMENTS": sum(
            [len(col.segs) for col in d.columns]
        ),  # number of unique segments among all columns
        "N_ROWS": len(raw),  # number of rows for the lookup table
        "N_OUTPUTS": len(d.groups),  # number of distinct outputs
        "N_PID_BITS": n_pid_bits,  # number of pseudo-ID bits absorbed per packet
        "N_MASKS": sum(
            [len(d.masks[row]) for row in need]
        ),  # number of masks used to catch data
        "CMP_J": [
            j for j, _ in d.comparators
        ],  # indices to compare against per comparator
        "CMP_V": [
            v for _, v in d.comparators
        ],  # values to compare against per comparator
    }

    # ----- Write segment read_write values -----

    # lets us look up comparators by index
    comparator_index_table = {pair: index for index, pair in enumerate(d.comparators)}

    # lets us look up token comparators by index
    token_index_table = {
        (j, t.key()): len(d.comparators) + offset
        for offset, (j, t) in enumerate(d.tok_comps)
    }

    all_segment_terms = []
    segment_term_offsets = []
    segment_ids = []
    column_segment_offsets = []

    for col in d.columns:
        column_segment_offsets.append(len(segment_ids))

        for segment_index, seg in enumerate(col.segs, start=1):
            segment_term_offsets.append(len(all_segment_terms))
            segment_ids.append(segment_index)

            segment_tokens = (
                dict(tk.scan_tokens(seg, d.config.tokens)) if has_token(seg) else {}
            )

            # a segment hits when every comparator in terms is true for the current flit.
            terms = []

            index_into_segment = 0
            while index_into_segment < len(seg):
                # determine the in-flit nibble position of the next token
                token_position = (
                    col.start + index_into_segment
                ) % d.config.nibbles_per_flit

                # is the datum at this index something we want to compare against?
                if index_into_segment in segment_tokens:
                    terms.append(
                        token_index_table[
                            (
                                token_position,
                                segment_tokens[index_into_segment].key(),
                            )
                        ]
                    )
                    # shift the index along so we're looking at the next token
                    index_into_segment += segment_tokens[index_into_segment].nibbles
                    continue
                # we don't need to look at it as a token, but it might still hit a comparator
                if (
                    seg[index_into_segment] != "x"
                ):  # we only care if the token isn't a wildcard
                    terms.append(
                        comparator_index_table[
                            (token_position, int(seg[index_into_segment], 16))
                        ]
                    )
                index_into_segment += 1
            all_segment_terms.extend(terms)

    segment_term_offsets.append(len(all_segment_terms))
    column_segment_offsets.append(len(segment_ids))

    master_dict["N_SEGMENT_TERMS"] = len(all_segment_terms)  # number of segment terms
    master_dict["ALL_SEGMENT_TERMS"] = (
        all_segment_terms  # indices into the vector of comparators
    )
    master_dict["SEGMENT_TERM_OFFSETS"] = (
        segment_term_offsets  # segment s uses terms in range [STO[s], STO[s+1])
    )
    master_dict["SEGMENT_IDS"] = segment_ids  #  this is what the segment writes on hit
    master_dict["COLUMN_SEGMENT_OFFSETS"] = (
        column_segment_offsets  # column c has segments in range [CSO[c], CSO[c+1])
    )

    column_flit_indices = []
    column_psid_widths = []
    column_is_onehot = []
    column_psid_offsets = []

    current_offset = 0

    for column in d.columns:
        column_flit_indices.append(column.t)
        column_psid_widths.append(column.width)
        column_is_onehot.append(column.onehot)
        column_psid_offsets.append(current_offset)

        current_offset += column.width

    master_dict["COLUMN_FLIT_INDICES"] = (
        column_flit_indices  # column c is in flit CFI[c]
    )
    master_dict["COLUMN_PSID_WIDTHS"] = column_psid_widths  # widths of the pseudo-ids
    master_dict["COLUMN_IS_ONEHOT"] = (
        column_is_onehot  # column onehot encoding vector: list[bool]!
    )
    master_dict["COLUMN_PSID_OFFSETS"] = (
        column_psid_offsets  # packet has a vector of pseudo-IDs, we need to index into it to determine which pseudo-ID each column has
    )

    row_term_offsets = []
    row_term_columns = []
    row_term_segment_ids = []
    row_mask_offsets = []
    row_mask_indices = []

    row_raw_index_lookup = {
        row: row_index_in_raw for row_index_in_raw, row in enumerate(raw)
    }

    for raw_row_index in raw:
        row_term_offsets.append(len(row_term_columns))
        for column_index, segment_id in d.row_sig[raw_row_index]:
            row_term_columns.append(column_index)
            row_term_segment_ids.append(segment_id)
        row_mask_offsets.append(len(row_mask_indices))
        if d.group_of[raw_row_index] >= 0:  # row is live and needs its masks
            row_mask_indices.extend(
                [row_raw_index_lookup[mask_bit] for mask_bit in d.masks[raw_row_index]]
            )

    row_term_offsets.append(len(row_term_columns))
    row_mask_offsets.append(len(row_mask_indices))

    row_groups = [d.group_of[raw_row_index] for raw_row_index in raw]

    n_row_terms = sum([len(d.row_sig[raw_idx]) for raw_idx in raw])

    master_dict["ROW_GROUPS"] = row_groups  # a mapping between rows and their outputs
    master_dict["N_ROW_TERMS"] = (
        n_row_terms  # the total number of term pairs over raw rows
    )
    master_dict["ROW_TERM_OFFSETS"] = (
        row_term_offsets  # row r uses the terms in range [RTO[r], RTO[r+1])
    )
    master_dict["ROW_TERM_COLUMNS"] = (
        row_term_columns  # the column of each term in the row
    )
    master_dict["ROW_TERM_SEGMENT_IDS"] = (
        row_term_segment_ids  # the list of segment ids per term
    )
    master_dict["ROW_MASK_OFFSETS"] = (
        row_mask_offsets  # row r uses the masks in range [RMO[r], RMO[r+1])
    )
    master_dict["ROW_MASK_INDICES"] = (
        row_mask_indices  # list that lets us determine the winning row
    )

    if d.form == "A":
        master_dict["ID_WIDTH"] = idw_of(d)
        master_dict["GROUP_IDS"] = list(d.groups)

    return master_dict


def emit_hls_header(d: Design, path: str) -> None:
    """Emits a header file corresponding to the sizes and tables required for HLS synthesis, based
    on the design passed to it.


    There are a _lot_ of relevant datatypes, bit widths, and data structures defined in the PIR.
    Emitting a header file here means we can reference them easily in HLS.
    """

    parsed_design = hls_tables(d)
    max_data_size = max(
        [
            parsed_design["N_PID_BITS"],
            parsed_design["N_COMPARATORS"],
            parsed_design["DATA_WIDTH"],
        ]
    )

    pad = lambda s: f"{s} == 0 ? 1 : {s}"

    _ARRAYS = [  # (name, element type, C++ size expression)
        ("CMP_J", "int", pad("N_NIBBLE_COMPARATORS")),
        ("CMP_V", "int", pad("N_NIBBLE_COMPARATORS")),
        ("ALL_SEGMENT_TERMS", "int", pad("N_SEGMENT_TERMS")),
        ("SEGMENT_TERM_OFFSETS", "int", "N_SEGMENTS + 1"),
        ("SEGMENT_IDS", "int", pad("N_SEGMENTS")),
        ("COLUMN_SEGMENT_OFFSETS", "int", "N_COLUMNS + 1"),
        ("COLUMN_FLIT_INDICES", "int", pad("N_COLUMNS")),
        ("COLUMN_PSID_WIDTHS", "int", pad("N_COLUMNS")),
        ("COLUMN_IS_ONEHOT", "bool", pad("N_COLUMNS")),
        ("COLUMN_PSID_OFFSETS", "int", pad("N_COLUMNS")),
        ("ROW_GROUPS", "int", pad("N_ROWS")),
        ("ROW_TERM_OFFSETS", "int", "N_ROWS + 1"),
        ("ROW_TERM_COLUMNS", "int", pad("N_ROW_TERMS")),
        ("ROW_TERM_SEGMENT_IDS", "int", pad("N_ROW_TERMS")),
        ("ROW_MASK_OFFSETS", "int", "N_ROWS + 1"),
        ("ROW_MASK_INDICES", "int", pad("N_MASKS")),
        ("GROUP_IDS", "unsigned long long", pad("N_OUTPUTS")),
    ]

    _array_widths = [  # (name, element type, C++ value expression)
        ("CMP_W", "int", pad("N_COMPARATORS")),
        ("SEG_W", "int", pad("N_SEGMENTS")),
        ("PID_W", "int", pad("N_PID_BITS")),
        ("ROW_W", "int", pad("N_ROWS")),
        ("OUT_W", "int", pad("N_OUTPUTS")),
    ]

    _array_names = set(map(lambda t: t[0], _ARRAYS))

    # Vitis limit
    assert max_data_size <= 4096

    if d.form == "A":
        assert idw_of(d) <= 64

    with open(path, "w") as f:
        # ----- Write preprocessor commands, includes, etc -----

        f.write("#pragma once\n")

        if max_data_size > 1024:
            f.write(f"#define AP_INT_MAX_W {max_data_size}\n")

        f.write("#include <ap_int.h>\n")

        if len(d.tok_comps) != 0:
            pass
            # f.write("inline ap_uint<N_TOKEN_COMPARATORS> token_bits(const ap_uint<DATA_WIDTH>& flit, const ports_t& p);\n")

        w_c = lambda var, val: f.write(f"constexpr int {var} = {val};\n")

        def write_array(var, t, l, val):
            spelling_function = str
            match t:
                case "bool":
                    spelling_function = lambda x: str(x).lower()
                case "unsigned long long":
                    spelling_function = lambda x: f"{x}ULL"

            f.write(
                f"constexpr {t} {var}[{l}] = {{{','.join(map(spelling_function, val))}}};\n"
            )

        for variable, value in parsed_design.items():
            if variable not in _array_names:
                w_c(variable, value)

        for variable, _type, value in _array_widths:
            w_c(variable, value)

        for arr_var, arr_t, arr_size in _ARRAYS:
            write_array(arr_var, arr_t, arr_size, parsed_design[arr_var])
