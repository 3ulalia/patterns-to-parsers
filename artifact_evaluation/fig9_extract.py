#!/usr/bin/env python3
"""
Aggregate Vivado synthesis results for Figure 9 into a CSV.

Usage:
    python3 fig9_extract.py <fig9_dir> <output_csv>

Subdirectory naming convention: {config}_u{width}_ug{n}
  config : flat | decomposed
  width  : 256 | 512 | 1024
  n      : 1 | 2 | 3 | 4
"""

import sys
import re
import csv
from pathlib import Path

T_CLK_NS = 2.0

FIG9_ORDER = [
    ("flat",       256,  1), ("flat",       256,  2), ("flat",       256,  3), ("flat",       256,  4),
    ("flat",       512,  1), ("flat",       512,  2), ("flat",       512,  3), ("flat",       512,  4),
    ("flat",      1024,  1), ("flat",      1024,  2), ("flat",      1024,  3), ("flat",      1024,  4),
    ("decomposed", 256,  1), ("decomposed", 256,  2), ("decomposed", 256,  3), ("decomposed", 256,  4),
    ("decomposed", 512,  1), ("decomposed", 512,  2), ("decomposed", 512,  3), ("decomposed", 512,  4),
    ("decomposed",1024,  1), ("decomposed",1024,  2), ("decomposed",1024,  3), ("decomposed",1024,  4),
]


def parse_results(subdir):
    proj = subdir / "vivado_proj"
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


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <fig9_dir> <output_csv>")
        sys.exit(1)

    fig9_dir = Path(sys.argv[1])
    out_csv = Path(sys.argv[2])

    results = {}
    for subdir in fig9_dir.iterdir():
        if not subdir.is_dir():
            continue
        parts = subdir.name.split('_')
        # expect: config, u{width}, ug{n}
        if len(parts) != 3:
            continue
        config, width_str, ug_str = parts
        if config not in ("flat", "decomposed"):
            continue
        if not width_str.startswith('u') or not ug_str.startswith('ug'):
            continue
        try:
            width = int(width_str[1:])
            ug = int(ug_str[2:])
        except ValueError:
            continue

        fmax, lut, ff = parse_results(subdir)
        results[(config, width, ug)] = (fmax, lut, ff)

    def fmt(v):
        return v if v is not None else "N/A"

    rows = []
    for key in FIG9_ORDER:
        fmax, lut, ff = results.get(key, (None, None, None))
        config, width, ug = key
        rows.append({
            "Config": config,
            "Width (bits)": width,
            "UG": ug,
            "Fmax (MHz)": fmt(fmax),
            "LUT": fmt(lut),
            "FF": fmt(ff),
        })

    fieldnames = ["Config", "Width (bits)", "UG", "Fmax (MHz)", "LUT", "FF"]
    with out_csv.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Figure 9 results written to {out_csv}")
    print()
    print(f"{'Config':<12} {'Width':>6} {'UG':>4} {'Fmax(MHz)':>10} {'LUT':>8} {'FF':>8}")
    print("-" * 53)
    for row in rows:
        print(f"{row['Config']:<12} {row['Width (bits)']:>6} {row['UG']:>4} "
              f"{str(row['Fmax (MHz)']):>10} {str(row['LUT']):>8} {str(row['FF']):>8}")


if __name__ == "__main__":
    main()
