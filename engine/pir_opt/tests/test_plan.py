"""Tests for plan.py / baseline.py / cycle_model.py / ifelse_dump.py / run_flow.py."""
import json
import os
import random

import pytest

from pir_opt.atoms import atomize
from pir_opt.baseline import build_baseline
from pir_opt.cycle_model import ModelError, packet_to_flits, run_baseline, run_plan
from pir_opt.golden import eval_pir
from pir_opt.ifelse_dump import format_dynamic, format_static_baseline, format_static_plan, format_summary
from pir_opt.pir import PirConfig, load_pir, parse_pir_lines
from pir_opt.plan import build_plan, plan_to_json
from pir_opt.run_flow import check_equivalence, main as run_flow_main
from pir_opt.vectors import bench_directed, directed_vectors, random_vectors

BENCH = os.path.join(os.path.dirname(__file__), "..", "bench")
HEX, JSON = os.path.join(BENCH, "mixed_hi.hex"), os.path.join(BENCH, "mixed_hi.json")


def pat(cfg, fields):
    nib = ["x"] * (2 * cfg.max_bytes)
    for pos, hx in fields.items():
        for i, ch in enumerate(hx):
            nib[2 * pos + i] = ch
    return "".join(nib)


def build(lines, cfg):
    rows = parse_pir_lines(lines, cfg)
    ap = atomize(rows, cfg)
    return rows, ap, build_plan(ap, rows), build_baseline(ap, rows)


@pytest.fixture(scope="module")
def bench():
    rows, cfg = load_pir(HEX, JSON)
    ap = atomize(rows, cfg)
    return rows, cfg, ap, build_plan(ap, rows), build_baseline(ap, rows)


def full_check(rows, cfg, ap, plan, bp, n=3000, seed=7):
    r = check_equivalence(rows, cfg, ap, plan, bp, n, seed)
    assert r["plan_mismatch"] == 0 and r["baseline_mismatch"] == 0, r["failures"]
    assert r["max_evals_per_unit"] <= 1
    return r


# ------------------------------------------------------------------ bench
def test_unit_counts(bench):
    _, _, ap, plan, bp = bench
    c = plan.counts()
    assert bp.counts()["comparators"] == 52 == len(ap.atoms)
    assert c["comparators"] == 22 == len(plan.cmp_units)
    assert c["ffs"] == 5 and bp.counts()["ffs"] == 30


def test_hi_family_consumers(bench):
    _, _, _, plan, _ = bench
    assert len(plan.families) == 1
    fam = plan.families[0]
    assert fam.name == "f0" and fam.anchors == tuple(range(24, 47)) and fam.carry_bytes == 1
    assert len(fam.consumers) == 8                       # one per anchor lane 0..7
    sched = {c: plan.and_units[c].schedule for c in fam.consumers}
    for b in range(7):
        assert sched[f"and_f0_l{b}"] == (3, 4, 5)
    # lane 7 anchors are bytes 31, 39 (47 is not an anchor): last byte in flits 4, 5
    assert sched["and_f0_l7"] == (4, 5)
    u7 = plan.and_units["and_f0_l7"]
    assert [(r.unit, r.delay) for r in u7.inputs] == [("cmp_l7_68", 1), ("cmp_l0_69", 0)]
    for b in range(7):
        assert all(r.delay == 0 for r in plan.and_units[f"and_f0_l{b}"].inputs)
    carries = [e for e in plan.edges if e.kind == "carry"]
    assert len(carries) == 1 and (carries[0].src, carries[0].dst) == ("cmp_l7_68", "and_f0_l7")
    assert list(plan.delay_regs) == ["cmp_l7_68_d1"]
    assert plan.cmp_units["cmp_l7_68"].schedule == (3, 4)    # producer sched = consumer sched - 1


