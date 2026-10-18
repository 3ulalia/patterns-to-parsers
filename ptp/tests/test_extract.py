"""Form B (extraction), custom tokens, valid/ready handshake, P4 frontend."""
import os
import shutil

import pytest

from ptp import design, emit, golden, model, p4_to_pir, pir, run, tokens as tk, vectors

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
ROOT = os.path.dirname(os.path.dirname(HERE))
HAVE_VERILATOR = bool(shutil.which(run.VERILATOR))
HAVE_P4C = shutil.which(os.environ.get("P4C", "p4c")) is not None


def _tokens(*entries):
    return tk.parse_tokens(list(entries))


def _rows(text, tokens=None, mw=None):
    return pir.parse_rows(text.strip().splitlines(), tokens, mw)


# ------------------------------------------------------------------ forms
def test_form_a_and_b_parse():
    a = _rows("12xx 4'd3\nabxx 4'd1")
    assert [r.form for r in a] == ["A", "A"] and a[0].id_value == 3
    b = _rows("12xx f:15:8;g:3:0\nabxx f:7:0", mw=16)
    assert b[0].fields == (("f", 15, 8), ("g", 3, 0)) and b[0].form == "B"


def test_mixed_forms_error():
    with pytest.raises(ValueError, match="mixed PIR forms"):
        _rows("12xx 4'd3\nabxx f:7:0")
    with pytest.raises(ValueError, match="mixed PIR forms"):
        _rows("12xx f:7:0\nabxx 4'd3")


def test_duplicate_field_error():
    with pytest.raises(ValueError, match="more than once"):
        _rows("12xx f:15:8;f:7:0")


def test_bad_tags():
    with pytest.raises(ValueError, match="out of range"):
        _rows("12xx f:16:8", mw=16)
    with pytest.raises(ValueError):
        _rows("12xx f:3:8")
    with pytest.raises(ValueError, match="neither"):
        _rows("12xx")


def test_extraction_values_and_convention():
    # window bit max_width-1 is the first wire bit: tag f:15:8 is the FIRST byte
    rows = _rows("ab12 f:15:8;g:7:4\nab13 h:3:0\ncdxx f:15:8", mw=16)
    cfg = pir.Config(16, 16, None, {}, "B", {"f": 8, "g": 4, "h": 4})
    pkt = [0xAB12]
    v, keys, defined = golden.expected(rows, cfg, pkt)
    assert defined and v == (0xAB, 0x1, 0)          # order f, g, h
    pkt = [0xAB13]
    assert golden.expected(rows, cfg, pkt)[0] == (0, 0, 3)
    assert golden.expected(rows, cfg, [0xCD00])[0] == (0xCD, 0, 0)
    assert golden.expected(rows, cfg, [0x0000])[0] == (0, 0, 0)       # no match -> zeros
    d = design.build(rows, cfg)
    for p in ([0xAB12], [0xAB13], [0xCD77], [0x1234]):
        assert model.evaluate(d, p) == golden.evaluate(rows, cfg, p)


def test_field_width_max_and_zero_extend(tmp_path):
    hx = tmp_path / "p.hex"
    hx.write_text("08xxxxxx f:31:24\n09xxxxxx f:31:20\n")
    rows, cfg = pir.load_pir(str(hx), None, 32)
    assert cfg.field_widths == {"f": 12}
    emit.emit_extraction(design.build(rows, cfg), str(tmp_path / "e.sv"))
    v = (tmp_path / "e.sv").read_text()
    assert "{4'b0, win[31:24]}" in v and "win[31:20]" in v and "sel[0]" in v and "sel[1]" in v
    assert "if" not in v.replace("module", "")      # no priority chain


def test_signature_groups_share_select():
    rows = _rows("12xx f:15:8\n13xx f:15:8\n14xx f:7:0", mw=16)
    cfg = pir.Config(16, 16, None, {}, "B", {"f": 8})
    d = design.build(rows, cfg)
    assert len(d.groups) == 2 and d.group_of == [0, 0, 1]


