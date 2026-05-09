#!/usr/bin/env python3
import sys
import re
from pathlib import Path
import csv
import os

proj_dir = Path(sys.argv[1])
timing_file = proj_dir / "timing.rpt"
util_file = proj_dir / "utilization.rpt"

wns = None
lut = None
ff = None

# -------- Parse timing.rpt for WNS --------
if timing_file.exists():
    with timing_file.open() as f:
        for line in f:
            # Look for the WNS value in Design Timing Summary
            m = re.search(r'^\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+.*$', line)
            if m:
                wns = m.group(1)
                break
else:
    print(f"Warning: {timing_file} not found")


T_clk = 2.0  # clk is set to 500MHz (2ns)

fmax_mhz = None
if wns is not None:
    try:
        wns_val = float(wns)
        T_crit = T_clk - wns_val  # ns
        fmax_mhz = round(1000.0 / T_crit)
    except:
        pass

# -------- Parse utilization.rpt for LUT and FF --------
if util_file.exists():
    with util_file.open() as f:
        for line in f:
            # Remove leading/trailing spaces and '|'
            clean_line = line.strip().lstrip('|').strip()
            # Split by '|' and strip each field
            parts = [p.strip() for p in clean_line.split('|')]
            if len(parts) >= 2:
                name, value = parts[0], parts[1]
                if name == "LUT as Logic":
                    lut = int(value)
                elif name == "LUT as Memory":
                    lut = lut + int(value)
                elif name == "Register as Flip Flop":
                    ff = int(value)
                elif name == "Register as Latch":
                    ff = ff + int(value)
else:
    print(f"Warning: {util_file} not found")

# -------- Output results --------
#print(f"{proj_dir.parent.name}, WNS={wns}, LUT={lut}, FF={ff}")

# Optional: write to CSV
csv_file = Path("xilinx_resource_summary.csv")
write_header = not csv_file.exists() or os.stat(csv_file).st_size == 0
with open(csv_file, 'a', newline='') as csvf:
    writer = csv.writer(csvf)
    if write_header:
        writer.writerow(["Design", "Fmax(MHz)", "LUT", "FF"])
    writer.writerow([proj_dir.parent.name, fmax_mhz, lut, ff])

