import glob
import os
import shutil

import pytest

from ptp import design, emit, golden, model, pir, run, vectors

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BENCH = DATA
SYN = DATA

ALL = [(b + ".hex", b + ".json", None, None)
       for b in (os.path.splitext(h)[0] for h in sorted(glob.glob(os.path.join(BENCH, "bench_*.hex"))))]
_syn = os.path.join(SYN, "synthetic_256.hex")
if os.path.exists(_syn):
    ALL += [(_syn, os.path.join(SYN, "one_group.json"), None, None),
            (_syn, os.path.join(SYN, "multi_group.json"), None, None),
            (_syn, os.path.join(SYN, "one_group.json"), 32, None),
            (_syn, os.path.join(SYN, "one_group.json"), None, 3)]
IDS = [f"{os.path.basename(h)}-dw{d}-cpc{c}" for h, _j, d, c in ALL]
HAVE_VERILATOR = bool(shutil.which(run.VERILATOR))


def _small(hexp, jsonp, dw, cpc):
    rows, cfg = pir.load_pir(hexp, jsonp, dw, cpc)
    return rows, cfg, design.build(rows, cfg)


@pytest.mark.parametrize("hexp,jsonp,dw,cpc", ALL, ids=IDS)
def test_golden_vs_model(hexp, jsonp, dw, cpc):
    rows, cfg, d = _small(hexp, jsonp, dw, cpc)
    pk, _dropped = vectors.make_packets(d, 300, seed=3)
    assert pk
    for p in pk:
        assert golden.evaluate(rows, cfg, p) == model.evaluate(d, p)


@pytest.mark.parametrize("hexp,jsonp,dw,cpc", ALL, ids=IDS)
def test_columns_exact(hexp, jsonp, dw, cpc):
    rows, cfg, d = _small(hexp, jsonp, dw, cpc)
    npf = cfg.nibbles_per_flit
    # columns tile the pattern, never straddle a flit, and are exact
    assert d.all_cols[0][0] == 0 and d.all_cols[-1][1] == cfg.num_nibbles
    for (s, e), nxt in zip(d.all_cols, d.all_cols[1:] + [None]):
        assert s // npf == (e - 1) // npf
        if nxt:
            assert nxt[0] == e
    pats = [r.pattern for r in rows]
    for c in d.columns:
        segs = design._distinct_segments(pats, c.start, c.end)
        assert segs == c.segs
        # brute-force pairwise conflict on a bounded sample (column_exact is the fast path)
        sample = segs[:200]
        for i, a in enumerate(sample):
            for b in sample[i + 1:]:
                assert any(x != "x" and y != "x" and x != y for x, y in zip(a, b)), (c.index, a, b)
        assert design.column_exact(segs)


def test_exactness_split():
    # "1x" vs "x2" do not conflict -> must be split per run; "1x" vs "11" likewise
    pats = ["1xxx", "x2xx", "xx3x", "xxx4"]
    cols = design.enforce_exact(pats, [(0, 4)])
    assert len(cols) > 1
    for s, e in cols:
        assert design.column_exact(design._distinct_segments(pats, s, e))
    assert not design.column_exact(["1x", "11"])
    assert design.column_exact(["12", "13", "2x"])


def _rows(text):
    return pir.parse_rows(text.strip().splitlines())


def _pkt(hexstr):
    return [int(hexstr, 16)]


BUG2 = "08xxxxxxxxxxxxxx 2'd1\nxx6869xxxxxxxxxx 2'd2\nxxxx6869xxxxxxxx 2'd2\n"


def test_bug2min_repro():
    rows = _rows(BUG2)
    cfg = pir.Config(64, 64)
    d = design.build(rows, cfg)
    p = _pkt("0800680000000000")    # 08 00 68 00 ..: only the 08 row matches
    assert golden.matched_ids(rows, cfg, p) == {1}
    assert model.evaluate(d, p) == 1


def test_same_id_overlap_hi():
    rows = _rows("xx6869xxxxxxxxxx 2'd2\nxxxxxx6869xxxxxx 2'd2\n08xxxxxxxxxxxxxx 2'd1\n")
    cfg = pir.Config(64, 64)
    d = design.build(rows, cfg)
    assert d.cm_count == 2   # 08-row vs each hi row (different ids) can co-match
    p = _pkt("0068696869000000")    # both hi rows match
    assert golden.matched_ids(rows, cfg, p) == {2}
    assert model.evaluate(d, p) == 2


def test_short_packet_and_long_packet():
    rows = _rows("xxxxxxxx12xxxxxx 3'd1\n" + "x" * 8 + "34xxxxxx 3'd2\n")
    cfg = pir.Config(32, 64)    # T = 2
    d = design.build(rows, cfg)
    assert model.evaluate(d, [0, 0x12000000]) == 1
    assert model.evaluate(d, [0]) == 0
    assert model.evaluate(d, [0, 0x34000000, 0x12000000]) == 2   # extra flit ignored
    assert golden.evaluate(rows, cfg, [0, 0x34000000, 0x12000000]) == 2


def test_bad_rows():
    with pytest.raises(ValueError, match="both id and extraction"):
        pir.parse_rows(["ab 4'd1 name:0:8\n"])
    with pytest.raises(ValueError):
        pir.parse_rows(["a_ 4'd1\n"])


