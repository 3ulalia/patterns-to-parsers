"""Generate the synthetic PIR benches that the step-2 tests (tests/test_plan.py) build inline.

  abc_anywhere    8 B flits, 32 B window: row {3:aa, 12:bb, 21:cc} -> 4'h1 (anchored, spans 3 flits)
                  then "abc" at every offset 0..29 -> 4'h5
  non_contiguous  8 B flits, 16 B window: hi@2 (4'h4), byte 2 = 'h' (4'h7), hi@5, hi@9, hi@11 (4'h4):
                  a foreign id sits between the first and the later hi rows (priority split)
  abc_1byte       1 B flits, 16 B window: "abc" at every offset 0..13 -> 4'h2 (carry chains of depth 2)

Run from ``engine/``:  python3 -m pir_opt.bench.make_synthetic
"""
from __future__ import annotations

import json
import os


def pat(max_bytes: int, fields: "dict[int, str]") -> str:
    nib = ["x"] * (2 * max_bytes)
    for pos, hx in fields.items():
        for i, ch in enumerate(hx):
            nib[2 * pos + i] = ch
    return "".join(nib)


def benches():
    m = 32
    abc = [(pat(m, {3: "aa", 12: "bb", 21: "cc"}), "4'h1")] + [(pat(m, {k: "616263"}), "4'h5") for k in range(30)]
    m = 16
    nc = [(pat(m, {2: "6869"}), "4'h4"), (pat(m, {2: "68"}), "4'h7"), (pat(m, {5: "6869"}), "4'h4"),
          (pat(m, {9: "6869"}), "4'h4"), (pat(m, {11: "6869"}), "4'h4")]
    ab1 = [(pat(m, {k: "616263"}), "4'h2") for k in range(14)]
    return {"abc_anywhere": (64, 256, abc), "non_contiguous": (64, 128, nc), "abc_1byte": (8, 128, ab1)}


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    for name, (dw, mw, rows) in benches().items():
        with open(os.path.join(here, name + ".hex"), "w") as f:
            for p, i in rows:
                f.write(f"{p} {i}\n")
        with open(os.path.join(here, name + ".json"), "w") as f:
            json.dump({"num_cols": 1, "char_per_col": mw // 4, "col_per_file": 1,
                       "data_width": dw, "max_width": mw, "custom": []}, f, indent=2)
            f.write("\n")
        print(f"wrote {len(rows)} rows: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