def test_sticky_loop_and_alive(bench):
    _, _, _, plan, _ = bench
    loops = [e for e in plan.edges if e.loop]
    assert ("found_f0", "found_f0", 1) in [(e.src, e.dst, e.delay) for e in loops]
    assert plan.stickies["found_f0"].loop and plan.stickies["found_f0"].schedule == (3, 4, 5)
    assert sorted(plan.alive) == ["alive_r0", "alive_r1", "alive_r2"]
    # header rows 0/1 share the EtherType AND consumer
    assert plan.alive["alive_r0"].terms[1] == plan.alive["alive_r1"].terms[1]
    assert [o.kind for o in plan.output] == ["row", "row", "row", "family"]


def test_each_unit_evaluated_at_most_once_per_cycle(bench):
    rows, cfg, ap, plan, bp = bench
    for p in bench_directed() + random_vectors(ap, 300, 1):
        r = run_plan(plan, p)
        assert r.max_evals_per_unit <= 1
        assert run_baseline(bp, p).max_evals_per_unit <= 1


def test_eval_counts_match_static(bench):
    rows, cfg, ap, plan, bp = bench
    full = bytes(48)
    assert run_plan(plan, full).total_evals == plan.counts()["evals_per_packet"]
    assert run_baseline(bp, full).total_evals == bp.counts()["evals_per_packet"]


def test_model_detects_double_eval(bench):
    _, _, _, plan, _ = bench
    from pir_opt.cycle_model import _Evaluator
    ev = _Evaluator()
    ev.evaluate("x", True)
    with pytest.raises(ModelError):
        ev.evaluate("x", True)


def test_model_detects_bad_schedule():
    cfg = PirConfig(64, 128)
    _, ap, plan, _ = build([pat(cfg, {3: "aa", 12: "bb"}) + " 4'h1\n"], cfg)[0:4]
    plan.cmp_units["cmp_l4_bb"].schedule = ()      # drop the schedule -> unscheduled read
    with pytest.raises(ModelError):
        run_plan(plan, bytes(16))


def test_zero_mismatches_bench(bench):
    rows, cfg, ap, plan, bp = bench
    r = full_check(rows, cfg, ap, plan, bp, 3000, 0)
    assert r["packets"] >= 3000
    assert all(r["hist"].get(k, 0) > 50 for k in (0, 1, 2, 3, 4)), r["hist"]


def test_straddle_and_short_packets(bench):
    rows, cfg, ap, plan, bp = bench
    for k in (31, 39, 46, 47, 24):
        for n in (k + 1, k + 2, k + 3, 48):
            p = bytearray(b"\x41" * n)
            for i, c in enumerate(b"hi"):
                if k + i < n:
                    p[k + i] = c
            p = bytes(p)
            assert run_plan(plan, p).path_id == eval_pir(rows, cfg, p) == run_baseline(bp, p).path_id


def test_dynamic_trace_straddle(bench):
    rows, cfg, ap, plan, bp = bench
    p = bytearray(b"\x41" * 48)
    p[12:14] = b"\x08\x00"
    p[23] = 0x11
    p[31:33] = b"hi"
    out = format_dynamic(plan, bp, rows, bytes(p))
    assert "golden=2  plan=2  baseline=2" in out and "OK" in out
    r = run_plan(plan, bytes(p))
    t3, t4 = r.trace[3], r.trace[4]
    assert "cmp_l7_68" in t3.cmp_true and t3.state["found_f0"] == 0
    assert "and_f0_l7" in [c for c, v in t4.consumers if v] and t4.state["found_f0"] == 1


def test_static_dumps_and_json(bench, tmp_path):
    rows, cfg, ap, plan, bp = bench
    s = format_static_plan(plan)
    assert "// t=3:" in s and "(cmp_l7_68_d1 && cmp_l0_69)" in s
    assert "// t=4..5:" not in format_static_plan(plan, expand=True)
    assert "if (cmp_l4_08 && cmp_l5_00) alive_r0 <= alive_r0; else alive_r0 <= 1'b0;" in s
    assert "else if (found_f0) path_id = 4'h4;" in s
    assert "52" in format_summary(plan, bp) and "22" in format_summary(plan, bp)
    assert format_static_baseline(bp).count("wire cmp_t") == 52
    j = plan_to_json(plan)
    json.dumps(j)
    assert {e["delay"] for e in j["edges"]} == {0, 1}
    assert j["schedule"]["and_f0_l7"] == [4, 5]
    assert sum(1 for u in j["units"] if u["kind"] == "cmp") == 22


