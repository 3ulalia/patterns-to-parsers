"""Generate the ``mixed_hi`` benchmark PIR (``mixed_hi.hex`` + ``mixed_hi.json``).

Rows, in priority order (first match wins):
  4'h1  EtherType 0800 @ bytes 12-13 AND IP proto 06 @ byte 23   (IPv4/TCP)
  4'h2  EtherType 0800 @ bytes 12-13 AND IP proto 11 @ byte 23   (IPv4/UDP)
  4'h3  EtherType 86dd @ bytes 12-13                              (IPv6)
  4'h4  ASCII "hi" starting at each byte offset k in [hi_start, hi_end]
        (one row per offset, same id)

Run from ``engine/``:  python3 -m pir_opt.bench.make_mixed_hi [--overlap] [--flit-width 64 ...]
"""
from __future__ import annotations

import argparse
import json
import os


def build_rows(max_bytes: int, hi_start: int, hi_end: int, proto_byte: int = 23,
               ethertype_byte: int = 12, overlap: bool = False) -> "list[tuple[str, str]]":
    def pat(fields: "dict[int, str]") -> str:
        nibbles = ["x"] * (2 * max_bytes)
        for pos, hexs in fields.items():
            for i, ch in enumerate(hexs):
                nibbles[2 * pos + i] = ch
        return "".join(nibbles)

    # --overlap: header rows get extra bytes inside the "hi" region (24..47) to stress sharing
    x1 = {34: "0050"} if overlap else {}                    # TCP dst port 80
    x2 = {36: "0035", 26: "68"} if overlap else {}          # UDP port 53 + a byte equal to 'h' at a hi lane
    x3 = {30: "69", 31: "68", 40: "6x"} if overlap else {}  # lanes 6/7 of flit 3 + nibble mask in flit 5
    rows = [
        (pat({ethertype_byte: "0800", proto_byte: "06", **x1}), "4'h1"),
        (pat({ethertype_byte: "0800", proto_byte: "11", **x2}), "4'h2"),
        (pat({ethertype_byte: "86dd", **x3}), "4'h3"),
    ]
    for k in range(hi_start, hi_end + 1):
        if k + 1 >= max_bytes:
            raise ValueError(f"'hi' at offset {k} does not fit in {max_bytes} bytes")
        rows.append((pat({k: "6869"}), "4'h4"))
    return rows


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--flit-width", type=int, default=64, help="data_width in bits")
    p.add_argument("--max-width", type=int, default=384, help="max_width in bits")
    p.add_argument("--hi-start", type=int, default=24, help="first byte offset of 'hi'")
    p.add_argument("--hi-end", type=int, default=46, help="last byte offset of 'hi' (inclusive)")
    p.add_argument("--out-dir", default=here)
    p.add_argument("--overlap", action="store_true",
                   help="header rows get extra bytes in the hi region (default name mixed_hi_overlap)")
    p.add_argument("--name", default=None)
    a = p.parse_args(argv)

    a.name = a.name or ("mixed_hi_overlap" if a.overlap else "mixed_hi")
    rows = build_rows(a.max_width // 8, a.hi_start, a.hi_end, overlap=a.overlap)
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.out_dir, a.name + ".hex"), "w") as f:
        for pattern, pid in rows:
            f.write(f"{pattern} {pid}\n")
    cfg = {"num_cols": 1, "char_per_col": a.max_width // 4, "col_per_file": 1,
           "data_width": a.flit_width, "max_width": a.max_width, "custom": []}
    with open(os.path.join(a.out_dir, a.name + ".json"), "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"wrote {len(rows)} rows to {a.out_dir}/{a.name}.hex")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
