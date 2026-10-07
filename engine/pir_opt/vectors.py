"""Test-vector generators for equivalence checking (general, not bench-specific).

* ``bench_directed`` -- the directed packets used by the step-1 tests
  (EtherType/proto headers, ``hi`` positions, priority, short, over-long).
* ``directed_vectors`` -- derived from any atomized PIR: for every row a packet
  satisfying it at full length / exactly its last byte / one byte short, plus
  single-byte mutations; and all-zero packets at boundary lengths.
* ``random_vectors`` -- seeded; random bytes biased towards the constrained
  values, with row patterns *planted* at their own anchor (headers) and at
  random positions biased to flit boundaries (floating patterns), random and
  boundary lengths (short packets, over-long packets).
"""
from __future__ import annotations

import random
from typing import List

from .atoms import AtomizedPir


def bench_directed(max_bytes: int = 48) -> List[bytes]:
    """Directed packets of the step-1 tests (valid for any PIR as plain bytes)."""
    def mk(eth=None, proto=None, hi=None, n=48) -> bytes:
        b = bytearray(n)
        if eth is not None and n >= 14:
            b[12:14] = eth
        if proto is not None and n > 23:
            b[23] = proto
        if hi is not None:
            for i, c in enumerate(b"hi"):
                if hi + i < n:
                    b[hi + i] = c
        return bytes(b)

    v = [mk(b"\x08\x00", 6), mk(b"\x08\x00", 0x11), mk(b"\x86\xdd"), mk(b"\x08\x00", 1), mk()]
    v += [mk(hi=k) for k in (24, 31, 32, 39, 40, 46, 47, 23, 10)]
    v += [mk(hi=47, n=64), mk(hi=46, n=64)]
    v += [mk(b"\x08\x00", 6, hi=30), mk(b"\x86\xdd", hi=40), mk(b"\x08\x00", 1, hi=40)]
    v += [mk(b"\x08\x00", 6, n=23), mk(b"\x08\x00", 6, n=24), mk(b"\x86\xdd", n=13),
          mk(b"\x86\xdd", n=14), mk(hi=31, n=32), mk(hi=31, n=33), b""]
    return v


def _fill(ap: AtomizedPir, rng: random.Random, n: int) -> bytearray:
    pool = sorted({a.value for a in ap.atoms})
    out = bytearray()
    for _ in range(n):
        out.append(rng.choice(pool) if pool and rng.random() < 0.4 else rng.randrange(256))
    return out


def _plant(buf: bytearray, shape, pos: int, rng: random.Random) -> None:
    for off, mask, value in shape:
        k = pos + off
        if 0 <= k < len(buf):
            buf[k] = value | (rng.randrange(256) & ~mask & 0xFF)


def directed_vectors(ap: AtomizedPir, rng_seed: int = 1) -> List[bytes]:
    cfg = ap.config
    B, M = cfg.bytes_per_flit, cfg.max_bytes
    rng = random.Random(rng_seed)
    out: List[bytes] = [b""]
    for n in sorted({1, B - 1, B, B + 1, M - 1, M, M + B}):
        if n > 0:
            out.append(bytes(n))
    for r in ap.rows:
        if r.anchor is None:
            continue
        last = r.anchor + r.shape[-1][0]
        for n in (M, last + 1, last, last + B + 1):
            if n <= 0:
                continue
            buf = bytearray(b"\x41" * n)
            _plant(buf, r.shape, r.anchor, rng)
            out.append(bytes(buf))
        for off, mask, _ in (r.shape[0], r.shape[-1]):  # mutate first / last constrained byte
            buf = bytearray(b"\x41" * M)
            _plant(buf, r.shape, r.anchor, rng)
            bit = 1 << (mask.bit_length() - 1)
            buf[r.anchor + off] ^= bit
            out.append(bytes(buf))
    return out


def random_vectors(ap: AtomizedPir, n: int, seed: int = 0) -> List[bytes]:
    cfg = ap.config
    B, M = cfg.bytes_per_flit, cfg.max_bytes
    rng = random.Random(seed)
    rng2 = random.Random(seed * 7919 + 1)     # independent stream: row-atom planting (below)
    rows = [r for r in ap.rows if r.anchor is not None]
    lengths = [0, 1, B - 1, B, B + 1, M - B, M - B + 1, M - 1, M, M, M, M + 1, M + B]
    out: List[bytes] = []
    for _ in range(n):
        if rng.random() < 0.7:
            ln = M
        elif rng.random() < 0.5:
            ln = rng.choice(lengths)
        else:
            ln = rng.randrange(0, M + 2 * B)
        ln = max(0, ln)
        buf = _fill(ap, rng, ln)
        for _ in range(rng.choice([0, 1, 1, 2, 3])):
            if not rows:
                break
            r = rng.choice(rows)
            roll = rng.random()
            if roll < 0.5:
                pos = r.anchor
            elif roll < 0.8:  # at / around a flit boundary (pattern straddles it)
                span = r.shape[-1][0]
                pos = rng.randrange(1, max(2, M // B + 1)) * B - rng.randrange(0, span + 1) - rng.choice([0, 0, 1])
            else:
                pos = rng.randrange(0, max(1, M))
            _plant(buf, r.shape, max(pos, 0), rng)
        # generic extra planting from randomly chosen rows' atoms (any bench): re-plant a row at its
        # anchor after the floating plants, either completely or as a near miss (one constrained
        # byte left random) so multi-byte rows with bytes inside other rows' regions occur often
        if rows and rng2.random() < 0.6:
            r = rng2.choice(rows)
            sub = r.shape
            if len(sub) > 1 and rng2.random() < 0.3:
                skip = rng2.randrange(len(sub))
                sub = tuple(x for i, x in enumerate(sub) if i != skip)
                _plant(buf, sub, r.anchor, rng2)
                k = r.anchor + r.shape[skip][0]
                if k < len(buf):
                    buf[k] = rng2.randrange(256)
            else:
                _plant(buf, sub, r.anchor, rng2)
        # generic extra planting from randomly chosen rows' atoms (any bench): re-plant a row at its
        # anchor after the floating plants, either completely or as a near miss (one constrained
        # byte left random) so multi-byte rows with bytes inside other rows' regions occur often
        if rows and rng2.random() < 0.6:
            r = rng2.choice(rows)
            sub = r.shape
            if len(sub) > 1 and rng2.random() < 0.3:
                skip = rng2.randrange(len(sub))
                sub = tuple(x for i, x in enumerate(sub) if i != skip)
                _plant(buf, sub, r.anchor, rng2)
                k = r.anchor + r.shape[skip][0]
                if k < len(buf):
                    buf[k] = rng2.randrange(256)
            else:
                _plant(buf, sub, r.anchor, rng2)
        out.append(bytes(buf))
    return out
