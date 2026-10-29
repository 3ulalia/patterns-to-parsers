"""
AI-generated (Claude, Anthropic); review before relying on it.

Phase 1 tests for the HLS backend: hls_ref (evaluating hls_tables() only) must agree with golden on
every packet, the tables must be well-formed, and the emitted header must be valid C++.
"""

import glob
import itertools
import os
import shutil
import subprocess

import pytest

from ptp import design, emit_hls, golden, hls_ref, pir, vectors
from ptp.tests.test_ptp import ALL, DATA, IDS, REAL

SUPPORTED = ALL + [
    (hex_path, json_path, None, None) for _name, hex_path, json_path in REAL
]
SUPPORTED_IDS = IDS + [name for name, _hex, _json in REAL]

# designs with token comparators (and/or form B) are Phase 4
UNSUPPORTED = sorted(glob.glob(os.path.join(DATA, "tok_*.json")))


def _load(hex_path, json_path, data_width, char_per_col):
    rows, cfg = pir.load_pir(hex_path, json_path, data_width, char_per_col)
    return rows, cfg, design.build(rows, cfg)


def _inline(text, data_width, max_width):
    rows = pir.parse_rows(text.strip().splitlines())
    cfg = pir.Config(data_width, max_width)
    return rows, cfg, design.build(rows, cfg)


def _assert_parity(rows, cfg, d, packets):
    tables = emit_hls.hls_tables(d)
    for packet in packets:
        assert hls_ref.evaluate(tables, packet) == golden.evaluate(rows, cfg, packet), (
            packet
        )


# ----- parity with golden


@pytest.mark.parametrize(
    "hex_path,json_path,data_width,char_per_col", SUPPORTED, ids=SUPPORTED_IDS
)
def test_parity(hex_path, json_path, data_width, char_per_col):
    rows, cfg, d = _load(hex_path, json_path, data_width, char_per_col)
    packets, _dropped = vectors.make_packets(d, 300, seed=3)
    assert packets
    _assert_parity(rows, cfg, d, packets)


@pytest.mark.parametrize("json_path", UNSUPPORTED, ids=os.path.basename)
def test_unsupported_raises(json_path):
    base = json_path[: -len(".json")]
    hex_path = next(h for h in (base + ".hex", base + "_ids.hex") if os.path.exists(h))
    _rows, _cfg, d = _load(hex_path, json_path, None, None)
    with pytest.raises(NotImplementedError):
        emit_hls.hls_tables(d)


# ----- edge cases (these also exercise zero-size padding)


def test_no_masks():
    rows, cfg, d = _inline("12xxxxxx 2'd1\n34xxxxxx 2'd2\n", 32, 32)
    assert emit_hls.hls_tables(d)["N_MASKS"] == 0
    _assert_parity(rows, cfg, d, [[0x12000000], [0x34000000], [0x56000000]])


def test_all_wildcard_row():
    rows, cfg, d = _inline("xxxxxxxx 2'd1\n", 32, 32)
    tables = emit_hls.hls_tables(d)
    assert tables["N_COLUMNS"] == 0 and tables["N_ROW_TERMS"] == 0
    _assert_parity(rows, cfg, d, [[0], [0xFFFFFFFF]])


def test_wildcard_fallback_row():
    rows, cfg, d = _inline("12xxxxxx 2'd1\nxxxxxxxx 2'd2\n", 32, 32)
    _assert_parity(rows, cfg, d, [[0x12000000], [0x34000000]])


def test_only_id_zero_rows():
    rows, cfg, d = _inline("12xxxxxx 2'd0\n", 32, 32)
    assert emit_hls.hls_tables(d)["N_OUTPUTS"] == 0
    _assert_parity(rows, cfg, d, [[0x12000000], [0]])


def test_short_and_long_packets():
    rows, cfg, d = _inline(
        "xxxxxxxx12xxxxxx 3'd1\n" + "x" * 8 + "34xxxxxx 3'd2\n", 32, 64
    )
    _assert_parity(rows, cfg, d, [[0], [0, 0x12000000], [0, 0x34000000, 0x12000000]])


