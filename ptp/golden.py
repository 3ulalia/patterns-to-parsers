"""Golden evaluator.  A packet is a list of flits (ints of DW bits).

Semantics: FIRST ROW WINS.  The rows are tried in file order; the output is that of the first
row that matches (form A: its id, 0 for an id-0 row or no match; form B: that row's extraction
fields, zero-extended, all zero for no match).  Overlapping rows are never an error.
Only flits that arrived count (a constrained nibble / token in a flit that never arrived does
not match); flits >= T are ignored.

matched_rows() -> indices of ALL matching rows (file order); matched_keys() -> set of their
lookup groups (form A: ids incl. 0; form B: extraction signatures).
expected() -> (value, keys, defined):
  value   form A: the winning id (0 for no match); form B: tuple of field values in
          `field_order(rows)` (all zero for no match)
  keys    the groups of all matching rows (len > 1: a "co-match" packet, priority decides)
  defined False when the winning signature reads window bits of a flit that never arrived
          (the RTL holds stale data there, so the packet must not be used as a test vector).
"""
from typing import Dict, FrozenSet, List, Optional, Sequence

from .pir import Config, Row

_cache = {}


def field_order(rows: Sequence[Row]) -> List[str]:
    seen: List[str] = []
    for r in rows:
        for n, _m, _l in r.fields:
            if n not in seen:
                seen.append(n)
    return seen


def nibble(cfg: Config, packet: Sequence[int], n: int):
    """Nibble n of the packet, or None if its flit never arrived."""
    t, j = cfg.locate(n)
    if t >= len(packet):
        return None
    return (packet[t] >> (cfg.data_width - 4 - 4 * j)) & 0xF


def _assemble(cfg: Config, packet: Sequence[int]):
    N, npf = cfg.num_nibbles, cfg.nibbles_per_flit
    V = A = 0
    for n in range(min(N, len(packet) * npf)):
        sh = 4 * (N - 1 - n)
        V |= (nibble(cfg, packet, n)) << sh
        A |= 0xF << sh
    return V, A


def _compiled(rows: Sequence[Row], cfg: Config):
    key = id(rows)
    hit = _cache.get(key)
    if hit and hit[0] is rows and hit[1] is cfg:
        return hit[2]
    N = cfg.num_nibbles
    comp = []
    for r in rows:
        mask = val = 0
        for n, ch in enumerate(r.litpat):
            if ch != "x":
                sh = 4 * (N - 1 - n)
                mask |= 0xF << sh
                val |= int(ch, 16) << sh
        toks = []
        for st, t in r.toks:
            sh = 4 * (N - st - t.nibbles)
            toks.append((sh, (1 << t.bits) - 1, t))
        comp.append((mask, val, r.key, toks))
    _cache[key] = (rows, cfg, comp)
    return comp


def row_matches(cfg: Config, row: Row, packet: Sequence[int], ports: Optional[Dict[str, int]] = None) -> bool:
    V, A = _assemble(cfg, packet)
    N = cfg.num_nibbles
    for n, ch in enumerate(row.litpat):
        if ch != "x" and nibble(cfg, packet, n) != int(ch, 16):     # None (not arrived) != int
            return False
    for st, t in row.toks:
        sh = 4 * (N - st - t.nibbles)
        if ((A >> sh) & ((1 << t.bits) - 1)) != (1 << t.bits) - 1 or not t.matches((V >> sh) & ((1 << t.bits) - 1), ports):
            return False
    return True


def matched_rows(rows: Sequence[Row], cfg: Config, packet: Sequence[int], ports=None) -> List[int]:
    V, A = _assemble(cfg, packet)
    out = []
    for i, (m, v, k, toks) in enumerate(_compiled(rows, cfg)):
        if (m & ~A) or ((V ^ v) & m):
            continue
        ok = True
        for sh, tm, t in toks:
            if ((A >> sh) & tm) != tm or not t.matches((V >> sh) & tm, ports):
                ok = False
                break
        if ok:
            out.append(i)
    return out


def matched_keys(rows: Sequence[Row], cfg: Config, packet: Sequence[int], ports=None) -> FrozenSet:
    return frozenset(rows[i].key for i in matched_rows(rows, cfg, packet, ports))


matched_ids = matched_keys


def window(cfg: Config, packet: Sequence[int]) -> int:
    """Top max_width bits of the (zero padded) T-flit concatenation; bit max_width-1 = first wire bit."""
    DW, T = cfg.data_width, cfg.num_flits
    w = 0
    for t in range(T):
        w = (w << DW) | (packet[t] if t < len(packet) else 0)
    return w >> (DW * T - cfg.max_width)


def _sig_defined(cfg: Config, sig, npkt: int) -> bool:
    for _n, msb, lsb in sig:
        if (cfg.max_width - 1 - lsb) // cfg.data_width >= npkt:
            return False
    return True


def expected(rows: Sequence[Row], cfg: Config, packet: Sequence[int], ports=None):
    idx = matched_rows(rows, cfg, packet, ports)
    keys = frozenset(rows[i].key for i in idx)
    if cfg.form == "A":
        return (rows[idx[0]].id_value if idx else 0), keys, True
    order = field_order(rows)
    vals = {n: 0 for n in order}
    defined = True
    if idx:
        win = window(cfg, packet)
        sig = rows[idx[0]].key
        defined = _sig_defined(cfg, sig, len(packet))
        for n, msb, lsb in sig:
            vals[n] = (win >> lsb) & ((1 << (msb - lsb + 1)) - 1)
    return tuple(vals[n] for n in order), keys, defined


def evaluate(rows, cfg, packet, ports=None):
    return expected(rows, cfg, packet, ports)[0]
