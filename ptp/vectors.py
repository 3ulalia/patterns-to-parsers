"""Seeded packet generator.  A packet is a list of flit ints.

Semantics are FIRST ROW WINS, so co-matching packets are valid (and deliberately generated:
two rows with different ids/signatures that can overlap are planted into one packet).  A packet
is dropped only if the winning extraction signature (form B) reads a flit that never arrived.
"""
import random
from typing import Dict, List, Tuple

from . import tokens as tk
from .design import Design
from . import golden
from .golden import expected


# ------------------------------------------------------------------ ports / tokens
def choose_ports(d: Design, seed: int) -> Dict[str, int]:
    """Random static port-token values (RANGE ports get lo < hi)."""
    rng = random.Random(seed * 977 + 13)
    ports: Dict[str, int] = {}
    toks = {t.id: t for t in d.config.tokens.values() if t.is_port}
    for t in toks.values():
        b = t.bits
        if t.opcode == "RANGE":
            lo = rng.randrange(0, (1 << b) - 1)
            span = min((1 << b) - 1 - lo, 1 << max(1, b // 2))
            ports[t.port_names()[0]] = lo
            ports[t.port_names()[1]] = lo + 1 + rng.randrange(span)
        else:
            for n in t.port_names():
                ports[n] = rng.getrandbits(b)
    # port tokens of equal width are often given values inside another token's RANGE, so that
    # overlapping port rows (priority!) can really co-match
    for t in toks.values():
        if t.opcode == "RANGE":
            continue
        rngs = [(ports[u.port_names()[0]], ports[u.port_names()[1]]) for u in toks.values()
                if u.opcode == "RANGE" and u.bits == t.bits]
        for n in t.port_names():
            if rngs and rng.random() < 0.6:
                lo, hi = rng.choice(rngs)
                ports[n] = rng.randrange(lo, hi)
    return ports


def _values(t: tk.Token, ports):
    return tuple(ports[n] for n in t.port_names()) if t.is_port else t.values


def inside_outside(t: tk.Token, ports) -> Tuple[List[int], List[int]]:
    """Candidate values that match / do not match the token (edge cases first)."""
    v, top = _values(t, ports), (1 << t.bits) - 1
    if t.opcode == "RANGE":
        lo, hi = v
        ins = [lo, hi - 1, (lo + hi - 1) // 2]
        out = [x for x in (lo - 1, hi, 0, top) if 0 <= x <= top and not lo <= x < hi]
    elif t.opcode == "NE":
        c = v[0]
        ins = [x for x in (c ^ 1, (c + 1) & top, (c - 1) & top, 0, top) if x != c]
        out = [c]
    else:
        ins = list(v)
        s = set(v)
        out = [x for x in {(m + 1) & top for m in v} | {(m - 1) & top for m in v} if x not in s] or \
              [x for x in (0, top) if x not in s]
    return ins, out


def sample_token(t: tk.Token, rng: random.Random, ports, inside: bool) -> int:
    ins, out = inside_outside(t, ports)
    lst = ins if inside else out
    if not lst:
        return rng.getrandbits(t.bits)
    if rng.random() < 0.7:
        return rng.choice(lst)
    # random member / non-member
    for _ in range(8):
        x = rng.getrandbits(t.bits)
        if t.matches(x, ports) == inside:
            return x
    return rng.choice(lst)


# ------------------------------------------------------------------ packets
def _set_nibbles(cfg, pkt: List[int], n: int, val: int, count: int = 1) -> None:
    for k in range(count):
        t, j = cfg.locate(n + k)
        if t >= len(pkt):
            continue
        sh = cfg.data_width - 4 - 4 * j
        nib = (val >> (4 * (count - 1 - k))) & 0xF
        pkt[t] = (pkt[t] & ~(0xF << sh)) | (nib << sh)


def _rand_packet(d: Design, rng: random.Random, nflits: int, pools) -> List[int]:
    cfg = d.config
    pkt = [rng.getrandbits(cfg.data_width) for _ in range(nflits)]
    for n, pool in pools.items():
        if pool and rng.random() < 0.5:
            _set_nibbles(cfg, pkt, n, rng.choice(pool))
    return pkt


def _plant(d: Design, rng, row, nflits: int, pools, ports, tok_inside=True) -> List[int]:
    cfg = d.config
    pkt = _rand_packet(d, rng, nflits, pools)
    for n, ch in enumerate(row.litpat):
        if ch != "x":
            _set_nibbles(cfg, pkt, n, int(ch, 16))
    for st, t in row.toks:
        _set_nibbles(cfg, pkt, st, sample_token(t, rng, ports, tok_inside), t.nibbles)
    return pkt


def _row_flits(cfg, row) -> int:
    last = max([n for n, c in enumerate(row.pattern) if c != "x"], default=0)
    return cfg.locate(last)[0] + 1


def _pick_len(cfg, rng: random.Random, need: int = 1) -> int:
    T = cfg.num_flits
    r = rng.random()
    if r < 0.55:
        return max(need, T)
    if r < 0.70:
        return max(need, 1 + rng.randrange(T))
    if r < 0.80:
        return T + 1 + rng.randrange(3)
    return max(need, 1 + rng.randrange(T + 2))


def _overlap_pairs(d: Design, rng: random.Random, want: int):
    """Row pairs (a, b) with different ids/signatures that can statically co-match."""
    rows = d.rows
    out = []
    order = list(range(len(rows)))
    for _ in range(min(len(rows), 400)):
        if len(out) >= want:
            break
        a = rng.choice(order)
        cands = order if len(order) <= 3000 else rng.sample(order, 3000)
        hit = [b for b in cands if b != a and rows[b].key != rows[a].key and d.comatch(a, b)]
        if hit:
            out.append((a, rng.choice(hit)))
    return out


def _plant_pair(d: Design, rng, ra, rb, nflits: int, pools, ports):
    """A packet matching BOTH rows if possible (else a packet matching ra)."""
    cfg = d.config
    best = None
    for _ in range(8):
        pkt = _plant(d, rng, ra, nflits, pools, ports)
        for n, ch in enumerate(rb.litpat):
            if ch != "x" and (ra.litpat[n] == "x" or ra.litpat[n] == ch):
                _set_nibbles(cfg, pkt, n, int(ch, 16))
        for st, t in rb.toks:
            _set_nibbles(cfg, pkt, st, sample_token(t, rng, ports, True), t.nibbles)
            if ra.toks and not set(golden.matched_rows(d.rows, cfg, pkt, ports)) >= {ra.index, rb.index}:
                # same-span tokens of both rows: look for a value inside both sets
                cands = list(inside_outside(t, ports)[0]) + [v for _s, u in ra.toks if u.bits == t.bits
                                                             for v in inside_outside(u, ports)[0]]
                rng.shuffle(cands)
                for v in cands:
                    _set_nibbles(cfg, pkt, st, v, t.nibbles)
                    if set(golden.matched_rows(d.rows, cfg, pkt, ports)) >= {ra.index, rb.index}:
                        break
        got = golden.matched_rows(d.rows, cfg, pkt, ports)
        if ra.index in got and rb.index in got:
            return pkt
        best = best or pkt
    return best


def _candidates(d: Design, n: int, seed: int, ports) -> List[List[int]]:
    cfg = d.config
    rng = random.Random(seed)
    pools = {}
    for r in d.rows:
        for k, ch in enumerate(r.litpat):
            if ch != "x":
                pools.setdefault(k, set()).add(int(ch, 16))
    pools = {k: sorted(v) for k, v in pools.items()}
    pkts: List[List[int]] = []
    rows = d.rows
    first = {}
    for r in rows:
        first.setdefault(r.key, r)
    directed = list(first.values())
    rest = [r for r in rows if r not in directed]
    budget = max(0, n // 2 - len(directed))
    rng.shuffle(rest)
    directed += rest[:budget]
    for r in directed:
        need = _row_flits(cfg, r)
        pkts.append(_plant(d, rng, r, rng.choice([need, need, _pick_len(cfg, rng, need)]), pools, ports))
    # deliberate co-matches: two overlapping rows of different ids/signatures in one packet
    for ra, rb in _overlap_pairs(d, rng, max(8, n // 4)):
        ra, rb = d.rows[ra], d.rows[rb]
        if rng.random() < 0.5:
            ra, rb = rb, ra
        need = max(_row_flits(cfg, ra), _row_flits(cfg, rb))
        pkts.append(_plant_pair(d, rng, ra, rb, rng.choice([need, need, _pick_len(cfg, rng, need)]), pools, ports))
    # token edge cases: every token of (some) rows, inside and outside the value set
    trows = [r for r in rows if r.toks]
    rng.shuffle(trows)
    for r in trows[: max(0, n // 4)]:
        need = _row_flits(cfg, r)
        for inside in (True, False):
            pkts.append(_plant(d, rng, r, rng.choice([need, _pick_len(cfg, rng, need)]), pools, ports, inside))
    # near misses: one constrained nibble changed / a token pushed out of its set / row truncated
    for _ in range(max(0, n // 4)):
        r = rng.choice(rows)
        cons = [k for k, c in enumerate(r.litpat) if c != "x"]
        need = _row_flits(cfg, r)
        pkt = _plant(d, rng, r, _pick_len(cfg, rng, need), pools, ports)
        x = rng.random()
        if r.toks and (not cons or x < 0.4):
            st, t = rng.choice(r.toks)
            _set_nibbles(cfg, pkt, st, sample_token(t, rng, ports, False), t.nibbles)
        elif cons and x < 0.8:
            k = rng.choice(cons)
            _set_nibbles(cfg, pkt, k, (int(r.litpat[k], 16) + 1 + rng.randrange(15)) & 0xF)
        elif need > 1:
            pkt = pkt[: need - 1]
        pkts.append(pkt)
    while len(pkts) < n:
        pkts.append(_rand_packet(d, rng, _pick_len(cfg, rng), pools))
    rng.shuffle(pkts)
    return pkts


def make_packets(d: Design, n: int, seed: int = 0, ports: Dict[str, int] = None):
    """Return (packets, dropped); `dropped` counts candidates whose winning signature reads a
    flit that never arrived (form B only).  Co-matching packets are kept (first row wins)."""
    ports = ports or {}
    kept: List[List[int]] = []
    dropped = 0
    for rnd in range(12):
        for p in _candidates(d, n, seed + 7919 * rnd, ports):
            _v, _keys, defined = expected(d.rows, d.config, p, ports)
            if defined:
                kept.append(p)
            else:
                dropped += 1
        if len(kept) >= n:
            break
    return kept[:n], dropped


def count_comatch(d: Design, pkts, ports=None) -> int:
    """Packets matched by rows of at least two different ids/signatures (priority decides)."""
    return sum(1 for p in pkts if len(golden.matched_keys(d.rows, d.config, p, ports)) > 1)
