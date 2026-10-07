"""Tests for pir_opt: parsing, atoms, families, golden evaluator."""
import os
import random

import pytest

from pir_opt.atoms import atomize, compress_ranges, pattern_to_bytes, Atom
from pir_opt.golden import eval_pir, eval_pir_via_atoms
from pir_opt.pir import PirConfig, load_pir, parse_pir_lines, parse_sv_literal

BENCH = os.path.join(os.path.dirname(__file__), "..", "bench")
HEX, JSON = os.path.join(BENCH, "mixed_hi.hex"), os.path.join(BENCH, "mixed_hi.json")

TCP, UDP, V6, HI = 1, 2, 3, 4


@pytest.fixture(scope="module")
def bench():
    rows, cfg = load_pir(HEX, JSON)
    return rows, cfg, atomize(rows, cfg)


# ---------------------------------------------------------------- parsing
def test_sv_literal():
    assert parse_sv_literal("4'h3") == (4, 3)
    assert parse_sv_literal("4'd3") == (4, 3)
    assert parse_sv_literal("7'hA") == (7, 10)
    with pytest.raises(ValueError):
        parse_sv_literal("2'd7")


def test_config_and_rows(bench):
    rows, cfg, _ = bench
    assert (cfg.data_width, cfg.max_width) == (64, 384)
    assert (cfg.bytes_per_flit, cfg.num_flits, cfg.max_bytes) == (8, 6, 48)
    assert len(rows) == 26
    assert all(len(r.pattern) == 96 for r in rows)
    assert [r.id_value for r in rows[:4]] == [1, 2, 3, 4]
    assert all(r.id_width == 4 and not r.is_extraction for r in rows)


def test_parse_skips_comments_and_marks_extraction():
    cfg = PirConfig(64, 64)
    rows = parse_pir_lines([
        "# comment\n", "\n", "// c\n",
        "0800xxxxxxxxxxxx 4'hA\n",
        "86ddxxxxxxxxxxxx id2:60:63\n",
    ], cfg)
    assert len(rows) == 2
    assert not rows[0].is_extraction and rows[0].id_value == 10
    assert rows[1].is_extraction and rows[1].extraction == "id2:60:63"
    # extraction rows are skipped in matching and atomization
    assert eval_pir(rows, cfg, bytes.fromhex("86dd000000000000")) == 0
    assert len(atomize(rows, cfg).rows) == 1


def test_bad_inputs():
    cfg = PirConfig(64, 64)
    with pytest.raises(ValueError):
        parse_pir_lines(["08 4'h1\n"], cfg)           # wrong length
    with pytest.raises(NotImplementedError):
        parse_pir_lines(["08zzxxxxxxxxxxxx 4'h1\n"], cfg)


# ------------------------------------------------------------------ atoms
def test_nibble_masks():
    assert pattern_to_bytes("6xx9xxxx") == [(0, 0xF0, 0x60), (1, 0x0F, 0x09)]
    assert pattern_to_bytes("xxxx") == []
    assert pattern_to_bytes("abxx") == [(0, 0xFF, 0xAB)]


def test_atoms_nibble_and_straddle():
    cfg = PirConfig(64, 128)  # 8 B flits, 16 B
    # byte 7 = 6x (flit 0, lane 7), byte 8 = x9 (flit 1, lane 0): straddles
    pat = "x" * 14 + "6x" + "x9" + "x" * 14
    rows = parse_pir_lines([pat + " 4'h5\n"], cfg)
    ap = atomize(rows, cfg)
    assert ap.atoms == [Atom(0, 7, 0xF0, 0x60), Atom(1, 0, 0x0F, 0x09)]
    assert ap.rows[0].atom_ids == (0, 1)
    assert ap.rows[0].anchor == 7
    assert ap.rows[0].shape == ((0, 0xF0, 0x60), (1, 0x0F, 0x09))


def test_atoms_dedupe_and_no_id(bench):
    _, _, ap = bench
    assert len(ap.atoms) == len(set(ap.atoms))
    # 0800 atoms shared by TCP and UDP rows
    assert ap.rows[0].atom_ids[:2] == ap.rows[1].atom_ids[:2]
    assert all(len(a) == 4 for a in ap.atoms)  # (t, lane, mask, value): no id


