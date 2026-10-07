# pir_opt notes (step 1: parse, atomize, golden evaluator)

Run from `engine/`: `python3 -m pytest pir_opt -q`, `python3 -m pir_opt.show_atoms pir_opt/bench/mixed_hi.hex pir_opt/bench/mixed_hi.json`,
regenerate bench: `python3 -m pir_opt.bench.make_mixed_hi`.

## Priority semantics: FIRST ROW IN FILE ORDER WINS (chosen; PTP RTL is only partly specified)

Evidence:
- README.md:76-93 (worked example): comparison stage is `if ... else if ... else if`, lookup stage likewise,
  ids assigned in row order -> earlier row shadows later ones.
- `engine/tools/build_rtl_countluts.py:151-` `extract_segments_and_tokens`: segment ids are handed out in
  first-appearance (file) order per column; `build_comparison_rtl` (:503, templates `comparison_rtl_tmpl.sv` /
  `_tree.sv`) emits conditions in that order as an else-if chain (`comparison_rtl_tmpl.sv:43-50`,
  `emit_tree` in `comparison_rtl_tmpl_tree.sv`), so within a column the first matching segment wins.
- `build_rtl_countluts.py:327-` `build_lookup_rtl`: rows keyed by pseudo-id string in a dict
  (`row_seg_id_dict[final_seg_id] = value`, :362) -> for identical keys the LAST row overwrites.
  `build_lookup_rtl_tree` (:257) builds a trie (`insert_nested`) rendered by
  `lookup_rtl_nested_ifelse_tmpl.sv` as if/else-if per level (first key in insertion order wins, no
  backtracking); `__skip__` (wildcard column) subtrees are emitted as separate sequential `if` blocks inside
  `always_comb`, so there the LAST assignment wins (this is the "sequential if, last wins" the plan mentions).

Surprises / caveats:
- The RTL's actual priority is a function of the column partitioning (`refine_char_per_col`, utils.py:354) and
  of which backend flags are used (`--tree*`, `--unique*`, flat); it is not a clean per-row rule. The tool
  largely assumes rows are meant to be mutually exclusive (see the comments in `select_tb_lines`).
- The flat lookup template `lookup_rtl_tmpl.sv` referenced at build_rtl_countluts.py:397 is NOT in
  `engine/templates/` (only `_unique` and `nested_ifelse` exist), so the default flat lookup cannot render.
- Hence the golden model uses the simple, well-defined rule above. If the RTL backend later needs to be
  matched bit-exactly on overlapping rows, this must be revisited (the optimizer must at minimum preserve
  first-row-wins; it may assume non-overlapping rows are order-independent).
- The bench is designed so first-wins is unambiguous: TCP/UDP/IPv6 rows precede the `hi` rows.

## Other decisions
- Short packets: byte granularity (a packet of n bytes provides bytes 0..n-1; a row constraining any nibble at
  byte >= n does not match). Real hardware may only distinguish per flit/tkeep; revisit if needed.
- A fully-wildcard row matches everything (empty atom set, empty shape, anchor None).
- Form-B rows (`name:start:end`) are parsed, flagged `is_extraction`, skipped in matching/atomization. Their
  file-order index is still counted in `PirRow.index`.
- Non-hex/non-x tokens in a pattern raise NotImplementedError (custom tokens out of scope).
- Does not import `engine/utils/utils.py` (it imports jinja2 at module load); nothing needed from it.
- Python 3.9 compatible (`from __future__ import annotations`).

# Step 2: time-bound plan, baseline model, cycle model, if/else dumps

Run from `engine/`: `python3 -m pir_opt.run_flow pir_opt/bench/mixed_hi.hex pir_opt/bench/mixed_hi.json`
(`--packet HEX` dynamic trace, `--expand`, `--static`, `--check N --seed S`, `--plan-json P`).
Files: `baseline.py` (PTP-style model), `plan.py` (optimizer + `plan_to_json`), `cycle_model.py`
(flit-by-flit executors for both), `ifelse_dump.py` (static/dynamic/summary views), `vectors.py`, `run_flow.py`,
`tests/test_plan.py`.

## What the optimizer found on `mixed_hi` (8 B flits, T=6, 26 rows)
| metric | baseline | optimized |
|---|---|---|
| comparators | 52 | 22 (6 header + 16 `h`/`i` lanes) |
| AND consumers (units / gates) | 28 gates | 10 units (8 `and_f0_l*`, 2 shared EtherType) / 18 gates |
| FFs | 30 partial-match latches | 5 = 3 alive + 1 sticky + 1 carry |
| unit evals per full packet | 82 | 77 (comparator evals identical: 52; the saving is area, not activity) |
| EOP if/else chain | 26 | 4 |
| loops (self edges d=1) | 0 | 4 (3 alive + `found_f0`) |

* `hi` family (23 anchors) -> 8 lane consumers; lanes 0..6 have schedule {3,4,5}; lane 7 has anchors 31, 39
  only (47 is not an anchor) -> schedule {4,5} and the single `d=1` carry edge `cmp_l7_68 -> and_hi_l7`
  (one register `cmp_l7_68_d1`). `cmp_l7_68` is scheduled {3,4} = consumer schedule - 1.
* Rows 0/1 (IPv4 TCP/UDP) share one EtherType AND consumer (CSE across rows, schedule {1}).

## Design decisions
* Delay of an input = flits between that byte and the shape's LAST byte: `d = (b+last)//B - (b+off)//B`
  (not `(b+off)//B`, which would delay the wrong byte). Consumer schedule = flit in which the last byte arrives.
