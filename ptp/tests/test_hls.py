"""
AI-generated (Claude, Anthropic); review before relying on it.

Tests for the HLS backend.
Phase 1: hls_ref (evaluating hls_tables() only) must agree with golden on every packet, the tables
must be well-formed, and the emitted header must be valid C++.
Phase 2: generate(backend="hls") writes a complete project, the CLI and runner plumbing work without
Vitis (using a fake vitis-run), and csim agrees with the vectors (marked `hls`, needs vitis-run).
"""

import glob
import itertools
import os
import shutil
import stat
import subprocess

import pytest

from ptp import design, emit_hls, golden, hls_ref, hls_run, pir, run, vectors
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


def _unsupported_hex(json_path):
    base = json_path[: -len(".json")]
    return next(h for h in (base + ".hex", base + "_ids.hex") if os.path.exists(h))


@pytest.mark.parametrize("json_path", UNSUPPORTED, ids=os.path.basename)
def test_unsupported_raises(json_path):
    _rows, _cfg, d = _load(_unsupported_hex(json_path), json_path, None, None)
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


# ----- Phase 2: project generation (no Vitis needed)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLE_HEX = os.path.join(REPO, "examples", "02_pir_to_rtl", "headers.pir")
EXAMPLE_JSON = os.path.join(REPO, "examples", "02_pir_to_rtl", "headers.json")

PROJECT_FILES = [
    "ptp_design.h",
    "kernel.h",
    "parser_top.cpp",
    "tb.cpp",
    "run_hls.tcl",
    "vec_flits.hex",
    "vec_ctrl.hex",
    "vec_exp.hex",
]


def _braces_balanced(text):
    depth = 0
    for character in text:
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        if depth < 0:
            return False
    return depth == 0


@pytest.mark.parametrize(
    "hex_path,json_path,data_width,char_per_col", SUPPORTED, ids=SUPPORTED_IDS
)
def test_generate_hls(tmp_path, hex_path, json_path, data_width, char_per_col):
    _d, stats, _n_flits = run.generate(
        hex_path,
        json_path,
        str(tmp_path),
        data_width,
        packets=200,
        char_per_col=char_per_col,
        backend="hls",
    )
    assert stats["model_mismatches"] == 0
    assert stats["hls_mismatches"] == 0
    for name in PROJECT_FILES:
        assert (tmp_path / name).is_file(), name
    assert not (tmp_path / "parser.sv").exists()


@pytest.mark.parametrize("json_path", UNSUPPORTED, ids=os.path.basename)
def test_generate_hls_unsupported_writes_nothing(tmp_path, json_path):
    with pytest.raises(NotImplementedError):
        run.generate(
            _unsupported_hex(json_path), json_path, str(tmp_path), backend="hls"
        )
    assert list(tmp_path.iterdir()) == []


def test_generate_unknown_backend(tmp_path):
    hex_path, json_path, data_width, _char_per_col = ALL[0]
    with pytest.raises(ValueError):
        run.generate(hex_path, json_path, str(tmp_path), data_width, backend="verilog")


@pytest.mark.parametrize("or_encode", [False, True])
def test_run_tcl(tmp_path, or_encode):
    hex_path, json_path, data_width, char_per_col = ALL[0]
    run.generate(
        hex_path,
        json_path,
        str(tmp_path),
        data_width,
        packets=20,
        char_per_col=char_per_col,
        or_encode=or_encode,
        backend="hls",
        hls_part="xcvu9p-flga2104-2-i",
        hls_clock=2.5,
    )
    script = (tmp_path / "run_hls.tcl").read_text()
    assert _braces_balanced(script)
    assert script.count(f"-DPTP_OR_ENCODE={int(or_encode)} ") == 2
    assert "set_part {xcvu9p-flga2104-2-i}" in script
    assert "create_clock -period 2.5 " in script
    assert "set_top parser_top" in script
    assert "if {[info exists ::env(PTP_STEP)]} { set step $::env(PTP_STEP) }" in script


# ----- Phase 2: CLI and runner plumbing, with a fake vitis-run