def test_cli(tmp_path, capsys):
    out = tmp_path / "plan.json"
    rc = run_flow_main([HEX, JSON, "--check", "200", "--plan-json", str(out)])
    assert rc == 0 and json.load(open(out))["config"]["num_flits"] == 6
    rc = run_flow_main([HEX, JSON, "--check", "0", "--packet", "00" * 48])
    assert rc == 0 and "RESULT golden=0" in capsys.readouterr().out


# ------------------------------------------------------- synthetic PIR #2
def test_abc_anywhere_plus_three_flit_row():
    cfg = PirConfig(64, 256)  # 8 B flits, 32 B window
    lines = [pat(cfg, {3: "aa", 12: "bb", 21: "cc"}) + " 4'h1\n"]       # anchored, spans 3 flits
    lines += [pat(cfg, {k: "616263"}) + " 4'h5\n" for k in range(0, 30)]  # "abc" anywhere
    rows, ap, plan, bp = build(lines, cfg)
    c = plan.counts()
    fam = plan.families[0]
    assert fam.name == "f0" and len(plan.families) == 1 and fam.carry_bytes == 2
    assert len(fam.consumers) == 8
    # lanes whose 3 bytes cross a flit boundary: anchor lanes 6 (a,b | c) and 7 (a | b,c)
    d = {u.lane: [r.delay for r in u.inputs] for u in map(plan.and_units.get, fam.consumers)}
    assert d[6] == [1, 1, 0] and d[7] == [1, 0, 0]
    assert all(d[b] == [0, 0, 0] for b in range(6))
    assert c["carry_edges"] == 3 and c["carry_ffs"] == 3
    assert plan.counts()["comparators"] < plan.counts()["baseline_comparators"]
    assert sorted(plan.alive) == ["alive_r0"] and sorted(plan.alive["alive_r0"].terms) == [0, 1, 2]
    assert plan.alive["alive_r0"].reach == 2
    full_check(rows, cfg, ap, plan, bp, 3000, 3)
    # exact-boundary cases
    for k in (5, 6, 7, 8, 13, 14, 29):
        for n in (k + 2, k + 3, 32):
            p = bytearray(b"\x20" * n)
            p[k:k + 3] = b"abc"[:max(0, min(3, n - k))]
            assert run_plan(plan, bytes(p)).path_id == eval_pir(rows, cfg, bytes(p)), (k, n)


def test_delay_two_flits():
    # 1-byte flits: a 3-byte literal gives d=2 chains
    cfg = PirConfig(8, 128)
    lines = [pat(cfg, {k: "616263"}) + " 4'h2\n" for k in range(0, 14)]
    rows, ap, plan, bp = build(lines, cfg)
    assert plan.families and set(plan.delay_regs) == {"cmp_l0_61_d1", "cmp_l0_61_d2", "cmp_l0_62_d1"}
    full_check(rows, cfg, ap, plan, bp, 500, 4)


def test_non_contiguous_priority():
    cfg = PirConfig(64, 128)
    lines = [pat(cfg, {2: "6869"}) + " 4'h4\n",     # r0  hi@2
             pat(cfg, {2: "68"}) + " 4'h7\n",       # r1  foreign id between
             pat(cfg, {5: "6869"}) + " 4'h4\n",     # r2  hi@5
             pat(cfg, {9: "6869"}) + " 4'h4\n",     # r3  hi@9
             pat(cfg, {11: "6869"}) + " 4'h4\n"]    # r4  hi@11
    rows, ap, plan, bp = build(lines, cfg)
    # split: {r0} (single anchor -> anchored row), {r2,r3,r4} folded
    assert len(plan.families) == 1 and plan.families[0].anchors == (5, 9, 11)
    assert plan.families[0].priority == 2 and "alive_r0" in plan.alive
    assert [o.priority for o in plan.output] == [0, 1, 2]
    assert plan.notes
    p = bytearray(b"\x20" * 16)
    p[2] = 0x68
    p[5:7] = b"hi"
    assert eval_pir(rows, cfg, bytes(p)) == 7           # r1 beats r2
    assert run_plan(plan, bytes(p)).path_id == 7
    full_check(rows, cfg, ap, plan, bp, 3000, 5)