* Comparator schedule = every flit where its output is read directly, or captured for a delay edge (`t-d`).
  The cycle model evaluates a comparator ONLY in its scheduled flits and a read outside the schedule raises
  `ModelError`; evaluating a unit twice in one cycle also raises. So schedules are proven complete and reuse is real.
* Anchored rows: one-hot `alive` bit (set at SOP, `alive &= terms` in flits with atoms), behind `AliveBit` so an
  encoded-state pass can replace it. Needs `reach` (EOP flit >= flit of last constrained byte) because a flit that
  never arrives cannot clear `alive`; family sticky bits do not need it (last byte is valid-gated).
* EOP stage reads the post-update (next-state) values of the EOP flit (combinational tail; no extra cycle modelled).
* Non-contiguous priority: a family is split into maximal priority-contiguous segments (no row with a different id
  strictly between its rows); each segment with >= 2 anchors is folded with priority = its best row, a
  single-anchor segment becomes an anchored row. Exact duplicate rows are dropped from the plan only.
* Baseline: one comparator per atom (t,lane,mask,value), one latch per (row, flit-with-atoms), EOP AND + chain.
* Structural gate/input counts are estimates (AND inputs include per-row update gates and EOP `alive && reach`).

## Limitations / open issues
* One-hot alive bits only; no cross-row sharing of alive prefixes (rows 0/1 could share an `0800` state) and no
  encoded-state minimization (Phase 3). Alive FFs grow with the number of unfolded rows.
* Folding needs >= 2 anchors with the same (id, shape); partially overlapping families with different shapes are not
  merged. Anchors in the same family but different lanes always get separate consumers (no lane-bank sharing
  beyond comparators).
* Byte-granular short-packet model (valid per lane); real tkeep/EOP timing and packets longer than the window
  (state saturates after flit T-1) are only modelled by truncation to the window.
* Loops are only flagged, not timed; Fmax of the sticky OR over many consumers is not estimated.
* The comparator "enable" is implicit (comparators evaluate every cycle in RTL; schedule = where consumed), only the
  AND consumers and state updates are really gated.
* No custom tokens / ranges / extraction rows; no LUT/FF cost model yet. Vectors are random + directed, not exhaustive.

# Step 3: generic names + overlapping bench (`mixed_hi_overlap`)

* Names are structure-derived only: `alive_r<row>`, `and_r<row>_t<t>`, folded families `f<k>` (k = rank of the
  segment's best row in priority) -> `found_f<k>`, `and_f<k>_l<lane>`; comparators `cmp_l<lane>_<hex>` with nibble masks
  as `6x` / `x5` (any other mask: `<value>m<mask>`); carry copies `_d<k>`. The dump lists the covered rows in a comment:
  `reg found_f0;  // family f0 = rows r3..r25, path_id 4'h4, sticky, ...`. `found_` != `alive_`: found is set by any hit,
  alive is cleared by a mismatch. No pattern ASCII reaches identifiers (the old `_family_name` was removed).
* Bench: `python3 -m pir_opt.bench.make_mixed_hi --overlap` -> `bench/mixed_hi_overlap.{hex,json}` (mixed_hi untouched).
  r0 + bytes 34-35 = 0050; r1 + bytes 36-37 = 0035 and byte 26 = 68; r2 + bytes 30,31 = 69,68 and byte 40 = `6x`.
* `vectors.random_vectors` additionally re-plants a randomly chosen row (complete, or near-miss with one constrained byte
  randomised) from the row's own shape (independent RNG stream), so headers with bytes inside the hi region occur often
  (ids 1/2/3 ~130 each in 3000 packets).
* Result on the overlap bench: 57 -> 27 comparators, 35 -> 5 FFs, 0 mismatches. Comparators shared between alive rows and
  family f0: `cmp_l2_68` (r1 t=3), `cmp_l6_69`, `cmp_l7_68` (r2 t=3, via `and_r2_t3`). `cmp_l0_6x` (r2 t=5) is NOT shared
  (different mask from the hi bank's `cmp_l0_68`). Lane-bank sharing of consumers (AND units) between rows and the family
  is not attempted: they have different shapes.

# Step 4: solver / library formulation (ARCHIVED → `archive/solver/`, `archive/SOLVER.md`; not part of the active flow)

Run from `engine/` with the venv (`pir_opt/.venv/bin/python`): `-m pir_opt.solver.compare <hex> <json> [--check N] [--detail]`;
tests `tests/test_solver.py` (skipped without `dd`/`pyeda`; system `python3 -m pytest pir_opt` still gives 51 passed).
* `classes.py` lane value classes (union of predicates over all flits; absent byte == all-false class 0, so no length variable),
  `tdd.py` MDD (pure Python) + `dd.autoref` BDD, per-boundary Myhill-Nerode state counts (BDD == MDD on all benches),
  `machine.py` derived Moore machine (binary / one-hot / one-hot-with-implicit-zero), Espresso via `logic.py`, folding detection,
  `compare.py` common metrics for baseline / hand plan / solver. New benches: `bench/make_synthetic.py` (abc_anywhere, non_contiguous, abc_1byte).
* Result: mixed_hi states `[1,3,4,6,6,5]` -> 3 FFs binary vs hand plan 5; same comparator set and per-cycle reads as the hand plan;
  but two-level next-state logic 2.5x (binary) to 60x (one-hot) larger because minimal states are mutually exclusive. 0 mismatches everywhere.
* Caps: MDD node cap, Espresso 3000 cubes / 120k literals, path cap 40000, chain_cover 1500 cubes, SIGALRM guards (20-30 s) on pure-Python steps.