def test_cross_signature_warning():
    rows = _rows("12xx f:15:8\nx2xx f:7:0", mw=16)
    cfg = pir.Config(16, 16, None, {}, "B", {"f": 8})
    assert design.build(rows, cfg).cm_count == 1


# ------------------------------------------------------------------ tokens
def test_token_opcode_semantics():
    rng = tk.parse_tokens([{"id": "GH", "type": "const", "opcode": "RANGE", "values": ["10", "20"]}])["GH"]
    assert [rng.matches(v) for v in (0x0F, 0x10, 0x1F, 0x20, 0x21)] == [False, True, True, False, False]  # hi exclusive
    ne = tk.parse_tokens([{"id": "GH", "type": "const", "opcode": "NE", "values": ["16", ""]}])["GH"]
    assert [ne.matches(v) for v in (0x15, 0x16, 0x17)] == [True, False, True]
    eq = tk.parse_tokens([{"id": "GH", "type": "const", "opcode": "EQOR", "values": ["01", "FF", "7"]}])["GH"]
    assert [eq.matches(v) for v in (0, 1, 7, 8, 0xFF)] == [False, True, True, False, True]


def test_port_token_semantics():
    t = tk.parse_tokens([{"id": "GH", "type": "port", "opcode": "RANGE", "prefix": "r"},
                         {"id": "IJ", "type": "port", "opcode": "EQOR", "prefix": "e", "num_ports": 2},
                         {"id": "KL", "type": "port", "opcode": "NE", "prefix": "n"}])
    ports = {"r_0": 5, "r_1": 9, "e_0": 3, "e_1": 4, "n": 7}
    assert [t["GH"].matches(v, ports) for v in (4, 5, 8, 9)] == [False, True, True, False]
    assert [t["IJ"].matches(v, ports) for v in (2, 3, 4, 5)] == [False, True, True, False]
    assert [t["KL"].matches(v, ports) for v in (6, 7)] == [True, False]
    assert t["GH"].port_names() == ["r_0", "r_1"] and t["IJ"].port_names() == ["e_0", "e_1"]


def test_token_config_errors():
    with pytest.raises(ValueError):
        tk.parse_tokens([{"id": "GH", "type": "const", "opcode": "RANGE", "values": ["20", "10"]}])
    with pytest.raises(ValueError):
        tk.parse_tokens([{"id": "GH", "type": "const", "opcode": "BAD", "values": ["1"]}])
    with pytest.raises(ValueError, match="prefix"):
        tk.parse_tokens([{"id": "G", "type": "const", "opcode": "NE", "values": ["1"]},
                         {"id": "GH", "type": "const", "opcode": "NE", "values": ["1"]}])


T3 = [{"id": "GHIJ", "type": "const", "opcode": "RANGE", "values": ["1000", "2000"]},
      {"id": "KLMN", "type": "const", "opcode": "EQOR", "values": ["3000", "FFFF"]},
      {"id": "PQRS", "type": "const", "opcode": "NE", "values": ["0016", ""]}]


def test_golden_with_tokens():
    toks = tk.parse_tokens(T3)
    rows = _rows("GHIJxxxx 3'd1\nxxxxKLMN 3'd2\n5555xxxx 3'd3", toks)
    cfg = pir.Config(32, 32, None, toks)
    g = lambda p: golden.evaluate(rows, cfg, [p])
    assert g(0x55550000) == 3
    assert g(0x10000000) == 1 and g(0x1FFF0000) == 1 and g(0x20000000) == 0
    assert g(0x00003000) == 2 and g(0x0000FFFF) == 2 and g(0x00003001) == 0
    d = design.build(rows, cfg)
    for p in (0x10000000, 0x1FFF1234, 0x20000000, 0x00003000, 0x0000FFFF, 0x00003001, 0xFFFFFFFF):
        assert model.evaluate(d, [p]) == g(p)


