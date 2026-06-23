#!/usr/bin/env python3
import sys
import re
from pathlib import Path
import csv
import os

proj_dir = Path(sys.argv[1])
proj_name = proj_dir.parent.name
fit_file = proj_dir / f"{proj_name}_proj.fit.rpt"
sta_file = proj_dir / f"{proj_name}_proj.sta.rpt"

fmax = None
alm = None
ff = None

# -------- Parse sta.rpt for Fmax --------
if sta_file.exists():
    """Extract Fmax from STA report"""
    with sta_file.open(encoding='latin-1') as f:
        for line in f:
            line = line.strip()
            match_fmax = re.match(r";\s*([\d.]+)\s*MHz\s*;", line)
            if match_fmax:
                fmax = float(match_fmax.group(1))
                break
else:
    print(f"Warning: {sta_file} not found")


# -------- Parse fit.rpt for ALM and FF --------
if fit_file.exists():
    """Extract ALM and FF from fit report"""
    with fit_file.open(encoding='latin-1') as f:
        for line in f:
            line = line.strip()
            # Match logic utilization (ALMs)
            match_alm = re.match(r"; Logic utilization \(in ALMs\)\s*;\s*([\d,]+)\s*/", line)
            if match_alm:
                alm = int(match_alm.group(1).replace(',', ''))
            # Match Total registers (FFs)
            match_ff = re.match(r"; Dedicated logic registers\s*;\s*([\d,]+)", line)
            if match_ff:
                ff = int(match_ff.group(1).replace(',', ''))
else:
    print(f"Warning: {fit_file} not found")

# -------- Output results --------
#print(f"{proj_dir.parent.name}, WNS={wns}, LUT={lut}, FF={ff}")

# Optional: write to CSV
csv_file = Path("quartus_resource_summary.csv")
write_header = not csv_file.exists() or os.stat(csv_file).st_size == 0
with open(csv_file, 'a', newline='') as csvf:
    writer = csv.writer(csvf)
    if write_header:
        writer.writerow(["Design", "Fmax(MHz)", "ALM", "FF"])
    writer.writerow([proj_dir.parent.name, fmax, alm, ff])