def _fake_vitis_run(tmp_path, monkeypatch, output, exit_code):
    script = tmp_path / "fake-vitis-run"
    script.write_text(
        "#!/bin/sh\n"
        'echo "args: $*"\n'
        'echo "step: $PTP_STEP"\n'
        f"printf '%s\\n' '{output}'\n"
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(hls_run, "VITIS_RUN", str(script))


@pytest.mark.parametrize(
    "output,exit_code,passed,mismatches",
    [
        ("PASS", 0, True, 0),
        ("PASS", 1, False, 0),
        ("FAIL: 3 mismatches", 1, False, 3),
        ("MISMATCH on packet 0", 1, False, 1),
        ("no summary line", 0, False, 0),
    ],
)
def test_run_parses_log(tmp_path, monkeypatch, output, exit_code, passed, mismatches):
    _fake_vitis_run(tmp_path, monkeypatch, output, exit_code)
    result = hls_run.run(str(tmp_path), "synth")
    assert result[:2] == (passed, mismatches)
    log = result[3]
    assert "args: --mode hls --tcl run_hls.tcl" in log
    assert "step: synth" in log


def test_run_rejects_unknown_step(tmp_path):
    with pytest.raises(ValueError):
        hls_run.run(str(tmp_path), "bitstream")


def test_run_without_vitis(tmp_path, monkeypatch):
    monkeypatch.setattr(hls_run, "VITIS_RUN", "ptp-no-such-vitis-run")
    assert hls_run.run(str(tmp_path))[0] is None


def _cli(tmp_path, *extra):
    hex_path, json_path, _data_width, _char_per_col = SUPPORTED[0]
    return run.main(
        [
            hex_path,
            json_path,
            "-o",
            str(tmp_path / "out"),
            "--packets",
            "50",
            "--backend",
            "hls",
            *extra,
        ]
    )


def test_cli_hls(tmp_path, capsys):
    assert _cli(tmp_path, "--part", "xcvu9p-flga2104-2-i", "--clock", "4") == 0
    assert "hls_mismatches=0" in capsys.readouterr().out
    script = (tmp_path / "out" / "run_hls.tcl").read_text()
    assert "set_part {xcvu9p-flga2104-2-i}" in script
    assert "create_clock -period 4.0 " in script


def test_cli_hls_unsupported(tmp_path, capsys):
    json_path = UNSUPPORTED[0]
    code = run.main(
        [
            _unsupported_hex(json_path),
            json_path,
            "-o",
            str(tmp_path),
            "--backend",
            "hls",
        ]
    )
    assert code == 38
    assert "HLS ERROR" in capsys.readouterr().out


def test_cli_hls_sim_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hls_run, "VITIS_RUN", "ptp-no-such-vitis-run")
    assert _cli(tmp_path, "--sim") == 0
    assert "SIM SKIPPED" in capsys.readouterr().out


def test_cli_hls_sim_pass(tmp_path, monkeypatch, capsys):
    _fake_vitis_run(tmp_path, monkeypatch, "PASS", 0)
    assert _cli(tmp_path, "--sim") == 0
    assert "SIM PASS mismatches=0 build=n/a" in capsys.readouterr().out


def test_cli_hls_sim_fail(tmp_path, monkeypatch, capsys):
    _fake_vitis_run(tmp_path, monkeypatch, "FAIL: 2 mismatches", 1)
    assert _cli(tmp_path, "--sim", "--hls-run", "cosim") == 1
    out = capsys.readouterr().out
    assert "SIM FAIL mismatches=2 build=n/a" in out
    # the failing log is echoed, which shows the step reached the runner
    assert "step: cosim" in out


# ----- Phase 2: C simulation (needs vitis-run; select with `-m hls`)

HAVE_VITIS = bool(shutil.which(hls_run.VITIS_RUN))
CSIM_CASES = SUPPORTED + [(EXAMPLE_HEX, EXAMPLE_JSON, None, None)]
CSIM_IDS = SUPPORTED_IDS + ["example_02"]


def _csim(outdir, hex_path, json_path, data_width, char_per_col, or_encode=False):
    _d, stats, _n_flits = run.generate(
        hex_path,
        json_path,
        str(outdir),
        data_width,
        packets=300,
        char_per_col=char_per_col,
        or_encode=or_encode,
        backend="hls",
    )
    assert stats["hls_mismatches"] == 0
    return hls_run.run(str(outdir), "csim")


@pytest.mark.hls
@pytest.mark.skipif(not HAVE_VITIS, reason="vitis-run missing")
@pytest.mark.parametrize(
    "hex_path,json_path,data_width,char_per_col", CSIM_CASES, ids=CSIM_IDS
)
def test_csim(tmp_path, hex_path, json_path, data_width, char_per_col):
    passed, mismatches, _elapsed, log = _csim(
        tmp_path, hex_path, json_path, data_width, char_per_col
    )
    assert passed and mismatches == 0, log[-3000:]


@pytest.mark.hls
@pytest.mark.skipif(not HAVE_VITIS, reason="vitis-run missing")
def test_csim_or_encode(tmp_path):
    passed, mismatches, _elapsed, log = _csim(
        tmp_path, EXAMPLE_HEX, EXAMPLE_JSON, None, None, or_encode=True
    )
    assert passed and mismatches == 0, log[-3000:]


@pytest.mark.hls
@pytest.mark.skipif(not HAVE_VITIS, reason="vitis-run missing")
def test_csim_detects_bug(tmp_path):
    run.generate(EXAMPLE_HEX, EXAMPLE_JSON, str(tmp_path), packets=300, backend="hls")
    kernel = tmp_path / "kernel.h"
    source = kernel.read_text()
    correct = "out = GROUP_IDS[group_number];"
    assert correct in source
    kernel.write_text(source.replace(correct, "out = GROUP_IDS[group_number] + 1;"))
    passed, mismatches, _elapsed, _log = hls_run.run(str(tmp_path), "csim")
    assert not passed and mismatches > 0
