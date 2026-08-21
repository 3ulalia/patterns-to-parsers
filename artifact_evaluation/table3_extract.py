#!/usr/bin/env python3
"""
Aggregate Vivado and Quartus synthesis results for Table 3 into a single CSV.

Usage:
    python3 table3_extract.py <design_dir> <output_csv>

<design_dir> is the directory containing both vivado_proj/ and quartus_proj/.
e.g., p4_out/table3/snort_10rule
"""

import sys
import re
import csv
from pathlib import Path

T_CLK_NS = 2.0  # 500 MHz target (Vivado)


def vivado_results(design_dir):
    proj = design_dir / "vivado_proj"
    wns = lut = ff = fmax = None

    timing = proj / "timing.rpt"
    if timing.exists():
        with timing.open() as f:
            for line in f:
                m = re.search(r'^\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+.*$', line)
                if m:
                    wns = float(m.group(1))
                    break
    else:
        print(f"Warning: {timing} not found")

    util = proj / "utilization.rpt"
    if util.exists():
        with util.open() as f:
            for line in f:
                parts = [p.strip() for p in line.strip().lstrip('|').strip().split('|')]
                if len(parts) >= 2:
                    name, val = parts[0], parts[1]
                    try:
                        if name == "LUT as Logic":
                            lut = int(val)
                        elif name == "LUT as Memory":
                            lut = (lut or 0) + int(val)
                        elif name == "Register as Flip Flop":
                            ff = int(val)
                        elif name == "Register as Latch":
                            ff = (ff or 0) + int(val)
                    except ValueError:
                        pass
    else:
        print(f"Warning: {util} not found")

    if wns is not None:
        t_crit = T_CLK_NS - wns
        fmax = round(1000.0 / t_crit) if t_crit > 0 else None

    return fmax, lut, ff


def quartus_results(design_dir):
    proj = design_dir / "quartus_proj"
    proj_name = design_dir.name
    fit_file = proj / f"{proj_name}_proj.fit.rpt"
    sta_file = proj / f"{proj_name}_proj.sta.rpt"

    fmax = alm = ff = None

    if sta_file.exists():
        with sta_file.open(encoding='latin-1') as f:
            for line in f:
                m = re.match(r";\s*([\d.]+)\s*MHz\s*;", line.strip())
                if m:
                    fmax = float(m.group(1))
                    break
    else:
        print(f"Warning: {sta_file} not found")

    if fit_file.exists():
        with fit_file.open(encoding='latin-1') as f:
            for line in f:
                line = line.strip()
                m = re.match(r"; Logic utilization \(in ALMs\)\s*;\s*(\d+)\s*/", line)
                if m:
                    alm = int(m.group(1))
                m = re.match(r"; Dedicated logic registers\s*;\s*(\d+)", line)
                if m:
                    ff = int(m.group(1))
    else:
        print(f"Warning: {fit_file} not found")

    return fmax, alm, ff


def fmt(val):
    return val if val is not None else "N/A"


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <design_dir> <output_csv>")
        sys.exit(1)

    design_dir = Path(sys.argv[1])
    out_csv = Path(sys.argv[2])

    viv_fmax, viv_lut, viv_ff = vivado_results(design_dir)
    qua_fmax, qua_alm, qua_ff = quartus_results(design_dir)

    rows = [
        {"Tool": "Vivado (AMD)", "Fmax (MHz)": fmt(viv_fmax), "LUT": fmt(viv_lut), "ALM": "—", "FF": fmt(viv_ff)},
        {"Tool": "Quartus (Intel)", "Fmax (MHz)": fmt(qua_fmax), "LUT": "—", "ALM": fmt(qua_alm), "FF": fmt(qua_ff)},
    ]

    fieldnames = ["Tool", "Fmax (MHz)", "LUT", "ALM", "FF"]
    with out_csv.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Table 3 results written to {out_csv}")
    print()
    print(f"{'Tool':<20} {'Fmax(MHz)':>10} {'LUT':>8} {'ALM':>8} {'FF':>8}")
    print("-" * 58)
    for row in rows:
        print(f"{row['Tool']:<20} {str(row['Fmax (MHz)']):>10} {str(row['LUT']):>8} "
              f"{str(row['ALM']):>8} {str(row['FF']):>8}")


if __name__ == "__main__":
    main()