def test_emit_smoke(tmp_path):
    h, j, dw, cpc = ALL[0]
    d, st, _ = run.generate(h, j, str(tmp_path), dw, packets=50, char_per_col=cpc)
    v = (tmp_path / "parser.sv").read_text()
    assert "module parser(" in v and "unique case (1'b1)" in v and "TreeLUT" not in v
    assert v.count("wire c_j") == st["comparators"]
    assert st["model_mismatches"] == 0


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator missing")
@pytest.mark.parametrize("hexp,jsonp,dw,cpc", ALL, ids=IDS)
def test_verilator(hexp, jsonp, dw, cpc, tmp_path):
    big = "u256" in hexp
    d, st, _ = run.generate(hexp, jsonp, str(tmp_path), dw, packets=300 if big else 2000, char_per_col=cpc)
    assert st["model_mismatches"] == 0
    ok, mism, _b, _r, log = run.simulate(str(tmp_path))
    assert ok and mism == 0, log[-2000:]


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator missing")
def test_bug_repros_sim(tmp_path):
    hx = tmp_path / "b.hex"
    hx.write_text(BUG2)
    rel = os.path.relpath(str(tmp_path / "o"))   # relative -o must work
    d, st, _ = run.generate(str(hx), None, rel, 64, packets=500)
    ok, mism, *_ = run.simulate(rel)
    assert ok and mism == 0


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator missing")
def test_tb_detects_bug(tmp_path):
    hexp = os.path.join(BENCH, "bench_mixed_hi.hex")
    run.generate(hexp, hexp[:-4] + ".json", str(tmp_path), None, packets=500)
    p = tmp_path / "parser.sv"
    s = p.read_text()
    assert "id_c = 4'd1;" in s
    p.write_text(s.replace("id_c = 4'd1;", "id_c = 4'd7;", 1))
    ok, mism, *_ = run.simulate(str(tmp_path))
    assert not ok and mism > 0


# ---- latch fix + OR encoding -------------------------------------------------------------------
import re

REAL = [("simple_parser", os.path.join(DATA, "simple_parser_ids.hex"), os.path.join(DATA, "simple_parser.json")),
        ("full_parser", os.path.join(DATA, "full_parser_ids.hex"), os.path.join(DATA, "full_parser.json")),
        ("hi_caseC", os.path.join(DATA, "hi_caseC.hex"), os.path.join(DATA, "hi_caseC.json"))]
REAL = [r for r in REAL if os.path.exists(r[1]) and os.path.exists(r[2])]


def _check_latches(v):
    pids = re.findall(r"reg \[\d+:0\] pid_(\d+);", v)
    assert pids
    for i in pids:
        assert f"pid_{i} <= pn_{i}" in v
        m = re.search(rf"always @\(posedge clk\) begin\n\tif \(clr\) pid_{i} <= 0;\n\telse if \(cap_t(\d+)\) pid_{i} <= pn_{i};\nend", v)
        m0 = re.search(rf"always @\(posedge clk\) if \(cap_t0\) pid_{i} <= pn_{i};", v)
        assert (m is not None) != (m0 is not None), i     # exactly one of the two shapes
        if m:
            assert m.group(1) != "0"
    assert not re.search(r"if \(rst\) pid_", v)
    assert "else if (clr)" not in v


@pytest.mark.parametrize("or_encode", [False, True])
def test_emit_modes_text(tmp_path, or_encode):
    h, j, dw, cpc = ALL[0]
    d, st, _ = run.generate(h, j, str(tmp_path), dw, packets=50, char_per_col=cpc, or_encode=or_encode)
    v = (tmp_path / "parser.sv").read_text()
    assert st["model_mismatches"] == 0 and st["or_encode"] == or_encode
    _check_latches(v)
    if or_encode:
        code = "\n".join(l.split("//")[0] for l in v.splitlines())
        assert "case" not in code
        assert re.search(r"pn_\d+ = \{.*\};", v) and re.search(r"id_c = \{.*\};", v)
        assert "FIRST ROW WINS" in v and "priority-masked" in v
    else:
        assert "unique case (1'b1)" in v


def test_or_vec():
    assert emit._or_vec(3, [(1, "a"), (2, "b"), (3, "c"), (4, "d")]) == "{d, b | c, a | c}"
    assert emit._or_vec(2, [(1, "a")]) == "{1'b0, a}"


def test_flit0_column_no_clear(tmp_path):
    rows = _rows("08xxxxxxxxxxxxxx 2'd1\n0Axxxxxxxxxxxxxx 2'd2\n")
    cfg = pir.Config(64, 64)
    d = design.build(rows, cfg)
    emit.emit_parser(d, str(tmp_path / "t.sv"))
    v = (tmp_path / "t.sv").read_text()
    assert "if (cap_t0) pid_0 <= pn_0;" in v and "if (clr) pid_0" not in v


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator missing")
@pytest.mark.parametrize("or_encode", [False, True])
@pytest.mark.parametrize("name,hexp,jsonp", REAL, ids=[r[0] for r in REAL])
def test_verilator_real(name, hexp, jsonp, or_encode, tmp_path):
    d, st, _ = run.generate(hexp, jsonp, str(tmp_path), None, packets=2000, or_encode=or_encode)
    assert st["model_mismatches"] == 0
    ok, mism, _b, _r, log = run.simulate(str(tmp_path))
    assert ok and mism == 0, log[-2000:]


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator missing")
@pytest.mark.parametrize("hexp,jsonp,dw,cpc", ALL[:4], ids=IDS[:4])
def test_verilator_or_bench(hexp, jsonp, dw, cpc, tmp_path):
    d, st, _ = run.generate(hexp, jsonp, str(tmp_path), dw, packets=1000, char_per_col=cpc, or_encode=True)
    ok, mism, _b, _r, log = run.simulate(str(tmp_path))
    assert ok and mism == 0, log[-2000:]
