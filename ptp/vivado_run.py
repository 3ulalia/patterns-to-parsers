"""
AI-generated (Claude, Anthropic); review before relying on it.

Out-of-context Vivado synthesis/implementation of the RTL backend, as the baseline for the HLS
backend.  Adapted from run_vivado.tcl / vivado_extract_resources.py on origin/ptp_v1, but in
non-project mode and with the part and clock written into the script, as hls_run does.
Results use the same keys as hls_report.parse_report, so the two backends compare directly.
"""

import os
import shutil
import subprocess
import time

VIVADO = os.environ.get("VIVADO", "vivado")
STEPS = ("synth", "impl")

# utilization.rpt row -> (resource, multiplier); rows that share a resource are summed
UTILIZATION_ROWS = {
    "LUT as Logic": ("LUT", 1),
    "LUT as Memory": ("LUT", 1),
    "Register as Flip Flop": ("FF", 1),
    "Register as Latch": ("FF", 1),
    "RAMB36/FIFO": ("BRAM_18K", 2),
    "RAMB18": ("BRAM_18K", 1),
    "URAM": ("URAM", 1),
    "DSPs": ("DSP", 1),
}


def write_project(outdir: str, top: str, part="xcu280-fsvh2892-2L-e", clock_ns=10):
    """Writes vivado.xdc and run_vivado.tcl into an outdir that holds the generated .sv files."""
    with open(os.path.join(outdir, "vivado.xdc"), "w") as f:
        f.write(f"create_clock -name clk -period {clock_ns} [get_ports clk]\n")
    with open(os.path.join(outdir, "run_vivado.tcl"), "w") as f:
        f.write(f"""\
set step synth
if {{[info exists ::env(PTP_STEP)]}} {{ set step $::env(PTP_STEP) }}
if {{$step ni {{synth impl}}}} {{
    puts "ERROR: unknown step '$step'"
    exit 1
}}

foreach source [lsort [glob *.sv]] {{
    if {{$source ne "parser_tb.sv"}} {{ read_verilog -sv $source }}
}}
read_xdc vivado.xdc
synth_design -mode out_of_context -top {top} -part {{{part}}}
if {{$step eq "impl"}} {{
    opt_design
    place_design
    route_design
}}

report_utilization -file utilization.rpt
report_timing_summary -file timing.rpt
set paths [get_timing_paths -delay_type max -max_paths 1 -nworst 1]
set wns ""
if {{[llength $paths] > 0}} {{ set wns [get_property SLACK [lindex $paths 0]] }}
set handle [open vivado_results.txt w]
puts $handle "step $step"
puts $handle "part {part}"
puts $handle "top {top}"
puts $handle "target_clock {clock_ns}"
puts $handle "wns $wns"
close $handle
exit
""")


def _number(text):
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def parse_utilization(path: str) -> dict:
    """First occurrence of each UTILIZATION_ROWS row in a report_utilization file."""
    resources = {}
    seen = set()
    with open(path) as f:
        for line in f:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            name = cells[0].rstrip("*").strip()
            if name not in UTILIZATION_ROWS or name in seen:
                continue
            value = _number(cells[1])
            if value is None:
                continue
            seen.add(name)
            resource, multiplier = UTILIZATION_ROWS[name]
            resources[resource] = resources.get(resource, 0) + value * multiplier
    return resources


def parse_reports(outdir: str):
    """Stats shaped like hls_report.parse_report, or None if Vivado hasn't run."""
    results_path = os.path.join(outdir, "vivado_results.txt")
    utilization_path = os.path.join(outdir, "utilization.rpt")
    if not os.path.exists(results_path) or not os.path.exists(utilization_path):
        return None
    values = {}
    with open(results_path) as f:
        for line in f:
            key, _separator, value = line.strip().partition(" ")
            values[key] = value.strip()

    target_clock = _number(values.get("target_clock", ""))
    wns = _number(values.get("wns", ""))
    estimated_clock = None
    if target_clock is not None and wns is not None:
        estimated_clock = round(target_clock - wns, 3)

    resources = {name: None for name in ("LUT", "FF", "BRAM_18K", "URAM", "DSP")}
    resources.update(parse_utilization(utilization_path))
    return {
        "report": utilization_path,
        "step": values.get("step"),
        "part": values.get("part"),
        "top": values.get("top"),
        "target_clock": target_clock,
        "wns": wns,
        "estimated_clock": estimated_clock,
        "resources": resources,
    }


def run(outdir: str, step="synth"):
    """(passed, elapsed_s, log, stats); passed is None when Vivado isn't on PATH."""
    if step not in STEPS:
        raise ValueError(f"unknown step {step}")
    outdir = os.path.abspath(outdir)
    vivado = shutil.which(VIVADO)
    if not vivado:
        return None, 0, "vivado not found on PATH!", None
    start_time = time.time()
    result = subprocess.run(
        [vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", "run_vivado.tcl"],
        check=False,
        cwd=outdir,
        env={**os.environ, "PTP_STEP": step},
        capture_output=True,
        text=True,
    )
    elapsed = time.time() - start_time
    stats = parse_reports(outdir)
    passed = result.returncode == 0 and stats is not None
    return passed, elapsed, result.stdout + result.stderr, stats


def summary(stats: dict) -> str:
    resources = " ".join(
        f"{name}={value}"
        for name, value in stats["resources"].items()
        if value is not None
    )
    return (
        f"VIVADO {stats['step']} WNS={stats['wns']} "
        f"clock={stats['estimated_clock']}/{stats['target_clock']}ns {resources}"
    )
