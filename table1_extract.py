#!/usr/bin/env python3
"""
Aggregate Vivado synthesis results for Table 1 into a single CSV.

Usage:
    python3 table1_extract.py <table1_dir> <output_csv>

Each subdirectory of <table1_dir> must follow the naming convention:
    {parser}_dw{width}_{mode}
e.g., simple_dw256_tree, full_dw512_unique

Reports are expected at:
    <table1_dir>/<subdir>/vivado_proj/utilization.rpt
    <table1_dir>/<subdir>/vivado_proj/timing.rpt
"""

import sys
import re
import csv
from pathlib import Path

T_CLK_NS = 2.0  # 500 MHz target


def parse_wns(timing_file: Path):
    if not timing_file.exists():
        return None
    with timing_file.open() as f:
        for line in f:
            m = re.search(r'^\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+.*$', line)
            if m:
                return float(m.group(1))
    return None


def parse_util(util_file: Path):
    lut = None
    ff = None
    if not util_file.exists():
        return lut, ff
    with util_file.open() as f:
        for line in f:
            clean = line.strip().lstrip('|').strip()
            parts = [p.strip() for p in clean.split('|')]
            if len(parts) >= 2:
                name, value = parts[0], parts[1]
                try:
                    if name == "LUT as Logic":
                        lut = int(value)
                    elif name == "LUT as Memory":
                        lut = (lut or 0) + int(value)
                    elif name == "Register as Flip Flop":
                        ff = int(value)
                    elif name == "Register as Latch":
                        ff = (ff or 0) + int(value)
                except ValueError:
                    pass
    return lut, ff


def fmax_from_wns(wns):
    if wns is None:
        return None
    t_crit = T_CLK_NS - wns
    if t_crit <= 0:
        return None
    return round(1000.0 / t_crit)


# Canonical row order matching Table 1
TABLE1_ORDER = [
    ("simple", 256, "unique"),
    ("simple", 256, "tree"),
    ("simple", 320, "unique"),
    ("simple", 320, "tree"),
    ("full",   64,  "unique"),
    ("full",   64,  "tree"),
    ("full",   320, "unique"),
    ("full",   320, "tree"),
    ("full",   512, "unique"),
    ("full",   512, "tree"),
]


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <table1_dir> <output_csv>")
        sys.exit(1)

    table1_dir = Path(sys.argv[1])
    out_csv = Path(sys.argv[2])

    results = {}

    for subdir in sorted(table1_dir.iterdir()):
        if not subdir.is_dir():
            continue
        name = subdir.name  # e.g. simple_dw256_tree
        parts = name.split('_')
        if len(parts) != 3:
            continue
        parser, dw_str, mode = parts
        if not dw_str.startswith('dw'):
            continue
        try:
            dw = int(dw_str[2:])
        except ValueError:
            continue

        vivado_proj = subdir / "vivado_proj"
        wns = parse_wns(vivado_proj / "timing.rpt")
        lut, ff = parse_util(vivado_proj / "utilization.rpt")
        fmax = fmax_from_wns(wns)

        results[(parser, dw, mode)] = {
            "Parser": parser.capitalize() + " Parser",
            "DW (bits)": dw,
            "Config": mode.capitalize(),
            "Fmax (MHz)": fmax if fmax is not None else "N/A",
            "LUT": lut if lut is not None else "N/A",
            "FF": ff if ff is not None else "N/A",
        }

    rows = []
    for key in TABLE1_ORDER:
        if key in results:
            rows.append(results[key])
        else:
            parser, dw, mode = key
            rows.append({
                "Parser": parser.capitalize() + " Parser",
                "DW (bits)": dw,
                "Config": mode.capitalize(),
                "Fmax (MHz)": "N/A",
                "LUT": "N/A",
                "FF": "N/A",
            })

    with out_csv.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=["Parser", "DW (bits)", "Config", "Fmax (MHz)", "LUT", "FF"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Table 1 results written to {out_csv}")
    print()
    print(f"{'Parser':<20} {'DW':>6} {'Config':>8} {'Fmax(MHz)':>10} {'LUT':>8} {'FF':>8}")
    print("-" * 65)
    for row in rows:
        print(f"{row['Parser']:<20} {row['DW (bits)']:>6} {row['Config']:>8} "
              f"{str(row['Fmax (MHz)']):>10} {str(row['LUT']):>8} {str(row['FF']):>8}")


if __name__ == "__main__":
    main()
