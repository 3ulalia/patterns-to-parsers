"""
AI-generated (Claude, Anthropic); review before relying on it.

Tests for the out-of-context Vivado baseline (ptp/vivado_run.py) and its --vivado-run CLI path.
Vivado is replaced by a fake that writes the reports; the utilization report below is hand-written
in the report_utilization format, not taken from a real run.
"""

import stat

import pytest

from ptp import run, vivado_run
from ptp.tests.test_hls import SUPPORTED, _braces_balanced

UTILIZATION = """\
1. CLB Logic
------------
+----------------------------+------+-------+-----------+-------+
|          Site Type         | Used | Fixed | Available | Util% |
+----------------------------+------+-------+-----------+-------+
| CLB LUTs*                  | 6100 |     0 |   1303680 |  0.47 |
|   LUT as Logic             | 6000 |     0 |   1303680 |  0.46 |
|   LUT as Memory            |  100 |     0 |    600960 |  0.02 |
| CLB Registers              |  301 |     0 |   2607360 |  0.01 |
|   Register as Flip Flop    |  300 |     0 |   2607360 |  0.01 |
|   Register as Latch        |    1 |     0 |   2607360 |  0.00 |
| Block RAM Tile             |  1.5 |     0 |      2016 |  0.07 |
|   RAMB36/FIFO*             |    1 |     0 |      2016 |  0.05 |
|   RAMB18                   |    1 |     0 |      4032 |  0.02 |
| URAM                       |    0 |     0 |       960 |  0.00 |
| DSPs                       |    2 |     0 |      9024 |  0.02 |

2. Primitives
| LUT as Logic               | 9999 |     0 |   1303680 |  0.77 |
"""

EXPECTED_RESOURCES = {"LUT": 6100, "FF": 301, "BRAM_18K": 3, "URAM": 0, "DSP": 2}


def _write_results(directory, wns="0.512", step="impl"):
    (directory / "utilization.rpt").write_text(UTILIZATION)
    (directory / "vivado_results.txt").write_text(
        f"step {step}\npart xcu280-fsvh2892-2L-e\ntop parser\ntarget_clock 4.0\nwns {wns}\n"
    )


def _fake_vivado(tmp_path, monkeypatch, exit_code=0, write_reports=True):
    script = tmp_path / "fake-vivado"
    lines = ["#!/bin/sh", 'echo "args: $*"', 'echo "step: $PTP_STEP"']
    if write_reports:
        report = tmp_path / "utilization.template"
        report.write_text(UTILIZATION)
        lines += [
            f"cp {report} utilization.rpt",
            (
                "printf 'step %s\\npart p\\ntop parser\\ntarget_clock 4.0\\nwns -0.25\\n' "
                '"$PTP_STEP" > vivado_results.txt'
            ),
        ]
    lines.append(f"exit {exit_code}")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(vivado_run, "VIVADO", str(script))


def test_write_project(tmp_path):
    vivado_run.write_project(str(tmp_path), "parser_top", "xcvu9p-flga2104-2-i", 2.5)
    xdc = (tmp_path / "vivado.xdc").read_text()
    assert xdc == "create_clock -name clk -period 2.5 [get_ports clk]\n"
    script = (tmp_path / "run_vivado.tcl").read_text()
    assert _braces_balanced(script)
    assert (
        "synth_design -mode out_of_context -top parser_top -part {xcvu9p-flga2104-2-i}"
        in script
    )
    assert 'if {$source ne "parser_tb.sv"}' in script
    assert "read_xdc vivado.xdc" in script
    assert 'puts $handle "target_clock 2.5"' in script
    assert "if {[info exists ::env(PTP_STEP)]} { set step $::env(PTP_STEP) }" in script


def test_parse_utilization(tmp_path):
    path = tmp_path / "utilization.rpt"
    path.write_text(UTILIZATION)
    # only the first table counts, so the later "LUT as Logic" row is ignored
    assert vivado_run.parse_utilization(str(path)) == EXPECTED_RESOURCES


def test_parse_reports(tmp_path):
    _write_results(tmp_path)
    stats = vivado_run.parse_reports(str(tmp_path))
    assert stats["step"] == "impl"
    assert stats["top"] == "parser"
    assert stats["target_clock"] == 4.0
    assert stats["wns"] == 0.512
    assert stats["estimated_clock"] == 3.488
    assert stats["resources"] == EXPECTED_RESOURCES
    assert vivado_run.summary(stats) == (
        "VIVADO impl WNS=0.512 clock=3.488/4.0ns LUT=6100 FF=301 BRAM_18K=3 URAM=0 DSP=2"
    )