def test_token_split_by_flit_error():
    toks = tk.parse_tokens([{"id": "GHIJ", "type": "const", "opcode": "NE", "values": ["1"]}])
    hx = "xxGHIJxx 2'd1\n"
    rows = _rows(hx, toks)
    cfg = pir.Config(16, 32, None, toks)       # 4 nibbles per flit; token at nibbles 2..5
    with pytest.raises(ValueError, match=r"GHIJ.*straddles the flit boundary"):
        pir.check_tokens(rows, cfg)


def _check_prio(rows, cfg, n=400, ports=None, seed=1):
    d = design.build(rows, cfg)
    pk, _ = vectors.make_packets(d, n, seed, ports or {})
    for p in pk:
        assert golden.evaluate(rows, cfg, p, ports) == model.evaluate(d, p, ports)
    return d, pk


def test_const_token_overlap_builds_onehot_priority():
    toks = tk.parse_tokens(T3[:1])
    rows = _rows("GHIJxxxx 3'd1\n1234xxxx 3'd2", toks)       # 0x1234 is inside [0x1000,0x2000)
    cfg = pir.Config(32, 32, None, toks)
    d, pk = _check_prio(rows, cfg)
    assert [c.onehot for c in d.columns] == [True] and d.columns[0].width == 2
    assert d.cm_count == 1 and d.masks[1] == [0] and any("ONE-HOT" in l for l in d.info)
    assert vectors.count_comatch(d, pk) > 0
    # first row wins: 0x1234 -> id 1; reversed order -> id 2
    assert golden.evaluate(rows, cfg, [0x12340000]) == 1
    rows2 = _rows("1234xxxx 3'd2\nGHIJxxxx 3'd1", toks)
    assert golden.evaluate(rows2, cfg, [0x12340000]) == 2 and golden.evaluate(rows2, cfg, [0x15000000]) == 1
    ok = _rows("GHIJxxxx 3'd1\n3234xxxx 3'd2", toks)
    d = design.build(ok, cfg)
    assert len(d.columns) == 1 and not d.columns[0].onehot and d.cm_count == 0


def test_token_token_overlap_and_partial():
    toks = tk.parse_tokens([T3[0], {"id": "ZZZZ", "type": "const", "opcode": "NE", "values": ["1500"]}])
    cfg = pir.Config(32, 32, None, toks)
    d = design.build(_rows("GHIJxxxx 3'd1\nZZZZxxxx 3'd2", toks), cfg)       # NE 1500 overlaps the range
    assert d.columns[0].onehot and d.cm_count == 1
    toks2 = tk.parse_tokens([T3[0], {"id": "WXYZ", "type": "const", "opcode": "EQOR", "values": ["3000", "4000"]}])
    d = design.build(_rows("GHIJxxxx 3'd1\nWXYZxxxx 3'd2", toks2), pir.Config(32, 32, None, toks2))   # disjoint
    assert d.columns and not d.columns[0].onehot and d.cm_count == 0
    # partially overlapping token spans cannot be proven exclusive -> one-hot, masked
    toks3 = tk.parse_tokens([{"id": "GH", "type": "const", "opcode": "EQOR", "values": ["10"]},
                             {"id": "IJ", "type": "const", "opcode": "EQOR", "values": ["20"]}])
    rows3 = _rows("GHxxxxxx 3'd1\nxIJxxxxx 3'd2", toks3)
    d, _ = _check_prio(rows3, pir.Config(32, 32, None, toks3))
    assert d.cm_count == 1 and any(c.onehot for c in d.columns)


def test_priority_different_ids_literal_rows():
    rows = _rows("12xx 2'd1\nx2xx 2'd2\n12xx 2'd3\nxxxx 2'd0\n1xxx 2'd1")
    cfg = pir.Config(16, 16)
    d, pk = _check_prio(rows, cfg, 300)
    assert d.dead == [2]                                  # identical match to row 0, other id: never wins
    assert d.masks[1] == [0] and d.cm_count >= 1
    for h, exp in ((0x1200, 1), (0x0200, 2), (0x1300, 0), (0x9999, 0)):
        assert golden.evaluate(rows, cfg, [h]) == exp
    assert vectors.count_comatch(d, pk) > 0


