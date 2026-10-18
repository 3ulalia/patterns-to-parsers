"""Analytical LUT estimator for ptp RTL.

python3 -m ptp_est.estimate <hex> <json> [--data-width N] [--char-per-col N] [--or-encode]
                             [--greedy|--exhaustive] [--dense-limit W] [--no-dense]

Three estimates are printed:
  algo1    paper Algorithm 1 on dense truth tables over the RAW input bits of each block
           (column encoder: 4*nibbles bits; lookup: sum of pseudo-ID bits), per output bit, no
           sharing, no wide-table bypass.  Blocks wider than --dense-limit are n/a (shown as 'ns'
           and replaced by the structural estimate in the '+struct' hybrid total).
  shared   4-bit-structured version: hash-consed multi-valued diagrams over nibble classes /
           pseudo-IDs with a recombination (mux) cost, identical sub-functions counted once across
           output bits, columns (shared nibble positions) and ids.
  noshare  same diagrams, every output bit / block costed independently (no sharing across bits,
           columns or ids; Algorithm 1's accounting).
  struct   trivial structural baseline.
"""
import argparse
import math
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE = os.path.dirname(_HERE)
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

from ptp import design as _design, pir as _pir          # read-only use
from . import dense as _dense, mdd as _mdd


def or_tree(n):
    return 0 if n <= 1 else -(-(n - 1) // 5)


def bits(n):
    return max(1, n.bit_length())


# ------------------------------------------------------------------ blocks
def idw_of(d):
    return max([r.id_width for r in d.rows] + [1])


def enc_cubes(d, c):
    """Column encoder: per output bit b, list of cubes {nibble pos i: value} (segment hits)."""
    seg_cubes = []
    for k, g in enumerate(c.segs, start=1):
        seg_cubes.append((k, {i: int(ch, 16) for i, ch in enumerate(g) if ch != "x"}))
    return [[cu for k, cu in seg_cubes if (k >> b) & 1] for b in range(c.width)]


def lookup_cubes(d):
    """Lookup: per id bit, list of cubes {col index: seg id} (one per row with that id bit)."""
    IDW = idw_of(d)
    per_bit = [[] for _ in range(IDW)]
    seen = set()
    for r, sig in zip(d.rows, d.row_sig):
        for b in range(IDW):
            if (r.id_value >> b) & 1 and (sig, b) not in seen:
                seen.add((sig, b))
                per_bit[b].append(dict(sig))
    return per_bit


def overhead(d):
    """flit counter + capture enables + clear + eop/valid pipeline (everything that is not
    comparators / encoders / lookup).  Simple constant model."""
    T = d.config.num_flits
    TW = bits(T)
    ts = {c.t for c in d.columns}
    counter = TW if T >= 2 else 1
    caps = sum(1 if TW <= 4 else 2 for _ in ts)
    clr = 1 if any(t > 0 for t in ts) else 0
    return counter + caps + clr + 1


# ------------------------------------------------------------------ structural baseline
def structural(d):
    npf = d.config.nibbles_per_flit
    enc = []
    for c in d.columns:
        t = 0
        for g in c.segs:
            n = sum(1 for ch in g if ch != "x")
            t += or_tree(n)
        for b in range(c.width):
            m = sum(1 for k in range(1, len(c.segs) + 1) if (k >> b) & 1)
            t += or_tree(m)
        enc.append(t)
    IDW = idw_of(d)
    rows_t, by_id, seen = 0, {}, {}
    for r, sig in zip(d.rows, d.row_sig):
        if sig not in seen:
            seen[sig] = r.index
            rows_t += or_tree(sum(d.columns[ci].width for ci, _ in sig))
        by_id.setdefault(r.id_value, set()).add(seen[sig])
    ids_t = sum(or_tree(len(v)) for k, v in by_id.items() if k)
    idc_t = sum(or_tree(sum(1 for k in by_id if k and (k >> b) & 1)) for b in range(IDW))
    return enc, rows_t + ids_t + idc_t


# ------------------------------------------------------------------ MDD (4-bit structured)
def _orders(varcount, cubes_all, exhaustive):
    nat = list(range(varcount))
    if not exhaustive:
        return [nat]
    freq = {}
    for cubes in cubes_all:
        for cu in cubes:
            for p in cu:
                freq[p] = freq.get(p, 0) + 1
    byf = sorted(nat, key=lambda p: (-freq.get(p, 0), p))
    return [nat, nat[::-1], byf, byf[::-1]]


def mdd_block(table_global, per_bit_cubes, varkeys, exhaustive):
    """Build the roots (one per output bit) in `table_global`.  With exhaustive, several variable
    orders are tried on a scratch table and the cheapest (standalone shared cost) is used."""
    nv = len(varkeys)
    orders = _orders(nv, per_bit_cubes, exhaustive)
    best = None
    for order in orders:
        if len(orders) > 1:
            t = _mdd.Table()
        else:
            t = table_global
        rank = {v: i for i, v in enumerate(order)}
        vi = [varkeys[v] for v in order]
        roots = [_mdd.build(t, [{rank[p]: val for p, val in cu.items()} for cu in cubes], nv, vi)
                 for cubes in per_bit_cubes]
        cost = t.shared_cost(roots)
        if best is None or cost < best[0]:
            best = (cost, order)
    order = best[1]
    rank = {v: i for i, v in enumerate(order)}
    vi = [varkeys[v] for v in order]
    return [_mdd.build(table_global, [{rank[p]: val for p, val in cu.items()} for cu in cubes], nv, vi)
            for cubes in per_bit_cubes]


def mdd_estimate(d, exhaustive=False):
    npf = d.config.nibbles_per_flit
    tg = _mdd.Table()
    blocks = []   # (name, roots, standalone_shared)
    for c in d.columns:
        cubes = enc_cubes(d, c)
        vk = [(("d", (c.start + i) % npf), 4) for i in range(c.nibbles)]
        # var keys carry (key, width)
        roots = mdd_block(tg, cubes, [(k, w) for (k, w) in vk], exhaustive)
        blocks.append((f"enc{c.index}", roots))
    cubes = lookup_cubes(d)
    vk = [(("p", c.index), c.width) for c in d.columns]
    roots = mdd_block(tg, cubes, vk, exhaustive)
    blocks.append(("lookup", roots))
    # accounting
    out = []
    seen = set()
    tot_shared = 0
    tot_noshare = 0
    for name, roots in blocks:
        alone = tg.shared_cost(roots)                 # sharing inside the block only
        ns = sum(tg.shared_cost([r]) for r in roots)  # every output bit independent (sharing only inside one bit's diagram)
        inc = tg.shared_cost(roots, seen)             # incremental under global sharing
        tot_shared += inc
        tot_noshare += ns
        out.append((name, ns, alone, inc))
    return out, tot_shared, tot_noshare


# ------------------------------------------------------------------ dense Algorithm 1
def _enc_dense(c, d, greedy, limit, seconds):
    w = 4 * c.nibbles
    if w > limit:
        return None
    cubes = enc_cubes(d, c)
    tabs = []
    for cs in cubes:
        masks = []
        for cu in cs:
            m = v = 0
            for i, val in cu.items():
                m |= 0xF << (4 * i)
                v |= val << (4 * i)
            masks.append((m, v))
        tabs.append(_dense.cube_table(w, masks))
    return _dense.count_table(tabs, w, not greedy, seconds)


def _lookup_dense(d, greedy, limit, seconds):
    off, w = {}, 0
    for c in d.columns:
        off[c.index] = w
        w += c.width
    if w > limit:
        return None
    tabs = []
    for cs in lookup_cubes(d):
        masks = []
        for cu in cs:
            m = v = 0
            for ci, sid in cu.items():
                m |= ((1 << d.columns[ci].width) - 1) << off[ci]
                v |= sid << off[ci]
            masks.append((m, v))
        tabs.append(_dense.cube_table(w, masks))
    return _dense.count_table(tabs, w, not greedy, seconds)


def algo1_estimate(d, struct_enc, struct_lookup, greedy=True, limit=20, seconds=120.0):
    per, hybrid, feasible = [], 0, True
    tot = 0
    for c, se in zip(d.columns, struct_enc):
        v = _enc_dense(c, d, greedy, limit, seconds)
        per.append((f"enc{c.index}", v))
        if v is None:
            feasible = False
            hybrid += se
        else:
            hybrid += v
            tot += v
    v = _lookup_dense(d, greedy, limit, seconds)
    per.append(("lookup", v))
    if v is None:
        feasible = False
        hybrid += struct_lookup
    else:
        hybrid += v
        tot += v
    return per, (tot if feasible else None), hybrid


# ------------------------------------------------------------------ top level
def estimate(hex_path, json_path=None, data_width=None, char_per_col=None, greedy=True,
             dense=True, dense_limit=20, exhaustive_orders=None, dense_seconds=120.0, verbose=False):
    rows, cfg = _pir.load_pir(hex_path, json_path, data_width, char_per_col)
    d = _design.build(rows, cfg, check_overlap=False)
    npf = cfg.nibbles_per_flit
    ov = overhead(d)
    timing = {}
    t0 = time.time()
    senc, slook = structural(d)
    s_tot = sum(senc) + slook + ov
    timing["struct"] = time.time() - t0
    t0 = time.time()
    ex = (not greedy) if exhaustive_orders is None else exhaustive_orders
    blocks, sh, ns = mdd_estimate(d, ex)
    timing["mdd"] = time.time() - t0
    res = dict(design=d, overhead=ov, struct=s_tot, struct_enc=senc, struct_lookup=slook,
               shared=sh + ov, noshare=ns + ov, blocks=blocks, timing=timing)
    if dense:
        t0 = time.time()
        per, tot, hyb = algo1_estimate(d, senc, slook, greedy, dense_limit, dense_seconds)
        timing["dense"] = time.time() - t0
        res.update(algo1=None if tot is None else tot + ov, algo1_hybrid=hyb + ov, algo1_blocks=per)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ptp_est.estimate")
    ap.add_argument("hex")
    ap.add_argument("json", nargs="?")
    ap.add_argument("--data-width", type=int)
    ap.add_argument("--char-per-col", type=int, dest="char_per_col")
    ap.add_argument("--or-encode", action="store_true",
                    help="accepted for symmetry with ptp.run; the boolean function is identical "
                         "under ID exclusivity so the estimate does not change")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--greedy", action="store_true", default=True)
    g.add_argument("--exhaustive", action="store_true")
    ap.add_argument("--dense-limit", type=int, default=20)
    ap.add_argument("--no-dense", action="store_true")
    ap.add_argument("--dense-seconds", type=float, default=120.0)
    a = ap.parse_args(argv)
    r = estimate(a.hex, a.json, a.data_width, a.char_per_col, not a.exhaustive,
                 not a.no_dense, a.dense_limit, None, a.dense_seconds)
    d = r["design"]
    print(f"rows={len(d.rows)} T={d.config.num_flits} DW={d.config.data_width} columns={len(d.columns)} "
          f"pid_bits={sum(c.width for c in d.columns)} comparators={len(d.comparators)}")
    a1 = {n: v for n, v in r.get("algo1_blocks", [])}
    print(f"{'block':<9}{'algo1':>7}{'noshare':>9}{'shared*':>9}{'incr':>7}{'struct':>8}")
    for (name, ns, alone, inc), i in zip(r["blocks"], range(len(r["blocks"]))):
        st = r["struct_enc"][i] if name != "lookup" else r["struct_lookup"]
        v = a1.get(name, "-")
        print(f"{name:<9}{'ns' if v is None else v:>7}{ns:>9}{alone:>9}{inc:>7}{st:>8}")
    print("(shared* = sharing within the block only; incr = extra LUTs under global sharing)")
    print(f"overhead (counter/caps/eop) = {r['overhead']}")
    print(f"TOTAL struct={r['struct']}  noshare={r['noshare']}  shared={r['shared']}", end="")
    if "algo1_hybrid" in r:
        print(f"  algo1={'n/a' if r['algo1'] is None else r['algo1']}  algo1+struct(hybrid)={r['algo1_hybrid']}", end="")
    print()
    print("runtime: " + " ".join(f"{k}={v:.2f}s" for k, v in r["timing"].items()))


if __name__ == "__main__":
    main()