# ----- table shape


@pytest.mark.parametrize(
    "hex_path,json_path,data_width,char_per_col", SUPPORTED, ids=SUPPORTED_IDS
)
def test_table_shapes(hex_path, json_path, data_width, char_per_col):
    tables = emit_hls.hls_tables(
        _load(hex_path, json_path, data_width, char_per_col)[2]
    )

    lengths = {
        "CMP_J": "N_NIBBLE_COMPARATORS",
        "CMP_V": "N_NIBBLE_COMPARATORS",
        "ALL_SEGMENT_TERMS": "N_SEGMENT_TERMS",
        "SEGMENT_IDS": "N_SEGMENTS",
        "COLUMN_FLIT_INDICES": "N_COLUMNS",
        "COLUMN_PSID_WIDTHS": "N_COLUMNS",
        "COLUMN_IS_ONEHOT": "N_COLUMNS",
        "COLUMN_PSID_OFFSETS": "N_COLUMNS",
        "ROW_GROUPS": "N_ROWS",
        "ROW_TERM_COLUMNS": "N_ROW_TERMS",
        "ROW_TERM_SEGMENT_IDS": "N_ROW_TERMS",
        "ROW_MASK_INDICES": "N_MASKS",
        "GROUP_IDS": "N_OUTPUTS",
    }
    for array, count in lengths.items():
        assert len(tables[array]) == tables[count], array

    # offset arrays: count + 1 entries, non-decreasing, ending at the total they index into
    offsets = {
        "SEGMENT_TERM_OFFSETS": ("N_SEGMENTS", "N_SEGMENT_TERMS"),
        "COLUMN_SEGMENT_OFFSETS": ("N_COLUMNS", "N_SEGMENTS"),
        "ROW_TERM_OFFSETS": ("N_ROWS", "N_ROW_TERMS"),
        "ROW_MASK_OFFSETS": ("N_ROWS", "N_MASKS"),
    }
    for array, (count, total) in offsets.items():
        values = tables[array]
        assert len(values) == tables[count] + 1, array
        assert values[0] == 0 and values[-1] == tables[total], array
        assert all(a <= b for a, b in itertools.pairwise(values)), array

    # a row is only ever masked by an earlier row
    for row in range(tables["N_ROWS"]):
        start, end = (
            tables["ROW_MASK_OFFSETS"][row],
            tables["ROW_MASK_OFFSETS"][row + 1],
        )
        assert all(mask_row < row for mask_row in tables["ROW_MASK_INDICES"][start:end])

    assert sum(tables["COLUMN_PSID_WIDTHS"]) == tables["N_PID_BITS"]


# ----- header compiles (stub ap_int.h; the real one needs Vitis, Phase 2)

HAVE_GXX = bool(shutil.which("g++"))

HEADER_CASES = [
    (
        "full_parser",
        lambda: _load(
            os.path.join(DATA, "full_parser_ids.hex"),
            os.path.join(DATA, "full_parser.json"),
            None,
            None,
        ),
    ),
    ("all_wildcard", lambda: _inline("xxxxxxxx 2'd1\n", 32, 32)),
    ("only_id_zero", lambda: _inline("12xxxxxx 2'd0\n", 32, 32)),
]


@pytest.mark.skipif(not HAVE_GXX, reason="g++ missing")
@pytest.mark.parametrize(
    "name,make_design", HEADER_CASES, ids=[name for name, _make in HEADER_CASES]
)
def test_header_compiles(tmp_path, name, make_design):
    (tmp_path / "ap_int.h").write_text("template <int W> struct ap_uint {};\n")
    header = tmp_path / "ptp_design.h"
    emit_hls.emit_hls_header(make_design()[2], str(header))

    result = subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-fsyntax-only",
            "-pedantic-errors",
            "-Wno-pragma-once-outside-header",
            f"-I{tmp_path}",
            "-x",
            "c++",
            str(header),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