def test_priority_form_b():
    rows = _rows("12xx f:15:8\nx2xx f:7:0\n1xxx g:3:0", mw=16)
    cfg = pir.Config(16, 16, None, {}, "B", {"f": 8, "g": 4})
    d, pk = _check_prio(rows, cfg)
    assert d.masks[1] == [0] and d.masks[2] == [0, 1]
    assert golden.evaluate(rows, cfg, [0x1234]) == (0x12, 0)


def test_id0_row_masks_later_rows():
    rows = _rows("12xx 2'd0\n1xxx 2'd3")
    cfg = pir.Config(16, 16)
    d, _ = _check_prio(rows, cfg)
    assert golden.evaluate(rows, cfg, [0x1200]) == 0 and golden.evaluate(rows, cfg, [0x1300]) == 3


def test_port_token_onehot_and_ports(capsys):
    rows, cfg = pir.load_pir(os.path.join(DATA, "tok_port.hex"), os.path.join(DATA, "tok_port.json"))
    d = design.build(rows, cfg)
    oh = [c for c in d.columns if c.onehot]
    assert len(oh) == 1 and "port token" in oh[0].reason and any("ONE-HOT" in l for l in d.info)
    assert [n for n, _w in emit._used_ports(d)] == ["ne_c", "rng_a_0", "rng_a_1", "set_b_0", "set_b_1", "set_b_2"]
    with pytest.raises(SystemExit):
        run.main(["-h"])
    out = capsys.readouterr().out
    assert "STATIC" in out and "FIRST ROW WINS" in out


def test_port_token_overlap_correct():
    rows, cfg = pir.load_pir(os.path.join(DATA, "tok_port.hex"), os.path.join(DATA, "tok_port.json"))
    d = design.build(rows, cfg)
    ports = {"rng_a_0": 0x100, "rng_a_1": 0x200, "set_b_0": 0x150, "set_b_1": 1, "set_b_2": 2, "ne_c": 5}
    pk, _ = vectors.make_packets(d, 500, 2, ports)
    assert vectors.count_comatch(d, pk, ports) > 0
    for p in pk:
        assert golden.evaluate(rows, cfg, p, ports) == model.evaluate(d, p, ports)
    # both tokens match 0x150: line 1 wins
    assert golden.evaluate(rows, cfg, [0x08000150, 0], ports) == 1


def test_onehot_rtl_text(tmp_path):
    toks = tk.parse_tokens(T3[:1])
    rows = _rows("GHIJxxxx 3'd1\n1234xxxx 3'd2", toks)
    d = design.build(rows, pir.Config(32, 32, None, toks))
    emit.emit_parser(d, str(tmp_path / "p.sv"))
    v = (tmp_path / "p.sv").read_text()
    assert "wire [1:0] pn_0 = {s_0_2, s_0_1};" in v
    assert '(* fsm_encoding = "none" *) reg [1:0] pid_0;' in v
    assert "wire row_0 = pid_0[0];" in v and "wire eff_1 = row_1 & ~(row_0);" in v
    assert "FIRST ROW WINS" in v and "ONE-HOT" in v


def test_column_boundary_never_cuts_token():
    hexp, jsonp = os.path.join(DATA, "tok_const.hex"), os.path.join(DATA, "tok_const.json")
    for cpc in (None, 1, 2, 3, 5):
        rows, cfg = pir.load_pir(hexp, jsonp, None, cpc)
        d = design.build(rows, cfg)
        inside = design.token_inside(rows)
        for s, e in d.all_cols:
            assert not (s in inside), (cpc, s)
        for c in d.columns:
            for g in c.segs:
                # a token appearing in a segment is complete
                assert sum(t.nibbles for _o, t in tk.scan_tokens(g, cfg.tokens)) == sum(ch.isupper() for ch in g)