def test_wildcard_nibbles_and_dups():
    cfg = PirConfig(64, 128)
    lines = [pat(cfg, {1: "6x", 9: "x9"}) + " 4'h1\n",
             pat(cfg, {1: "6x", 9: "x9"}) + " 4'h1\n",                # exact duplicate
             "x" * 32 + " 4'h3\n"]                                    # wildcard row
    rows, ap, plan, bp = build(lines, cfg)
    assert [o.kind for o in plan.output] == ["row", "always"]
    full_check(rows, cfg, ap, plan, bp, 1000, 6)
    assert run_plan(plan, b"").path_id == eval_pir(rows, cfg, b"") == 3
    assert run_plan(plan, b"\x61").path_id == 3


# ------------------------------------------------------- naming (generic)
def test_names_are_structural(bench):
    import re
    _, _, _, plan, bp = bench
    ids = (list(plan.cmp_units) + list(plan.and_units) + list(plan.stickies) + list(plan.alive)
           + list(plan.delay_regs))
    ok = re.compile(r"^(cmp_l\d+_[0-9a-fx]{2}(m[0-9a-f]{2})?(_d\d+)?|and_f\d+_l\d+|and_r\d+_t\d+|"
                    r"found_f\d+|alive_r\d+)$")
    assert all(ok.match(i) for i in ids), [i for i in ids if not ok.match(i)]
    s = format_static_plan(plan)
    assert "family f0 = rows r3..r25, path_id 4'h4, sticky" in s


def test_nibble_mask_comparator_names():
    cfg = PirConfig(64, 128)
    _, ap, plan, _ = build([pat(cfg, {1: "6x", 9: "x9"}) + " 4'h1\n"], cfg)[0:4]
    assert {"cmp_l1_6x", "cmp_l1_x9"} == set(plan.cmp_units)


# ---------------------------------------------------------- overlap bench
OHEX, OJSON = os.path.join(BENCH, "mixed_hi_overlap.hex"), os.path.join(BENCH, "mixed_hi_overlap.json")


@pytest.fixture(scope="module")
def ovl():
    rows, cfg = load_pir(OHEX, OJSON)
    ap = atomize(rows, cfg)
    return rows, cfg, ap, build_plan(ap, rows), build_baseline(ap, rows)


def test_overlap_unit_counts(ovl):
    _, _, ap, plan, bp = ovl
    c = plan.counts()
    assert len(ap.atoms) == bp.counts()["comparators"] == 57
    assert c["comparators"] == len(plan.cmp_units) == 27
    assert c["ffs"] == 5 and bp.counts()["ffs"] == 35
    assert len(plan.families) == 1 and plan.families[0].name == "f0"
    assert plan.families[0].anchors == tuple(range(24, 47))
    assert sorted(plan.alive) == ["alive_r0", "alive_r1", "alive_r2"]
    assert [o.kind for o in plan.output] == ["row", "row", "row", "family"]


def shared_cmp_units(plan):
    fam = {r.unit for c in plan.families[0].consumers for r in plan.and_units[c].inputs}
    alive = set()
    for a in plan.alive.values():
        for srcs in a.terms.values():
            for s in srcs:
                alive |= {s} if s in plan.cmp_units else {r.unit for r in plan.and_units[s].inputs}
    return sorted(fam & alive)