def test_straddling_hi_rows(bench):
    _, _, ap = bench
    by_anchor = {r.anchor: r for r in ap.rows if r.path_id == HI}
    for k in (31, 39):
        ts = {ap.atoms[a].t for a in by_anchor[k].atom_ids}
        assert ts == {k // 8, k // 8 + 1}
    assert {ap.atoms[a].t for a in by_anchor[24].atom_ids} == {3}


def test_families(bench):
    _, _, ap = bench
    assert len(ap.families) == 4
    fam = {f.path_id: f for f in ap.families}
    assert fam[HI].anchors == tuple(range(24, 47))
    assert fam[HI].shape == ((0, 0xFF, 0x68), (1, 0xFF, 0x69))
    assert fam[TCP].anchors == (12,) and fam[V6].anchors == (12,)
    assert fam[TCP].shape[-1] == (11, 0xFF, 0x06)


def test_compress_ranges():
    assert compress_ranges(list(range(24, 47))) == "24..46"
    assert compress_ranges([1, 2, 3, 7, 9, 10]) == "1..3, 7, 9..10"
    assert compress_ranges([]) == ""


# ----------------------------------------------------------------- golden
def _ev(bench, p):
    rows, cfg, ap = bench
    a, b = eval_pir(rows, cfg, p), eval_pir_via_atoms(ap, p)
    assert a == b
    return a


def mk(**kw):
    """mk(eth=b'\\x08\\x00', proto=6, hi=24, n=48)"""
    n = kw.get("n", 48)
    b = bytearray(n)
    if "eth" in kw:
        b[12:14] = kw["eth"]
    if "proto" in kw and n > 23:
        b[23] = kw["proto"]
    if "hi" in kw and kw["hi"] is not None:
        k = kw["hi"]
        for i, c in enumerate(b"hi"):
            if k + i < n:
                b[k + i] = c
    return bytes(b[:n])


def test_directed_headers(bench):
    assert _ev(bench, mk(eth=b"\x08\x00", proto=6)) == TCP
    assert _ev(bench, mk(eth=b"\x08\x00", proto=0x11)) == UDP
    assert _ev(bench, mk(eth=b"\x86\xdd")) == V6
    assert _ev(bench, mk(eth=b"\x08\x00", proto=1)) == 0
    assert _ev(bench, mk()) == 0


@pytest.mark.parametrize("k,expect", [
    (24, HI), (31, HI), (32, HI), (39, HI), (40, HI), (46, HI),
    (47, 0),   # only 'h' fits in the 48 B window
    (23, 0),   # outside range (and 'i' lands on byte 24 only)
    (10, 0),
])
def test_hi_positions(bench, k, expect):
    assert _ev(bench, mk(hi=k)) == expect


def test_hi_straddle_boundaries(bench):
    # 31 -> 'h' last lane of flit 3, 'i' lane 0 of flit 4
    p = mk(hi=31)
    assert p[31] == ord("h") and p[32] == ord("i")
    assert _ev(bench, p) == HI
    assert _ev(bench, mk(hi=39)) == HI


def test_packet_longer_than_max_ignored(bench):
    # 'hi' at 47..48: byte 48 beyond window -> only 'h' considered
    p = mk(hi=47, n=64)
    assert p[47:49] == b"hi"
    assert _ev(bench, p) == 0
    assert _ev(bench, mk(hi=46, n=64)) == HI


def test_priority(bench):
    # TCP header and hi both present -> TCP (earlier row) wins
    assert _ev(bench, mk(eth=b"\x08\x00", proto=6, hi=30)) == TCP
    assert _ev(bench, mk(eth=b"\x86\xdd", hi=40)) == V6
    assert _ev(bench, mk(eth=b"\x08\x00", proto=1, hi=40)) == HI


def test_first_row_wins_on_overlap():
    cfg = PirConfig(64, 64)
    rows = parse_pir_lines(["08xxxxxxxxxxxxxx 4'h2\n", "0800xxxxxxxxxxxx 4'h1\n"], cfg)
    assert eval_pir(rows, cfg, bytes.fromhex("0800000000000000")) == 2
    assert eval_pir_via_atoms(atomize(rows, cfg), bytes.fromhex("0800000000000000")) == 2


def test_short_packets(bench):
    # packet ends right before byte 23: proto cannot match -> no TCP
    assert _ev(bench, mk(eth=b"\x08\x00", proto=6, n=23)) == 0
    # packet ends right after byte 23: proto present -> TCP
    assert _ev(bench, mk(eth=b"\x08\x00", proto=6, n=24)) == TCP
    # EtherType truncated at byte 13 (n=13)
    assert _ev(bench, mk(eth=b"\x86\xdd", n=13)) == 0
    assert _ev(bench, mk(eth=b"\x86\xdd", n=14)) == V6
    # hi cut off: 'h' at 31 but packet ends at 32 -> 'i' missing
    assert _ev(bench, mk(hi=31, n=32)) == 0
    assert _ev(bench, mk(hi=31, n=33)) == HI
    assert _ev(bench, b"") == 0


def test_wildcard_row_matches_everything():
    cfg = PirConfig(64, 64)
    rows = parse_pir_lines(["xxxxxxxxxxxxxxxx 4'h9\n"], cfg)
    assert eval_pir(rows, cfg, b"") == 9
    assert eval_pir_via_atoms(atomize(rows, cfg), b"") == 9


def test_nibble_dont_care_eval():
    cfg = PirConfig(64, 64)
    rows = parse_pir_lines(["6xxxxxxxxxxxxxx9 4'h7\n"], cfg)
    ap = atomize(rows, cfg)
    p = bytes([0x6F] + [0] * 6 + [0xA9])
    assert eval_pir(rows, cfg, p) == 7 == eval_pir_via_atoms(ap, p)
    q = bytes([0x7F] + [0] * 6 + [0xA9])
    assert eval_pir(rows, cfg, q) == 0 == eval_pir_via_atoms(ap, q)


# ------------------------------------------------------------ cross-check
def test_random_equivalence(bench):
    rows, cfg, ap = bench
    rng = random.Random(12345)
    hits = {0: 0, TCP: 0, UDP: 0, V6: 0, HI: 0}
    for _ in range(2000):
        n = rng.choice([0, 5, 13, 14, 23, 24, 32, 33, 47, 48, 48, 48, 64, 100])
        b = bytearray(rng.choice([0, 0x41]) if rng.random() < .5 else rng.randrange(256)
                      for _ in range(n))
        for i in range(n):
            b[i] = rng.randrange(256) if rng.random() < .3 else 0x41
        if rng.random() < .6 and n > 14:
            b[12:14] = rng.choice([b"\x08\x00", b"\x86\xdd", b"\x08\x06"])
        if rng.random() < .6 and n > 23:
            b[23] = rng.choice([6, 0x11, 1])
        for _ in range(rng.choice([0, 0, 1, 2])):
            k = rng.randrange(18, 50)
            for i, c in enumerate(b"hi"):
                if k + i < n:
                    b[k + i] = c
        p = bytes(b)
        g = eval_pir(rows, cfg, p)
        assert g == eval_pir_via_atoms(ap, p), p.hex()
        hits[g] += 1
    assert all(v > 50 for v in hits.values()), hits