def test_cli_overlap_is_info_not_error(tmp_path, capsys):
    hx = tmp_path / "a.hex"
    hx.write_text("GHIJxxxx 3'd1\n1234xxxx 3'd2\n")
    js = tmp_path / "a.json"
    import json
    js.write_text(json.dumps({"data_width": 32, "max_width": 32, "custom": T3[:1]}))
    assert run.main([str(hx), str(js), "-o", str(tmp_path / "o"), "--packets", "100"]) == 0
    out = capsys.readouterr().out
    assert "INFO" in out and "can both match" in out and "ERROR" not in out
    hx.write_text("xxGHIJxx 3'd1\n")          # token cut by a flit boundary stays a hard error
    js.write_text(json.dumps({"data_width": 16, "max_width": 32, "custom": T3[:1]}))
    assert run.main([str(hx), str(js), "-o", str(tmp_path / "o2")]) == 3


def test_vectors_cover_tokens():
    rows, cfg = pir.load_pir(os.path.join(DATA, "tok_const.hex"), os.path.join(DATA, "tok_const.json"))
    d = design.build(rows, cfg)
    pk, _ = vectors.make_packets(d, 800, 1, {})
    seen = {golden.evaluate(rows, cfg, p) for p in pk}
    assert {0, 1, 2, 3, 4, 5} <= seen


# ------------------------------------------------------------------ verilator
SIMS = [("tok_const", None), ("tok_port", None), ("tok_formb", None), ("tok_const", 16)]


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator missing")
@pytest.mark.parametrize("name,dw", SIMS)
@pytest.mark.parametrize("or_encode", [False, True])
def test_verilator_tokens(name, dw, or_encode, tmp_path):
    d, st, _ = run.generate(os.path.join(DATA, name + ".hex"), os.path.join(DATA, name + ".json"), str(tmp_path),
                            dw, packets=2000, or_encode=or_encode)
    assert st["model_mismatches"] == 0 and st["packets"] >= 2000 - 1
    ok, mism, _b, _r, log = run.simulate(str(tmp_path))
    assert ok and mism == 0, log[-2000:]


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator missing")
@pytest.mark.parametrize("name", ["tok_formb", "tok_const"])
def test_tb_detects_broken_handshake(name, tmp_path):
    run.generate(os.path.join(DATA, name + ".hex"), os.path.join(DATA, name + ".json"), str(tmp_path), None, packets=300)
    f = "parser_top.sv" if name == "tok_formb" else "parser.sv"
    p = tmp_path / f
    s = p.read_text()
    old = "assign data_in_ready = mid | (~done & out_free);" if name == "tok_formb" else \
        "assign data_in_ready = mid | (~eop_d & out_free);"
    assert old in s
    p.write_text(s.replace(old, "assign data_in_ready = 1'b1;"))        # accepts SOP even when output is full
    ok, mism, *_ = run.simulate(str(tmp_path))
    assert not ok


def test_handshake_text(tmp_path):
    d, st, _ = run.generate(os.path.join(DATA, "tok_const.hex"), os.path.join(DATA, "tok_const.json"), str(tmp_path), packets=20)
    v = (tmp_path / "parser.sv").read_text()
    assert "assign data_in_ready = mid | (~eop_d & out_free);" in v
    assert "wire out_free = ~out_valid | out_ready;" in v
    assert "if (out_valid & out_ready) out_valid <= 1'b0;" in v and "output reg out_valid" in v
    assert "input out_ready" in v and "output reg [2:0] id_out" in v


# ------------------------------------------------------------------ P4 frontend
def test_dedupe_last():
    tags = [("prot", 600, 593, "ipv6", "a"), ("sip", 500, 470, "ipv6", "s"), ("prot", 300, 293, "ext1", "a"),
            ("prot", 200, 193, "ext2", "a")]
    kept, dropped = p4_to_pir.dedupe_last(tags)
    assert [t[:3] for t in kept] == [("sip", 500, 470), ("prot", 200, 193)]
    assert len(dropped) == 2


