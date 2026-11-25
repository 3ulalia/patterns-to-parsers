"""
AI-generated (Claude, Anthropic); review before relying on it.

Finds and parses the Vitis HLS csynth XML report for a generated project.
Works for both the classic Tcl project layout (hls_proj/sol/syn/report/)
and the Unified IDE component layout (<component>/hls/syn/report/).
"""

import glob
import os
from xml.etree import ElementTree

RESOURCES = ("LUT", "FF", "BRAM_18K", "URAM", "DSP")


def find_report(outdir: str, top: str = "parser_top"):
    """Newest <top>_csynth.xml under outdir (falls back to csynth.xml), or None."""
    for name in (f"{top}_csynth.xml", "csynth.xml"):
        pattern = os.path.join(outdir, "**", "syn", "report", name)
        candidates = glob.glob(pattern, recursive=True)
        if candidates:
            return max(candidates, key=os.path.getmtime)
    return None


def _number(text):
    """int or float from report text; None for missing/unbounded values ('undef', '?', '-')."""
    if text is None:
        return None
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def _find_number(root, path):
    return _number(root.findtext(path))


def parse_report(path: str) -> dict:
    root = ElementTree.parse(path).getroot()
    latency = "PerformanceEstimates/SummaryOfOverallLatency/"

    interval = _find_number(root, latency + "PipelineInitiationInterval")
    if interval is None:
        interval = _find_number(root, latency + "Interval-max")

    resources = {}
    available = {}
    for name in RESOURCES:
        resources[name] = _find_number(root, f"AreaEstimates/Resources/{name}")
        available[name] = _find_number(root, f"AreaEstimates/AvailableResources/{name}")

    protocols = {}
    for port in root.iter("RtlPorts"):
        port_object = port.findtext("Object")
        protocol = port.findtext("IOProtocol")
        if port_object and protocol:
            protocols.setdefault(port_object, set()).add(protocol)

    return {
        "report": path,
        "version": root.findtext("ReportVersion/Version"),
        "part": root.findtext("UserAssignments/Part"),
        "top": root.findtext("UserAssignments/TopModelName"),
        "target_clock": _find_number(root, "UserAssignments/TargetClockPeriod"),
        "clock_uncertainty": _find_number(root, "UserAssignments/ClockUncertainty"),
        "estimated_clock": _find_number(
            root,
            "PerformanceEstimates/SummaryOfTimingAnalysis/EstimatedClockPeriod",
        ),
        "pipeline_type": root.findtext("PerformanceEstimates/PipelineType"),
        "latency_min": _find_number(root, latency + "Best-caseLatency"),
        "latency_max": _find_number(root, latency + "Worst-caseLatency"),
        "interval": interval,
        "pipeline_depth": _find_number(root, latency + "PipelineDepth"),
        "resources": resources,
        "available": available,
        "protocols": protocols,
    }


def report(outdir: str, top: str = "parser_top"):
    """Parsed stats for the newest report under outdir, or None if synthesis hasn't run."""
    path = find_report(outdir, top)
    if path is None:
        return None
    return parse_report(path)


def summary(stats: dict) -> str:
    """One-line display form of report()."""
    resources = " ".join(
        f"{name}={value}"
        for name, value in stats["resources"].items()
        if value is not None
    )
    protocols = ",".join(sorted(set().union(*stats["protocols"].values())))
    return (
        f"SYNTH II={stats['interval']} latency={stats['latency_min']}-{stats['latency_max']} "
        f"clock={stats['estimated_clock']}/{stats['target_clock']}ns {resources} "
        f"interfaces={protocols}"
    )
