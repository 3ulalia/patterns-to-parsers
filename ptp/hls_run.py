"""
Runner for the HLS version of the project.
"""

import glob
import os
import shutil
import subprocess
import time

from .design import Design

VITIS_HLS = os.environ.get("VITIS_HLS", "vitis_hls")


def write_project(
    d: Design,
    outdir: str,
    or_encode: bool = False,
    part="xcu280-fsvh2892-2L-e",
    clock_ns=10,
):
    for f in glob.glob(os.path.join(os.path.dirname(__file__), "hls", "*")):
        shutil.copy(f, outdir)
    with open(os.path.join(outdir, "run_hls.tcl"), "w") as f:
        f.write(f"""
        open_project -reset hls_proj
        set_top parser_top
        add_files parser_top.cpp -cflags {{-std=c++17 -DPTP_OR_ENCODE={int(or_encode)} }}
        add_files -tb tb.cpp     -cflags {{-std=c++17 -DPTP_OR_ENCODE={int(or_encode)} }}
        add_files -tb {{vec_flits.hex vec_ctrl.hex vec_exp.hex}}

        open_solution -reset sol -flow_target vivado
        set_part {{{part}}}
        create_clock -period {clock_ns} -name default
        # source directives.tcl ;# not yet!

        set step [expr {{$argc > 0 ? [lindex $argv 0] : "csim"}}]
        if {{$step ni {{csim synth cosim impl}}}} {{
            puts "ERROR: unknown step '$step'"
            exit 1
        }}
        csim_design
        if {{$step in {{synth cosim impl}}}} {{ csynth_design }}
        if {{$step in {{cosim impl}}}} {{cosim_design}}
        if {{$step eq "impl"}} {{export_design -flow impl -rtl verilog}}
        exit
        """)


def run(outdir: str, step="csim"):
    if step not in ("csim", "synth", "cosim", "impl"):
        raise ValueError(f"unknown step {step}")

    outdir = os.path.abspath(outdir)
    vitis = shutil.which(VITIS_HLS)
    if not vitis:
        return None, 0, 0, "Vitis HLS not found on PATH!"
    start_time = time.time()
    result = subprocess.run(
        [vitis, "-f", "run_hls.tcl", "-tclargs", step],
        cwd=outdir,
        capture_output=True,
        text=True,
    )
    end_time = time.time()
    log = result.stdout + result.stderr
    mismatches = log.count("MISMATCH")
    for line in log.splitlines():
        if line.startswith("FAIL:"):
            try:
                mismatches = int(line.split()[1])
            except ValueError:
                pass
    passed = result.returncode == 0 and any(
        [line.startswith("PASS") for line in log.splitlines()]
    )
    return passed, mismatches, end_time - start_time, log
