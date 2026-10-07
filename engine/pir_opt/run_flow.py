"""CLI: ``python3 -m pir_opt.run_flow <hex> <json> [options]`` (run from ``engine/``).

Default output: brief atoms summary, baseline-vs-optimized summary table,
static per-cycle if/else for both, and an equivalence check of the plan model
and the baseline model against the golden evaluator (exit status 1 on any
mismatch).

``--static``   print the static if/else views (default on unless --packet is given)
``--expand``   do not group identical consecutive cycles
``--packet H`` dynamic per-cycle trace for the packet with hex bytes H
``--check N``  N seeded random packets (+ directed ones); 0 disables (default 3000)
``--seed S``   random seed (default 0)
``--plan-json P`` write the plan IR subset to P
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List

from .atoms import atomize, format_families
from .baseline import build_baseline
from .cycle_model import run_baseline, run_plan
from .golden import eval_pir
from .ifelse_dump import (format_dynamic, format_static_baseline, format_static_plan,
                          format_summary)
from .pir import load_pir
from .plan import build_plan, plan_to_json
from .vectors import bench_directed, directed_vectors, random_vectors


def check_equivalence(rows, cfg, ap, plan, bp, n_random: int, seed: int) -> dict:
    """Run plan model + baseline model vs golden. Returns counts and first failures."""
    vecs = bench_directed(cfg.max_bytes) + directed_vectors(ap, seed + 1) + random_vectors(ap, n_random, seed)
    res = {"packets": len(vecs), "plan_mismatch": 0, "baseline_mismatch": 0,
           "max_evals_per_unit": 0, "failures": [], "hist": {}}
    for p in vecs:
        g = eval_pir(rows, cfg, p)
        rp = run_plan(plan, p, trace=False)
        rb = run_baseline(bp, p, trace=False)
        res["max_evals_per_unit"] = max(res["max_evals_per_unit"], rp.max_evals_per_unit)
        res["hist"][g] = res["hist"].get(g, 0) + 1
        if rp.path_id != g:
            res["plan_mismatch"] += 1
        if rb.path_id != g:
            res["baseline_mismatch"] += 1
        if (rp.path_id != g or rb.path_id != g) and len(res["failures"]) < 5:
            res["failures"].append((p.hex(), g, rp.path_id, rb.path_id))
    return res


def main(argv: "List[str] | None" = None) -> int:
    ap_ = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap_.add_argument("hex")
    ap_.add_argument("json")
    ap_.add_argument("--static", action="store_true")
    ap_.add_argument("--expand", action="store_true")
    ap_.add_argument("--packet", default=None)
    ap_.add_argument("--check", type=int, default=3000)
    ap_.add_argument("--seed", type=int, default=0)
    ap_.add_argument("--plan-json", default=None)
    args = ap_.parse_args(argv)

    rows, cfg = load_pir(args.hex, args.json)
    ap = atomize(rows, cfg)
    plan = build_plan(ap, rows)
    bp = build_baseline(ap, rows)

    print(f"config: {cfg.data_width}-bit flits ({cfg.bytes_per_flit} B), window {cfg.max_bytes} B, "
          f"T={cfg.num_flits} flits; rows: {len(ap.rows)} matchable; atoms: {len(ap.atoms)}; "
          f"families: {len(ap.families)}")
    print(format_families(ap))
    print()
    print(format_summary(plan, bp))
    for n in plan.notes:
        print(f"note: {n}")
    print()

    if args.static or args.packet is None:
        print(format_static_baseline(bp, args.expand))
        print()
        print(format_static_plan(plan, args.expand))
        print()

    if args.packet is not None:
        pkt = bytes.fromhex(args.packet.replace(" ", ""))
        print(format_dynamic(plan, bp, rows, pkt))
        print()

    if args.plan_json:
        with open(args.plan_json, "w", encoding="utf-8") as f:
            json.dump(plan_to_json(plan), f, indent=2)
            f.write("\n")
        print(f"plan IR written to {args.plan_json}")

    rc = 0
    if args.check > 0:
        r = check_equivalence(rows, cfg, ap, plan, bp, args.check, args.seed)
        print(f"equivalence check: {r['packets']} packets (directed + {args.check} random, seed {args.seed}); "
              f"golden path_id histogram {dict(sorted(r['hist'].items()))}")
        print(f"  plan mismatches: {r['plan_mismatch']}   baseline mismatches: {r['baseline_mismatch']}   "
              f"max evals of any unit in one cycle: {r['max_evals_per_unit']}")
        for hx, g, a, b in r["failures"]:
            print(f"  FAIL golden={g} plan={a} baseline={b} packet={hx}")
        if r["plan_mismatch"] or r["baseline_mismatch"]:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