def test_overlap_shared_comparators(ovl):
    _, _, _, plan, _ = ovl
    assert shared_cmp_units(plan) == ["cmp_l2_68", "cmp_l6_69", "cmp_l7_68"]
    # the nibble-masked comparator (flit 5 lane 0) is a distinct unit from the hi bank's cmp_l0_68
    assert "cmp_l0_6x" in plan.cmp_units and plan.cmp_units["cmp_l0_6x"].schedule == (5,)
    assert 3 in plan.cmp_units["cmp_l2_68"].schedule
    assert plan.cmp_units["cmp_l7_68"].schedule == (3, 4)


def test_overlap_alive_rows_have_per_cycle_consumers(ovl):
    _, _, _, plan, _ = ovl
    a = plan.alive
    assert sorted(a["alive_r0"].terms) == [1, 2, 4] and a["alive_r0"].reach == 4
    assert sorted(a["alive_r1"].terms) == [1, 2, 3, 4] and a["alive_r1"].reach == 4
    assert sorted(a["alive_r2"].terms) == [1, 3, 5] and a["alive_r2"].reach == 5
    assert a["alive_r1"].terms[3] == ("cmp_l2_68",)
    assert a["alive_r2"].terms[3] == ("and_r2_t3",)
    assert [(r.unit, r.delay) for r in plan.and_units["and_r2_t3"].inputs] == [("cmp_l6_69", 0), ("cmp_l7_68", 0)]
    assert a["alive_r2"].terms[5] == ("cmp_l0_6x",)
    assert a["alive_r0"].terms[4] == ("and_r0_t4",) and a["alive_r1"].terms[4] == ("and_r1_t4",)
    s = format_static_plan(plan, expand=True)
    assert "if (cmp_l2_68) alive_r1 <= alive_r1; else alive_r1 <= 1'b0;" in s
    assert "if (cmp_l6_69 && cmp_l7_68) alive_r2 <= alive_r2; else alive_r2 <= 1'b0;" in s
    assert "if (cmp_l0_6x) alive_r2 <= alive_r2; else alive_r2 <= 1'b0;" in s


def test_overlap_zero_mismatches(ovl):
    rows, cfg, ap, plan, bp = ovl
    r = full_check(rows, cfg, ap, plan, bp, 3000, 0)
    assert r["packets"] >= 3000
    assert all(r["hist"].get(k, 0) > 50 for k in (0, 1, 2, 3, 4)), r["hist"]
    full_check(rows, cfg, ap, plan, bp, 3000, 11)


def test_overlap_directed_r1_over_hi(ovl):
    rows, cfg, ap, plan, bp = ovl
    p = bytearray(b"\x41" * 48)
    p[12:14] = b"\x08\x00"
    p[23] = 0x11
    p[26:28] = b"hi"                    # 'h' doubles as r1's byte 26, and "hi" is a family hit
    p[36:38] = b"\x00\x35"
    p = bytes(p)
    assert eval_pir(rows, cfg, p) == 2 == run_baseline(bp, p).path_id
    r = run_plan(plan, p)
    assert r.path_id == 2 and r.trace[-1].state["found_f0"] == 1 and r.trace[-1].state["alive_r1"] == 1
    q = bytearray(p)                    # without the UDP port bytes the family wins
    q[37] = 0
    assert run_plan(plan, bytes(q)).path_id == eval_pir(rows, cfg, bytes(q)) == 4
    v6 = bytearray(b"\x41" * 48)        # IPv6, straddling 0x69,0x68 at 30,31, byte 40 = 6x
    v6[12:14] = b"\x86\xdd"
    v6[30], v6[31], v6[40] = 0x69, 0x68, 0x6f
    assert run_plan(plan, bytes(v6)).path_id == eval_pir(rows, cfg, bytes(v6)) == 3
    v6[40] = 0x70
    assert run_plan(plan, bytes(v6)).path_id == eval_pir(rows, cfg, bytes(v6)) == 0   # 'ih' is not 'hi'
