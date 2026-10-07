"""CLI: ``python3 -m pir_opt.show_atoms <hex> <json>`` (run from ``engine/``)."""
from __future__ import annotations

import argparse

from .atoms import atomize, format_all
from .pir import load_pir


def main(argv=None) -> int:
    ap_ = argparse.ArgumentParser(description="Print atoms, rows and families of a PIR.")
    ap_.add_argument("hex", help=".hex PIR file")
    ap_.add_argument("json", help="config JSON (data_width, max_width)")
    args = ap_.parse_args(argv)

    rows, cfg = load_pir(args.hex, args.json)
    ap = atomize(rows, cfg)
    n_ext = sum(r.is_extraction for r in rows)
    print(f"config: data_width={cfg.data_width} ({cfg.bytes_per_flit} B/flit)  "
          f"max_width={cfg.max_width} ({cfg.max_bytes} B)  flits T={cfg.num_flits}")
    print(f"rows: {len(rows)} total, {len(ap.rows)} matchable, {n_ext} extraction (skipped)")
    print(f"atoms: {len(ap.atoms)} unique   families: {len(ap.families)}\n")
    print(format_all(ap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
