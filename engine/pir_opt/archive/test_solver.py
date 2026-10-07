"""Tests for the solver/ package (skipped when dd / pyeda are not installed)."""
import os

import pytest

pytest.importorskip("dd")
pytest.importorskip("pyeda")

from pir_opt.atoms import atomize
from pir_opt.pir import load_pir
from pir_opt.solver import compare
from pir_opt.solver.classes import build_lane_classes
from pir_opt.solver.tdd import build_bdd_states, build_tdd, count_states_explicit

BENCH = os.path.join(os.path.dirname(__file__), "..", "bench")


def paths(name):
    return os.path.join(BENCH, name + ".hex"), os.path.join(BENCH, name + ".json")


def load(name):
    rows, cfg = load_pir(*paths(name))
    return rows, cfg, atomize(rows, cfg)


def test_class_partition_overlap():
    _, _, ap = load("mixed_hi_overlap")
    lanes = build_lane_classes(ap)
    l0 = lanes[0]                               # predicates 68, 69, 6x
    assert len(l0.classes) == 4
    assert l0.classes[0].label == "rest" and 0x68 not in l0.classes[0].values
    by = {c.label: set(c.values) for c in l0.classes}
    assert by["68"] == {0x68} and by["69"] == {0x69}
    assert by["6x*"] == set(range(0x60, 0x70)) - {0x68, 0x69}
    assert sum(len(c.values) for c in l0.classes) == 256
    assert all(l0.class_of[v] == c.cid for c in l0.classes for v in c.values)


def test_states_bdd_equals_explicit_mixed_hi():
    rows, cfg, ap = load("mixed_hi")
    idw = max(r.id_width for r in rows)
    tdd = build_tdd(ap, idw)
    explicit = count_states_explicit(tdd)
    assert build_bdd_states(ap, idw).states == explicit == [1, 3, 4, 6, 6, 5]


@pytest.mark.parametrize("name", ["mixed_hi_overlap", "non_contiguous", "abc_1byte"])
def test_states_bdd_equals_explicit_other(name):
    rows, cfg, ap = load(name)
    idw = max(r.id_width for r in rows)
    assert build_bdd_states(ap, idw).states == count_states_explicit(build_tdd(ap, idw))


@pytest.mark.parametrize("name", ["mixed_hi", "mixed_hi_overlap", "non_contiguous", "abc_anywhere"])
def test_zero_mismatches(name):
    br = compare.run_bench(*paths(name), n_check=500, seed=3, fold=False)
    assert br.table_mismatches == 0
    assert [m.mismatches for m in br.metrics] == [0] * 5
    assert br.packets >= 500


def test_mixed_hi_structure_and_table():
    br = compare.run_bench(*paths("mixed_hi"), n_check=200, fold=True)
    base, plan, sb, so, sz = br.metrics
    assert (base.n_cmp, plan.n_cmp, sb.n_cmp) == (52, 22, 22)
    assert sb.state_bits == [0, 2, 2, 3, 3, 3] and plan.ffs_alloc == 5 and sb.ffs_alloc == 3
    assert so.state_bits == br.states_mdd[:0] + [0, 3, 4, 6, 6, 5]
    tab = compare.format_table(br)
    for col in ("baseline", "hand plan", "solver(binary)", "solver(one-hot)"):
        assert col in tab
    assert br.folding[(3, 4)] == "embeds"


def test_folding_iso_on_one_byte_flits():
    br = compare.run_bench(*paths("abc_1byte"), n_check=100, fold=True)
    assert br.folding[(4, 9)] == "iso" and br.folding[(3, 13)] == "iso"


def test_cli(capsys):
    h, j = paths("non_contiguous")
    assert compare.main([h, j, "--check", "100"]) == 0
    assert "solver(binary)" in capsys.readouterr().out
