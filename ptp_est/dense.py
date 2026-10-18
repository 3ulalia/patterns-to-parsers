"""Algorithm 1 (paper) on dense truth tables, exactly as published except that the
"w - X > 6 -> ceil(0.7 * 2^(w-X))" bypass is NOT used (infeasible tables are reported as None).

Count(f, greedy): d <= X -> [f non-const]; pick split variable (greedy = most agreeing cofactor
entries, else try all and take the min); equal cofactors -> Count(f0); else Count(f0)+Count(f1).
Top level is called with greedy=False (try every variable), recursion with greedy=True.
Implementation notes: constant f returns 0 early (identical result), results are memoised on the
table bytes (identical result).  `exhaustive=True` makes every level try-all (budgeted).
"""
import time
import numpy as np

X = 6


class Budget(Exception):
    pass


def cube_table(w, cubes):
    """cubes: list of (mask, val) over w bits (bit i of the index = variable i).  -> uint8[2^w]"""
    idx = np.arange(1 << w, dtype=np.uint32)
    f = np.zeros(1 << w, dtype=np.uint8)
    for m, v in cubes:
        f |= ((idx & np.uint32(m)) == np.uint32(v)).astype(np.uint8)
    return f


def count_bit(f, w, exhaustive=False, deadline=None):
    """f: uint8 array shaped (2,)*w (or flat, reshaped here)."""
    f = f.reshape((2,) * w) if w else f.reshape(())
    memo = {}

    def strip(f):
        i = 0
        while i < f.ndim:
            a, b = np.take(f, 0, i), np.take(f, 1, i)
            if np.array_equal(a, b):
                f = a  # irrelevant variable (greedy would pick it first, agreement = 100%)
            else:
                i += 1
        return f

    def rec(f, greedy):
        if deadline and time.time() > deadline:
            raise Budget()
        if not f.any():
            return 0
        if f.all():
            return 0
        d = f.ndim
        if d <= X:
            return 1
        key = (d, greedy, f.tobytes()) if f.size <= (1 << 14) else None
        if key is not None and key in memo:
            return memo[key]
        g = strip(f)
        if g.ndim < d:  # irrelevant variables were removed one by one (each is a recursion step)
            r = rec(g, True)
        else:
            cof = [(np.take(f, 0, i), np.take(f, 1, i)) for i in range(d)]
            if greedy and not exhaustive:
                i = max(range(d), key=lambda i: int((cof[i][0] == cof[i][1]).sum()))
                cands = [i]
            else:
                cands = range(d)
            r = None
            for i in cands:
                f0, f1 = cof[i]
                v = rec(f0, True) + rec(f1, True)
                if r is None or v < r:
                    r = v
        if key is not None:
            memo[key] = r
        return r

    f = np.ascontiguousarray(f)
    return rec(f, False)


def count_table(tables, w, exhaustive=False, seconds=120.0):
    """tables: list of flat uint8[2^w] (one per output bit) -> total LUTs, or None on budget."""
    deadline = time.time() + seconds
    try:
        return sum(count_bit(t, w, exhaustive, deadline) for t in tables)
    except Budget:
        return None
