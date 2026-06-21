#!/usr/bin/env python3
"""
Aggregate synthesis results for Figures 6 and 7 into a CSV.

Usage:
    python3 fig_extract.py <fig_dir> <output_csv> --tool vivado|quartus

Subdirectory naming convention: {parser}_dw{width}_unique
e.g., simple_dw64_unique, full_dw512_unique

Vivado:  reads vivado_proj/utilization.rpt + timing.rpt  → columns: LUT, FF, Fmax
Quartus: reads quartus_proj/*.fit.rpt + *.sta.rpt         → columns: ALM, FF, Fmax
"""

import sys
import re
import csv
import argparse
from pathlib import Path

T_CLK_NS = 2.0  # 500 MHz target (Vivado only)

FIG_ORDER = [
    ("simple", 64),
    ("simple", 128),
    ("simple", 256),
    ("simple", 320),
    ("simple", 512),
    ("full",   64),
    ("full",   128),
    ("full",   256),
    ("full",   320),
    ("full",   512),
]


# ---------- Vivado parsers ----------

def vivado_wns(timing_file):
    if not timing_file.exists():
        return None
    with timing_file.open() as f:
        for line in f:
            m = re.search(r'^\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+.*$', line)
            if m:
                return float(m.group(1))
    return None


def vivado_util(util_file):
    lut = ff = None
    if not util_file.exists():
        return lut, ff
    with util_file.open() as f:
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
    return lut, ff


def vivado_fmax(wns):
    if wns is None:
        return None
    t_crit = T_CLK_NS - wns
    return round(1000.0 / t_crit) if t_crit > 0 else None


def extract_vivado(subdir):
    proj = subdir / "vivado_proj"
    wns = vivado_wns(proj / "timing.rpt")
    lut, ff = vivado_util(proj / "utilization.rpt")
    return {"LUT": lut, "FF": ff, "Fmax (MHz)": vivado_fmax(wns)}


# ---------- Quartus parsers ----------

def extract_quartus(subdir):
    proj = subdir / "quartus_proj"
    proj_name = subdir.name  # e.g. simple_dw64_unique
    fit_file = proj / f"{proj_name}_proj.fit.rpt"
    sta_file = proj / f"{proj_name}_proj.sta.rpt"

    alm = ff = fmax = None

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

    return {"ALM": alm, "FF": ff, "Fmax (MHz)": fmax}


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fig_dir", help="Directory containing figure RTL/synthesis subdirs")
    parser.add_argument("output_csv", help="Output CSV path")
    parser.add_argument("--tool", choices=["vivado", "quartus"], required=True)
    args = parser.parse_args()

    fig_dir = Path(args.fig_dir)
    out_csv = Path(args.output_csv)
    tool = args.tool

    resource_col = "LUT" if tool == "vivado" else "ALM"
    fieldnames = ["Parser", "DW (bits)", resource_col, "FF", "Fmax (MHz)"]

    results = {}
    for subdir in fig_dir.iterdir():
        if not subdir.is_dir():
            continue
        parts = subdir.name.split('_')
        if len(parts) != 3 or not parts[1].startswith('dw') or parts[2] != 'tree':
            continue
        try:
            dw = int(parts[1][2:])
        except ValueError:
            continue
        parser_name = parts[0]

        data = extract_vivado(subdir) if tool == "vivado" else extract_quartus(subdir)
        results[(parser_name, dw)] = data

    rows = []
    for (parser_name, dw) in FIG_ORDER:
        data = results.get((parser_name, dw), {resource_col: None, "FF": None, "Fmax (MHz)": None})
        rows.append({
            "Parser": parser_name.capitalize() + " Parser",
            "DW (bits)": dw,
            resource_col: data.get(resource_col, "N/A") if data.get(resource_col) is not None else "N/A",
            "FF": data.get("FF", "N/A") if data.get("FF") is not None else "N/A",
            "Fmax (MHz)": data.get("Fmax (MHz)", "N/A") if data.get("Fmax (MHz)") is not None else "N/A",
        })

    with out_csv.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Figure results ({tool}) written to {out_csv}")
    print()
    print(f"{'Parser':<20} {'DW':>6} {resource_col:>6} {'FF':>8} {'Fmax(MHz)':>10}")
    print("-" * 55)
    for row in rows:
        print(f"{row['Parser']:<20} {row['DW (bits)']:>6} {str(row[resource_col]):>6} "
              f"{str(row['FF']):>8} {str(row['Fmax (MHz)']):>10}")


if __name__ == "__main__":
    main()
