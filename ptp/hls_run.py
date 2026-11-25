"""
Runner for the HLS version of the project.
"""

import glob
import os
import shutil
import subprocess
import textwrap
import time

from .design import Design
from .hls_report import report

VITIS_RUN = os.environ.get("VITIS_RUN", "vitis-run")

TOP = "parser_top"
STAGE_FUNCTIONS = (
    "compare",
    "segment_hits",
    "capture",
    "row_matches",
    "winning_group",
    "group_to_id",
)
PIPELINE_STYLES = ("stp", "flp", "frp")
RESET_STYLES = ("none", "control", "state", "all")

OPTIONS = {
    "ii": "initiation interval of the per-flit pipeline (positive integer)",
    "pipeline_style": "stp | flp | frp (stall / flushable / free-running pipeline)",
    "latency": "maximum latency of parser_top, in cycles",
    "inline": "off: keep each kernel stage as its own RTL module",
    "balance": "on | off: expression balancing of the and/or trees",
    "reset": "none | control | state | all: which registers get a reset",
    "uncertainty": "clock uncertainty in ns (Vitis default: 27% of the period)",
}


def _positive_int(key, value):
    if not value.isdigit() or int(value) < 1:
        raise ValueError(f"{key} must be a positive integer, got '{value}'")
    return int(value)


def _choice(key, value, choices):
    if value not in choices:
        raise ValueError(f"{key} must be one of {', '.join(choices)}, got '{value}'")
    return value


def parse_options(text: str) -> dict:
    """'ii=2,balance=off' -> {'ii': 2, 'balance': 'off'}; see OPTIONS."""
    options = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        key, separator, value = item.partition("=")
        if not separator:
            raise ValueError(f"expected key=value, got '{item}'")
        if key not in OPTIONS:
            raise ValueError(
                f"unknown HLS option '{key}' (known: {', '.join(OPTIONS)})"
            )
        if key in ("ii", "latency"):
            options[key] = _positive_int(key, value)
        elif key == "pipeline_style":
            options[key] = _choice(key, value, PIPELINE_STYLES)
        elif key == "inline":
            options[key] = _choice(key, value, ("off",))
        elif key == "balance":
            options[key] = _choice(key, value, ("on", "off"))
        elif key == "reset":
            options[key] = _choice(key, value, RESET_STYLES)
        elif key == "uncertainty":
            try:
                options[key] = float(value)
            except ValueError:
                raise ValueError(f"uncertainty must be a number, got '{value}'")
    return options


def directives(options: dict):
    """(tcl_lines, cfg_lines) for the options; the two lists configure the same build."""
    # each entry: (directive, location, [(option, value)], [flags])
    entries = []
    pipeline_arguments = []
    if "ii" in options:
        pipeline_arguments.append(("II", options["ii"]))
    if "pipeline_style" in options:
        pipeline_arguments.append(("style", options["pipeline_style"]))
    if pipeline_arguments:
        entries.append(("pipeline", TOP, pipeline_arguments, []))
    if "latency" in options:
        entries.append(("latency", TOP, [("max", options["latency"])], []))
    if options.get("inline") == "off":
        for function in STAGE_FUNCTIONS:
            entries.append(("inline", function, [], ["off"]))
    if options.get("balance") == "off":
        entries.append(("expression_balance", TOP, [], ["off"]))
    elif options.get("balance") == "on":
        entries.append(("expression_balance", TOP, [], []))

    tcl_lines = []
    cfg_lines = []
    for directive, location, arguments, flags in entries:
        tcl_words = [f"set_directive_{directive}"]
        tcl_words += [f"-{name} {value}" for name, value in arguments]
        tcl_words += [f"-{flag}" for flag in flags]
        tcl_lines.append(" ".join(tcl_words + [location]))
        cfg_words = [location]
        cfg_words += [f"{name}={value}" for name, value in arguments]
        cfg_words += flags
        cfg_lines.append(f"syn.directive.{directive}={' '.join(cfg_words)}")

    if "reset" in options:
        tcl_lines.append(f"config_rtl -reset {options['reset']}")
        cfg_lines.append(f"syn.rtl.reset={options['reset']}")
    if "uncertainty" in options:
        tcl_lines.append(f"set_clock_uncertainty {options['uncertainty']}")
        cfg_lines.append(f"clock_uncertainty={options['uncertainty']}")
    return tcl_lines, cfg_lines


def write_project(
    d: Design,
    outdir: str,
    or_encode: bool = False,
    part="xcu280-fsvh2892-2L-e",
    clock_ns=10,
    options=None,
):
    for f in glob.glob(os.path.join(os.path.dirname(__file__), "hls", "*")):
        shutil.copy(f, outdir)

    if options is None:
        options = {}
    tcl_directives, cfg_directives = directives(options)
    with open(os.path.join(outdir, "directives.tcl"), "w") as f:
        f.write("".join(line + "\n" for line in tcl_directives))

    cflags = f"-std=c++17 -DPTP_OR_ENCODE={int(or_encode)}"
    with open(os.path.join(outdir, "run_hls.tcl"), "w") as f:
        f.write(f"""
        open_project -reset hls_proj
        open_solution -reset sol -flow_target vivado
        set_top parser_top
        add_files parser_top.cpp -cflags {{{cflags}}}
        add_files -tb tb.cpp     -cflags {{{cflags}}}
        add_files -tb {{vec_flits.hex vec_ctrl.hex vec_exp.hex}}

        set_part {{{part}}}
        create_clock -period {clock_ns} -name default
        source directives.tcl

        set step csim
        if {{[info exists ::env(PTP_STEP)]}} {{ set step $::env(PTP_STEP) }}
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
    with open(os.path.join(outdir, "hls_config.cfg"), "w") as f:
        f.write(
            textwrap.dedent(f"""
        part={part}

        [hls]
        clock={clock_ns}
        flow_target=vivado
        syn.file=parser_top.cpp
        syn.top=parser_top
        syn.cflags={cflags}
        tb.file=tb.cpp
        tb.file=vec_flits.hex
        tb.file=vec_ctrl.hex
        tb.file=vec_exp.hex
        tb.cflags={cflags}
        package.output.format=ip_catalog
        """).lstrip()
        )
        f.write("".join(line + "\n" for line in cfg_directives))


def run(outdir: str, step="csim"):
    steps = ("csim", "synth", "cosim", "impl")
    if step not in steps:
        raise ValueError(f"unknown step {step}")

    outdir = os.path.abspath(outdir)
    vitis_run = shutil.which(VITIS_RUN)
    if not vitis_run:
        return None, 0, 0, "vitis-run not found on PATH!", None
    start_time = time.time()
    result = subprocess.run(
        [vitis_run, "--mode", "hls", "--tcl", "run_hls.tcl"],
        cwd=outdir,
        env={**os.environ, "PTP_STEP": step},
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

    rep = report(outdir) if steps.index(step) > 0 else None

    return passed, mismatches, end_time - start_time, log, rep
