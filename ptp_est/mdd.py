"""Multi-valued decision diagrams over "wide variables" (a 4-bit data nibble, or a latched
pseudo-ID of W bits), hash-consed so that identical sub-functions are one node.

A node = (var_key, width, default_child, ((value, child), ...)).  A variable only matters
through which of its *constrained* values it equals (or none -> default branch).  This is the
nibble-class compression: a nibble constrained to {3, 7} has 3 classes, not 16 values / 4 bits.

LUT cost model (X = 6 inputs), see local_cost():
  leaf (all children constant)      : function of the var's w bits alone
  internal, k distinct non-const    : the var's w bits + (6 - w) spare inputs per LUT carry the
      children signals; (nibble: w=4 -> 2 spare, a chain of max(1,k-1) LUTs).
"""
import sys
from bisect import bisect_left

sys.setrecursionlimit(100000)

X = 6


def leaf_cost(w):
    return 1 if w <= X else -(-(w - 1) // (X - 1))


def merge_cost(w, k, has_one):
    """LUTs to combine k distinct non-constant child signals under a w-bit variable."""
    s = X - w
    if k == 0:
        return leaf_cost(w)
    if s >= 2:
        return 1 if k <= s else 1 + -(-(k - s) // (s - 1))
    terms = k + (1 if has_one else 0)
    if s == 1:  # w == 5: one LUT per (decode & child) term, OR-ed
        return terms + (-(-(terms - 1) // (X - 1)) if terms > 1 else 0)
    dec = leaf_cost(w)  # w >= 6: decode each class, AND with child, OR
    n1 = -(-terms // 3)
    return terms * dec + n1 + (-(-(n1 - 1) // (X - 1)) if n1 > 1 else 0)


def or_tree_n(n):
    return 0 if n <= 1 else -(-(n - 1) // 5)


class Table:
    def __init__(self):
        self.nodes = {}      # key -> id
        self.info = [None, None]   # ids 0/1 are constants
        self._lc = {}
        self._al = {}

    def mk(self, var, w, default, items):
        items = tuple(sorted((v, c) for v, c in items if c != default))
        if not items:
            return default
        key = (var, w, default, items)
        i = self.nodes.get(key)
        if i is None:
            i = len(self.info)
            self.nodes[key] = i
            self.info.append(key)
        return i

    # ---- SOP-form nodes: ('lit', var, v) | ('cube', lits) | ('sop', cube_ids)
    def mk_lit(self, var, v):
        return self._mk(("lit", var, v))

    def _mk(self, key):
        i = self.nodes.get(key)
        if i is None:
            i = len(self.info)
            self.nodes[key] = i
            self.info.append(key)
        return i

    def mk_cube(self, lits):
        """lits: [(var, w, value)].  w==4 literals are shared nibble comparators (1 LUT each);
        pid literals (w<4) are decoded inside the consuming LUT."""
        lits = tuple(sorted(lits))
        return self._mk(("cube", lits))

    def mk_sop(self, cube_ids):
        cube_ids = tuple(sorted(set(cube_ids)))
        if len(cube_ids) == 1:
            return cube_ids[0]
        return self._mk(("sop", cube_ids))

    def kids(self, n):
        k = self.info[n]
        if k[0] == "lit":
            return []
        if k[0] == "cube":
            return [self.mk_lit(v, val) for v, w, val in k[1] if w >= 4]
        if k[0] == "sop":
            return list(k[1])
        _, _, d, items = k
        return list({d} | {c for _, c in items})

    def local_cost(self, n):
        c = self._lc.get(n)
        if c is None:
            k = self.info[n]
            if k[0] == "lit":
                c = 1
            elif k[0] == "cube":
                wide = [l for l in k[1] if l[1] >= 4]
                narrow = [l for l in k[1] if l[1] < 4]
                if wide:   # signals to merge: wide literals (LUT with 2 spare inputs) + narrow bits
                    sig = len(wide) - 1 + sum(l[1] for l in narrow)
                    c = -(-max(0, sig - 2) // 5)
                else:
                    tot = sum(l[1] for l in narrow)
                    c = -(-(tot - 1) // 5) if tot > 1 else 0
            elif k[0] == "sop":
                c = or_tree_n(len(k[1]))
            else:
                var, w, d, items = k
                ch = {d} | {c for _, c in items}
                kk = sum(1 for c in ch if c > 1)
                c = merge_cost(w, kk, 1 in ch)
            self._lc[n] = c
        return c

    def alone(self, n):
        """tree-ish standalone cost (shared children of one node merged); used to choose SOP vs Shannon."""
        if n <= 1:
            return 0
        v = self._al.get(n)
        if v is None:
            v = self.local_cost(n) + sum(self.alone(c) for c in self.kids(n))
            self._al[n] = v
        return v

    def shared_cost(self, roots, seen=None):
        """sum of local costs over distinct reachable nodes (identical sub-functions once)."""
        seen = set() if seen is None else seen
        tot, stack = 0, [r for r in roots if r > 1]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            tot += self.local_cost(n)
            for c in self.kids(n):
                if c > 1 and c not in seen:
                    stack.append(c)
        return tot


def build(table, cubes, nvars, varinfo, sop=True):
    """cubes: list of dict pos->value (pos indexes `varinfo` = [(key, width), ...] in the chosen
    variable order).  Returns the root node of OR of the cubes (an empty cube = const 1)."""
    pos_l = [sorted(c) for c in cubes]
    last = [p[-1] if p else -1 for p in pos_l]
    memo = {}

    def rec(S, pos):
        if not S:
            return 0
        for c in S:
            if last[c] < pos:
                return 1
        key = (S, pos)
        r = memo.get(key)
        if r is not None:
            return r
        vp = min(pos_l[c][bisect_left(pos_l[c], pos)] for c in S)
        by, U = {}, []
        for c in S:
            v = cubes[c].get(vp)
            if v is None:
                U.append(c)
            else:
                by.setdefault(v, []).append(c)
        U_t = tuple(U)
        dflt = rec(U_t, vp + 1)
        items = [(v, rec(tuple(sorted(cs + U)), vp + 1)) for v, cs in by.items()]
        k, w = varinfo[vp]
        r = table.mk(k, w, dflt, items)
        if sop:
            cs = [table.mk_cube([(varinfo[p][0], varinfo[p][1], v) for p, v in cubes[c].items() if p >= pos])
                  for c in S]
            rs = table.mk_sop(cs)
            if table.alone(rs) < table.alone(r):
                r = rs
        memo[key] = r
        return r

    return rec(tuple(range(len(cubes))), 0)