@pytest.mark.skipif(not HAVE_P4C, reason="p4c missing")
def test_p4_last_occurrence(tmp_path):
    p4 = os.path.join(ROOT, "p4", "simple_parser.p4")
    pir_path, json_path, info = p4_to_pir.generate_pir(p4, 768, str(tmp_path), 64)
    assert info["form"] == "B" and info["repeats"]
    lines = open(pir_path).read().split("\n")
    lines = [l for l in lines if l]
    ext2 = [l for l in lines if "86dd6" in l and "2b" in l.split()[0] and "prot:271:264" in l]
    assert ext2, "IPv6->ext1->ext2 paths must extract ext2's nextHdr (msb 271) as prot"
    for l in lines:
        tags = l.split()[1].split(";")
        names = [t.split(":")[0] for t in tags]
        assert len(names) == len(set(names))
        if "prot:271:264" in tags:                       # ext2 present -> neither ipv6 nor ext1 nextHdr
            assert "prot:607:600" not in tags and "prot:335:328" not in tags
    # IPv6 -> ext1 -> TCP: ext1 nextHdr (offset 432 -> msb 335)
    assert any("prot:335:328" in l and "prot:271:264" not in l for l in lines)
    # the produced PIR loads in ptp
    rows, cfg = pir.load_pir(pir_path, json_path)
    assert cfg.form == "B" and cfg.max_width == 768


@pytest.mark.skipif(not HAVE_P4C, reason="p4c missing")
def test_p4_mixed_tags_error(tmp_path):
    prog = tmp_path / "m.p4"
    prog.write_text("""#include <core.p4>
#include <v1model.p4>
header eth_t { bit<48> d; bit<48> s; bit<16> t; }
header ip_t { bit<8> proto__prot__; bit<24> rest; }
header ip2_t { bit<32> rest; }
struct metadata { }
struct headers { eth_t eth; ip_t ip; ip2_t ip2; }
parser MyParser(packet_in packet, out headers hdr, inout metadata meta, inout standard_metadata_t sm) {
    state start { packet.extract(hdr.eth); transition select(hdr.eth.t) { 0x0800: parse_ip; 0x86dd: parse_ip2; default: accept; } }
    state parse_ip { packet.extract(hdr.ip); transition accept; }
    state parse_ip2 { packet.extract(hdr.ip2); transition accept; }
}
control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply { } }
control MyIngress(inout headers hdr, inout metadata meta, inout standard_metadata_t sm) { apply { } }
control MyEgress(inout headers hdr, inout metadata meta, inout standard_metadata_t sm) { apply { } }
control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply { } }
control MyDeparser(packet_out packet, in headers hdr) { apply { } }
V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(), MyComputeChecksum(), MyDeparser()) main;
""")
    with pytest.raises(ValueError, match="no extraction tags"):
        p4_to_pir.generate_pir(str(prog), 256, str(tmp_path / "o"), 64)


@pytest.mark.skipif(not (HAVE_P4C and HAVE_VERILATOR), reason="p4c/verilator missing")
@pytest.mark.parametrize("prog", ["simple_parser", "full_parser"])
def test_verilator_p4_form_b(prog, tmp_path):
    mw = {"simple_parser": 768, "full_parser": 896}[prog]
    pir_path, json_path, _ = p4_to_pir.generate_pir(os.path.join(ROOT, "p4", prog + ".p4"), mw, str(tmp_path / "p4"), 64)
    out = tmp_path / "o"
    d, st, _ = run.generate(pir_path, json_path, str(out), None, packets=2000)
    assert st["model_mismatches"] == 0 and st["form"] == "B"
    ok, mism, _b, _r, log = run.simulate(str(out))
    assert ok and mism == 0, log[-2000:]
    assert (out / "extraction.sv").exists() and (out / "parser_top.sv").exists()