def test_parse_reports_no_timing_paths(tmp_path):
    _write_results(tmp_path, wns="")
    stats = vivado_run.parse_reports(str(tmp_path))
    assert stats["wns"] is None
    assert stats["estimated_clock"] is None


def test_parse_reports_missing(tmp_path):
    assert vivado_run.parse_reports(str(tmp_path)) is None
    (tmp_path / "vivado_results.txt").write_text("wns 0.1\n")
    assert vivado_run.parse_reports(str(tmp_path)) is None


@pytest.mark.parametrize("step", vivado_run.STEPS)
def test_run(tmp_path, monkeypatch, step):
    _fake_vivado(tmp_path, monkeypatch)
    outdir = tmp_path / "out"
    outdir.mkdir()
    passed, _elapsed, log, stats = vivado_run.run(str(outdir), step)
    assert passed
    assert "args: -mode batch -nojournal -nolog -source run_vivado.tcl" in log
    assert f"step: {step}" in log
    assert stats["step"] == step
    assert stats["estimated_clock"] == 4.25


def test_run_failure(tmp_path, monkeypatch):
    _fake_vivado(tmp_path, monkeypatch, exit_code=1)
    assert vivado_run.run(str(tmp_path))[0] is False


def test_run_without_reports(tmp_path, monkeypatch):
    _fake_vivado(tmp_path, monkeypatch, write_reports=False)
    passed, _elapsed, _log, stats = vivado_run.run(str(tmp_path))
    assert passed is False
    assert stats is None


def test_run_rejects_unknown_step(tmp_path):
    with pytest.raises(ValueError):
        vivado_run.run(str(tmp_path), "bitstream")


def test_run_without_vivado(tmp_path, monkeypatch):
    monkeypatch.setattr(vivado_run, "VIVADO", "ptp-no-such-vivado")
    assert vivado_run.run(str(tmp_path))[0] is None


# ----- CLI: --vivado-run runs after a passing --sim (Verilator is stubbed out)


def _cli(tmp_path, monkeypatch, simulation_passes=True, *extra):
    def fake_simulate(_outdir):
        if simulation_passes:
            return True, 0, 0.0, 0.0, "PASS"
        return False, 1, 0.0, 0.0, "FAIL: 1 mismatches"

    monkeypatch.setattr(run, "simulate", fake_simulate)
    hex_path, json_path, _data_width, _char_per_col = SUPPORTED[0]
    return run.main(
        [hex_path, json_path, "-o", str(tmp_path / "out"), "--packets", "50", "--sim"]
        + list(extra)
    )


def test_cli_writes_vivado_project(tmp_path, monkeypatch):
    assert (
        _cli(
            tmp_path, monkeypatch, True, "--part", "xcvu9p-flga2104-2-i", "--clock", "3"
        )
        == 0
    )
    script = (tmp_path / "out" / "run_vivado.tcl").read_text()
    assert "-top parser -part {xcvu9p-flga2104-2-i}" in script
    xdc = (tmp_path / "out" / "vivado.xdc").read_text()
    assert "-period 3.0 " in xdc


def test_cli_vivado_pass(tmp_path, monkeypatch, capsys):
    _fake_vivado(tmp_path, monkeypatch)
    assert _cli(tmp_path, monkeypatch, True, "--vivado-run", "synth") == 0
    out = capsys.readouterr().out
    assert "VIVADO synth WNS=-0.25 clock=4.25/4.0ns LUT=6100" in out


def test_cli_vivado_fail(tmp_path, monkeypatch, capsys):
    _fake_vivado(tmp_path, monkeypatch, exit_code=1)
    assert _cli(tmp_path, monkeypatch, True, "--vivado-run", "impl") == 1
    # the Vivado log is echoed, not the simulation log
    assert "step: impl" in capsys.readouterr().out


def test_cli_vivado_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vivado_run, "VIVADO", "ptp-no-such-vivado")
    assert _cli(tmp_path, monkeypatch, True, "--vivado-run", "synth") == 0
    assert "VIVADO SKIPPED" in capsys.readouterr().out


def test_cli_vivado_needs_passing_sim(tmp_path, monkeypatch, capsys):
    _fake_vivado(tmp_path, monkeypatch)
    assert _cli(tmp_path, monkeypatch, False, "--vivado-run", "synth") == 1
    assert "step: synth" not in capsys.readouterr().out
