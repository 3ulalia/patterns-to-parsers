# PIR Optimizer — Plan

Audience: an engineer or agent picking this up cold. Read **§0 first**. §1–§9 below are the original
background plan; where they disagree with §0, §0 wins.

---

## 0. Status (2026-10-06) and next-session plan

### 0.1 Where we are

The Python prototype lives in `engine/pir_opt/`. Nothing is committed yet. See `engine/pir_opt/NOTES.md` for details.

- **Parsing and atoms.** `pir.py` and `atoms.py` parse `.hex` + JSON. Every row is turned into ID-free byte **atoms**
  `(t, lane, mask, value)`. Rows are grouped into **families** by `(path_id, shape)`, each with a set of anchor positions.
- **Golden evaluator.** `golden.py` treats priority as *first row in file order wins* (a documented choice; the RTL
  depends on the lookup mode). Short packets are handled at byte granularity.
- **Chosen design.** `plan.py` implements the design we are keeping:
  - **Shared comparators**: one comparator per `(lane, mask, value)`, reused in every flit, with a schedule of the flits
    where it is used.
  - **Alive rows**: one bit per anchored row, set at SOP and cleared by any flit that mismatches.
  - **Folded families**: one AND per lane, enabled by the schedule, feeding a sticky `found_f<k>` bit, plus delay
    registers that carry a byte across a flit boundary.
  - The final lookup shrinks to a small EOP **priority encoder** over the alive and found bits.
- **Models and output.**
  - `baseline.py` models today's PTP structure, so we can compare against it.
  - `cycle_model.py` simulates both designs flit by flit and asserts that no unit is evaluated twice in one cycle.
  - `ifelse_dump.py` / `run_flow.py` print the if/else flow per cycle, plus a trace for a single packet.
- **Results.** Both models have 0 mismatches against golden.

  | bench | comparators (baseline → plan) | FFs (baseline → plan) | EOP chain (baseline → plan) |
  |---|---|---|---|
  | `mixed_hi` | 52 → 22 | 30 → 5 | 26 → 4 |
  | `mixed_hi_overlap` | 57 → 27 | 35 → 5 | — |
- **Tests.** `cd engine && python3 -m pytest pir_opt -q` gives 51 passed.
- **Archived.** `engine/pir_opt/archive/` holds two parked experiments, kept for reference only:
  - **Solver / decision-diagram formulation.** It confirmed that the hand plan's comparator set is minimal. Minimal
    state saved only 1–2 FFs and made the next-state logic 2.6–95× larger, so we don't use it.
  - **ABC/BLIF export.** Never finished.
- **Decisions.**
  - Keep **alive rows + shared atoms**.
  - Measure gains in software (comparators, state bits, literals) before any LUT counts.
  - Use Sonnet subagents for the coding.

### 0.2 Next-session plan (in order)

1. **Pseudo-time: split a pattern across cycles for timing, even when all its bytes are in one flit.**
   - Today the logical time of an atom is the flit it arrives in (`t = byte // B`). Add a **stage**: `s ≥ t`.
   - The scheduler may assign atoms of one row to later stages, so a wide AND becomes a chain of small per-cycle ANDs
     with an alive bit between them. The bits it needs later are registered, as delay edges with `d = s − t`.
   - This reuses the existing time model unchanged. A flit that arrives at `t` is consumed at `s`. Alive bits, sticky
     bits and the EOP encoder already handle multi-cycle rows.
   - **Inputs.** A per-stage budget, e.g. max AND inputs or LUT levels per cycle, and a max total latency.
   - **Outputs.** The stage of each atom, the delay registers, and the added EOP latency.
   - **Design choice to evaluate.** Delay the **1-bit comparator result** instead of the 8 raw data bits whenever the
     comparator can be evaluated at arrival. This is usually 8× fewer FFs. Delay the raw data only when the comparator
     itself must move to a later stage. Report both options.
   - **Check.** The cycle model must stay equivalent to golden for any legal schedule. Add a test that sweeps budgets
     on `mixed_hi_overlap`.
2. **Patterns wider than a flit: done.** A pattern longer than the flit is already split across flits by the
   space-time mapping, with carries and alive bits. No new work is needed. Item 1 generalises this.
3. **SystemVerilog templates for the optimized plan.**
   - New Jinja templates go in `engine/pir_opt/templates/`. Leave `engine/templates/` unchanged; the original PTP stays
     as the reference.
   - **Units to cover:**
     - the shared comparator bank (one `assign` per comparator unit, gated by valid);
     - delay/carry registers;
     - alive-bit registers with their per-flit update, selected by the flit index;
     - sticky family bits with their lane ANDs and schedule enables;
     - the EOP priority encoder;
     - a top module.
   - **Ports.** The top module must use the **same ports and handshake as `hex_to_rtl_top` / `parser_top`**, so the
     existing testbenches drop in unchanged.
   - **Emit from the plan IR.** Use `plan_to_json` as the source, not ad-hoc logic.
   - **Flit index mapping.** Match the RTL's flit counter: flit 0 is `sop`, and flit `i` is `flit_counter == i-1`;
     see §4.1.
4. **Simulate original PTP and optimized PTP for equivalence.**
   - **Benches.** Full and simple P4 parsers. Their generated RTL and self-checking testbenches are in
     `artifact_out/table1/{full,simple}_dw*_tree/` (`hex_to_rtl_top_tb.sv`, `parser_top_tb.sv`). Regenerate with
     `make p4_full` / `make p4_simple`.
   - **Simulators.** Verilator (Makefile flags: `--binary --sv --timing`) and Icarus. Both are installed in
     `~/oss-cad-suite/bin`.
   - **Steps:**
     - (a) Run the existing TBs against the original RTL as the reference.
     - (b) Run the same TBs against the optimized RTL.
     - (c) Run a differential TB: random and directed packets driven into both designs, comparing `path_id` per packet.
       Use the Python golden evaluator and `vectors.py` to make the vectors.
   - **Watch for:**
     - The priority semantics on overlapping rows (golden is first-row-wins; the RTL tree lookup may differ). Check
       whether the P4 rows are mutually exclusive.
     - Form-B extraction (`mask_extractor` selects fields using pseudo-IDs). Start with `path_id` only. Then drive the
       extractor select from the alive/found state.
   - **First step.** Get the Python plan running on the P4 PIRs (`engine/tmp/` after `p4_to_rtl.py`) and report
     comparators, alive bits per boundary and mismatches.
5. **Shorthand for floating patterns in the `.hex`.**
   - Today "hi" in bytes 24..47 needs 23 expanded rows. Add a compact notation that is parsed straight into a
     **family**, without expanding the rows.
   - Reuse the regex vocabulary already specified in `REGEX_TOKENS.md` (nibble-based terms and repetition).
     Candidate forms, to be decided:
     - a row-level `6869 @[24..46]` anchor range;
     - a regex token such as `.{48,92}6869` in nibbles.
   - Keep the expanded form valid. Both must produce the same atoms and families, so test expanded ≡ shorthand.
6. **Ordered patterns: "hi" after "ff" within a window.**
   - Example: `ff` followed by `6869` starting within N bytes after it (`ff .{0,N} hi`).
   - New state kind: a **distance-since-last-`ff`** register.
     - It is a saturating counter of `ceil(log2(N+1))` bits, or a one-hot shift register for small N.
     - It is updated per lane inside a flit and carried across flit boundaries.
     - A "hi" consumer at lane b checks `dist(b) ≤ N`.
   - Compare against the naive expansion (one row per (ff position, hi position) pair, which is O(N·window)), using
     the software metrics.
   - Cover these cases: `ff` and `hi` in the same flit, straddling a flit boundary, overlapping (`ff` inside the
     window of a previous `ff`), and N larger than the flit.

---

## 1. Why we are doing this

### 1.1 What PTP does today (verified against generated RTL)

- Every PIR row is a fixed-position pattern over the whole packet window (`max_width`).
- The backend (`engine/tools/build_rtl_countluts.py`, templates in `engine/templates/`)
  builds **one `comparison_logic_i` module per flit index**. All modules see the live flit
  every cycle; module *i*'s output (per-column pseudo-IDs) is latched only when
  `flit_counter == i-1` (`hex_to_rtl_top_tmpl.sv`).
- At EOP, `lookup_logic` folds **all** latched pseudo-IDs (a wide nested if/else) into one
  priority `path_id`.
- Consequences:
  - Cost scales with **packet length × distinct column values**, not data width
    (Table 1: full parser = 1,331 / 1,357 / 1,352 LUTs at 64 / 320 / 512-bit DW).
  - Each per-flit bank does useful work 1 cycle per packet.
  - A *floating* pattern ("`hi` anywhere") must be written as one row per offset, and every
    flit gets its own identical copy of the comparators. In a mixed PIR (floating + header
    rows) column refinement also splits header columns (e.g. 16-bit EtherType → two byte
    columns) and widens pseudo-IDs.
  - Largest evaluated case (fig9: 4,096 rows × 80 bits, `PatternToParser/new_results/fig9_results.csv`):
    35K–152K LUTs (decomposed), 285K–337K LUTs (flat), 166–282 MHz.

### 1.2 What RapidDetect (RD) does — the motivating workload

Repo: `../RapidDetect-RCC` (FCCM 2026 RCC). Sigma-rule threat detection on JSON system logs,
200 Gbps = 64 B/cycle @ 400 MHz on a Versal V80.

Pipeline:
1. **Field tagger** (`hardware/src/rapidd_stages.cpp`): 3 fixed markers (`"syscall": `,
   `"exe": `, `"path": `) matched at every byte position; a 2-bit tag (`FTAG`, `include/utils/types.h`)
   is painted on each byte until the closing quote, carried across flits.
2. **MSPM** (`hardware/src/mspm/mspm.cpp`), 64 positions × 8 length buckets (2..8 bytes; 8 has two buckets):
   - shift-or prefilter (table indexed by byte + 5 bits of next byte → 64-bit "can a length-L pattern end here" mask),
   - multiplicative hash of the last L bytes → 1 valid bit from a small bitmap table,
   - compactor (512 hit slots → few), then a sparse lookup: hash table → rule ID (overloaded IDs
     expanded via `collision_table.h`), 8-bit XOR tag check, and the field check
     `ftag_bmap & (1 << hit.ftag)` (mspm.cpp:666) — **field check happens on sparse hits, not per position**.
3. **CPM** (`hardware/src/nf.cpp`, 16 B/cycle, only on events that passed MSPM): all literals hashed into a
   128-bit per-event fingerprint; subset test against each candidate rule's fingerprint.
4. **Hyperscan on CPU** removes false positives (hashing + fingerprint are approximate).

Numbers extracted today from `hardware/include/tables/rid2sid_table.h` (comments list each rule's literals):

| Quantity | Value |
|---|---|
| Rule IDs (AND-terms / conjuncts of ~200 Sigma rules) | 4,348 |
| Literals per conjunct | avg 4.2, max 13 |
| Distinct (literal, field) | 1,571 (1,542 distinct literals, ≤ 8 B chunks, avg 7.17 B) |
| Distinct fast patterns (one per conjunct, matched at line rate) | 744 |
| Max conjuncts sharing one literal | 1,239 |
| Distinct byte values in literals | 98 (74 case-folded) |
| Trie (prefix) nodes over the literals | 7,255 (vs 11,061 total chars) |
| Trace event length (`traces/*.50k.gz`) | avg 344 B, max 434 B |
| **RD resources (user-reported)** | **175,964 LUT; 279,482 FF; 938 BRAM; 13 URAM** (+ CPU cores for Hyperscan) |

### 1.3 Findings that motivate the optimizer

1. **The comparator is not the problem; the structure is.** PTP's cost blow-up comes from
   (a) enumerating floating patterns per offset, (b) a separate bank per flit even when banks are
   identical, (c) a wide EOP fold. Back-of-envelope for RD's literal set in PTP-as-is at 1536 B
   packets: ~2.4M PIR rows, ~4.7M LUTs. With folding (one shared 64-lane bank + carry + sticky
   bits): ~200K LUTs, exact, no CPU.
2. **Folding.** Rows that are translations of the same shape (same ID) across all
   (flit, lane) anchors → one shared bank, reused every cycle, plus an L−1 byte carry and a
   per-packet sticky bit. Partial anchor sets (e.g. "only from byte 18", "only in payload")
   → shared bank + an **enable** (mask) — enables scale with lanes/regions, not literals.
   Data-dependent regions → a shared per-byte **tag** (exactly RD's field tagger).
3. **Chaining instead of EOP lookup.** Pass a state from flit *i* to *i+1*; the state is the
   *set* of rows still alive. Encoded state (DFA-like) is small for header parsing;
   for many floating literals combined by AND it explodes (2^k) → use one-hot sticky bits.
   The minimal carried state = cut width of a time-ordered decision diagram.
4. **Memory (BRAM/URAM) — storage vs bandwidth.** A LUT6 is a 64-bit ROM evaluated every cycle;
   a BRAM36 is a 36,864-bit ROM (≈ 576 LUT truth tables) with **2 reads/cycle** and ≤ 15-bit address
   (URAM: 4K × 72, 12-bit). So:
   - things that scale with #patterns → memory, read rarely;
   - things done at every position every cycle → LUTs, or tiny memories replicated W/2 times.
   - RD follows this: tiny bitmaps replicated across 64 positions, big tables read only after compaction.
5. **Memory options that keep PTP exact (no CPU):**
   - Anchored rows, one lookup per packet: **tuple-space search** (group rows by wildcard mask) +
     **cuckoo / perfect hashing** built at compile time (zero pattern–pattern collisions) with the
     **key stored** in the slot (input–pattern collisions rejected by one compare). fig9-style
     4,096 × 80-bit: est. ~12 BRAM36 + < 1K LUTs vs 35K–337K LUTs today.
   - Per-flit idle banks: comparison stage as ROM addressed by `{flit_counter, byte}` → pseudo-ID
     (~100 BRAM36 for 64 lanes × 24 flits, independent of pattern count).
   - Floating literals at scale: wide cheap filter (hash bitmap/shift-or) + narrow exact verify
     (cuckoo table with stored key) + per-event literal bits + AND-terms: est. 30–70K LUTs + 2–20 Mb BRAM.
   - Chained transitions in ROM only when `state bits + pseudo-ID bits ≤ ~14` and the replaced logic is
     ≥ a few hundred LUTs; otherwise LUTs win (e.g. "`hi`" as ROM = 8 BRAM36 vs ~40 LUTs).
6. **Crossover intuition.** Pure LUT exact matching costs ~140 LUTs per floating literal at 64 lanes;
   RD's total LUT budget ≈ 1,250 such literals. Hashing/memory wins as #literals grows (RD claims up to 10K).

All of the above were hand estimates. **This project replaces hand estimates with a tool that
finds the structure automatically and reports a cost.**

---

## 2. Goal

Build `engine/pir_opt/` — a Python package that takes a PIR (`.hex` + config JSON) and produces:

1. a **hardware plan** (JSON IR, §5) describing shared banks, per-flit stages, enables/tags,
   carried state, memory-mapped structures, and the output logic;
   The plan carries an explicit **time model** (§4.1): units without time, schedules, and
   delay-annotated edges, so a later RTL backend knows which hardware is reused across clock cycles;
2. a **cost report** (LUT / FF / BRAM36 / URAM estimates + structural counts) for the plan and for
   the *baseline* (today's PTP structure on the same PIR);
3. a **cycle-level Python model** of the plan, checked against a **golden PIR evaluator** on
   random and directed packets.

Success = for each benchmark, the plan is equivalent to the PIR (golden evaluator agrees on all test
packets) and the cost report shows where/why it is cheaper than the baseline.

### Non-goals (for now)
- No SystemVerilog emission, no Vivado/Quartus runs, no change to the existing backend.
- Field extraction (form-B rows `name:start:end`) — parse and carry through, do not optimize.
- REGEX tokens (`REGEX_TOKENS.md`) — out of scope; design the IR so they can be added.

---

## 3. Semantics the optimizer must preserve

Read `README.md` (PIR section) and `engine/tools/build_rtl_countluts.py` / `engine/utils/utils.py`.

- Nibble granularity; `x` = don't-care nibble. Row width = `max_width`. Byte 0 = first byte on the wire;
  hex string left-to-right = MSB-first in `data_in` (see generated `comparison_logic_*.sv`).
- Custom tokens (`RANGE`, `NE`, `EQOR`, constant or port-based) from the config `custom` list.
- **Priority:** confirm from `build_lookup_rtl*` exactly how overlapping matches resolve
  (nested if/else vs sequential `if` — they differ; the "hi" run produced sequential `if`s where the
  *last* assignment wins). Write it down in the golden evaluator and test it.
- **Short packets:** a packet that ends before flit *i* leaves module *i*'s latch at 0 (reset at SOP);
  rows that constrain bytes beyond EOP must not match. Reproduce this exactly.
- Output: single `path_id` (0 = no match). Optionally support a rule bit-vector output mode as an
  extension, but default to PTP semantics.

---

## 4. Approach — the three "boxes" + state + memory

Data model (sparse; use `scipy.sparse` / plain dicts):

```
ATOM   (flit t, bit_range, predicate)      predicate = const value | RANGE | NE | EQOR   (no ID!)
ROW    AND of atoms + ID + priority        (keeps multi-byte patterns intact)
ID     OR of rows with the same ID          (all offsets of one floating rule share an ID)
SHAPE  a row's atoms re-based to its first constrained byte, flit-agnostic
FAMILY (ID, shape) → set of anchor positions (t, b)
```

### 4.1 Time model (explicit — this is what lets the RTL reuse hardware across clock cycles)

PTP today has no notion of time in its IR: byte *k* of the packet is just a bit position, and the
backend builds one comparator per (position, constant). The optimizer must instead reason in
**space-time** coordinates, like a systolic-array / HLS scheduler.

- **Time = the flit counter.** The only notion of time is the per-packet flit counter that PTP already
  has. Byte *k* → `(t, lane) = (k // B, k % B)`, where `B` = bytes per flit and `t` = flit index of the
  current packet. Bit ranges map the same way; an atom that crosses a flit boundary is split into two
  atoms at `t` and `t+1`.
  - Off-by-one in today's RTL (`hex_to_rtl_top_tmpl.sv`): `flit_counter` is reset *on* the SOP cycle, so
    flit 0 is identified by `sop`, and flit `i ≥ 1` by `flit_counter == i-1`. The IR uses `t` (flit
    index); the RTL backend maps `t = sop ? 0 : flit_counter + 1` (or changes the counter so it equals `t`).
  - Saturation: the counter stops at the last flit of the window. Schedules like "all t" are unaffected;
    an enable indexed by the counter must treat the saturated value as "every later flit".
  - The counter is the *only* static time. Positions that depend on data (variable payload start, JSON
    fields) are not time — they come from a tag unit (data-dependent enable), as in RD's field tagger.
- **Three kinds of object** (keep them separate in the IR):
  - **OP** — a logical operation *at a time*: `cmp(t, lane_range, pred)`, `and(t, …)`, `or(t, …)`.
  - **UNIT** — a physical piece of hardware, *no time*: `cmp(lane_range, pred)`, an AND tree, a ROM.
  - **SCHEDULE / BINDING** — which OPs a UNIT executes, i.e. the set of flit-counter values
    `S ⊆ {0..T-1}` in which its output is consumed.
- **Sharing rule.** Only one flit is on the bus per cycle, so OPs at different `t` never compete for a
  unit. Therefore **all OPs with the same `(lane_range, pred)` bind to one comparator UNIT, whatever
  their `t`**; OPs with the same `(t, lane_range, pred)` are the same OP (CSE).
  - This is broader than "folding floating patterns": two *anchored* rows that check lane 4 == `08`
    in flit 1 and in flit 3 also share one comparator. Today PTP builds two.
  - What differs per cycle is the **consumer**: which AND/state logic uses the comparator's output in
    cycle `t`. That is expressed by schedules, not by duplicating comparators.
- **Delays on edges.** Represent the plan as a dataflow graph whose edges carry a delay `d` in flits
  (`d = 0` same cycle; `d = 1` value from the previous flit — carries, state, sticky bits; `d = k` k flits
  ago). Every `d ≥ 1` edge becomes `d` registers in RTL (Leiserson–Saxe retiming-graph formalism).
- **Folding consumers.** If a consumer has the *same structure* for every `t` in its schedule and its
  inputs bind to the same units (e.g. "`h` at lane b−1 AND `i` at lane b" for all t), it becomes **one**
  consumer UNIT with an **enable** `= (flit_counter ∈ S)` — that enable *is* the mask, derived, never
  hand-written. Data-dependent enables come from a tag unit instead of `flit_counter`. Consumers that
  differ per `t` stay per-cycle logic selected by `flit_counter` (or a `{t, …}`-addressed ROM).
- **Two clocks of "time".** Keep *logical time* (flit index `t`) separate from *physical pipeline
  stages* (registers inserted inside one flit's logic for Fmax). The IR records logical time; pipeline
  depth is an RTL concern. But flag every **loop** in the graph (a cycle whose total delay is 1, e.g.
  state/sticky feedback): its logic must close within one clock and cannot simply be pipelined.
- **Emission contract for the future RTL backend:** one instance per UNIT; enable from its schedule
  (or tag); `d` registers per delayed edge; loops called out for timing.

Small example (8-byte flits, "`hi` anywhere" + rows with EtherType at bytes 12–13 and proto at byte 23):

| OP(s) | UNIT | Schedule S | Note |
|---|---|---|---|
| `cmp(t, lane b, ==68)` for all t, b | `cmp(lane b, ==68)` ×8 | all t | built once, not once per flit |
| `and(cmp(lane b-1,68)@t, cmp(lane b,69)@t)` | `and_hi(b)` ×7 | all t | folded consumer |
| `and(cmp(lane 7,68)@t-1, cmp(lane 0,69)@t)` | `and_hi(0)` with a `d=1` edge | all t ≥ 1 | carry register |
| `cmp(1, lanes 4-5, ==0800)` | `cmp(lanes 4-5, ==0800)` | {1} | per-cycle consumer |
| `cmp(2, lane 7, ==06)` | `cmp(lane 7, ==06)` | {2} | its result reaches the row AND via a `d=1` edge (state) |
| `found_hi` | OR + register, `d=1` self-edge | all t | loop → timing-critical |

Pipeline:

```
PIR → expand → atoms/rows matrix → space-time ops (t, lane)
  [Box 3] families + anchor-set coalescing  → shared banks / per-flit stages / enables
  [Box 1] per-ID two-level minimization     → fewer merged rows
  [Box 2] cross-row/ID shared sub-products   → rectangles (CSE)
  [State] cut width per flit boundary        → encoded state vs one-hot per group
  [Mem]   LUT-vs-memory mapping pass          → ROMs / cuckoo tables / filter+verify
  [Cost]  estimate; [Model] cycle-level sim; [Check] vs golden evaluator
```

| Step | Known problem | Library (fallback) |
|---|---|---|
| Box 3: periodic families, masks | Polyhedral / affine set coalescing; folding (Parhi), systolic space-time mapping | `islpy` (`coalesce`, box hull). Fallback: pure-Python interval/lattice detection per (ID, shape) |
| Box 1: merge rows | Two-level logic minimization | `pyeda` `espresso_exprs`; `sympy` `SOPform` only for tiny cases |
| Box 2: shared sub-products | Kernel extraction / rectangle covering | Greedy rectangle extraction in Python; exact via `ortools` CP-SAT or `python-mip` on small instances; optional ABC `fx` via Yosys/pyosys |
| State | BDD/MDD cut width with time-ordered variables | `dd` (CUDD) or PyEDA BDD; one-hot fallback when width explodes |
| Floating literals at scale | Automata (NFA/DFA, Aho-Corasick) | `pyformlang` / `automata-lib`; `hyperscan` (pip) as a reference matcher only |
| Memory mapping | Tuple-space search, cuckoo/perfect hashing, RFC-style tables | Pure Python (cuckoo placement with seed search is ~100 lines) |

None of these libraries are installed yet (`pyeda`, `dd`, `islpy` checked missing). Use a venv;
pin versions in `engine/pir_opt/requirements.txt`. Every library-backed step must have a pure-Python
fallback so the pipeline runs end-to-end without optional deps.

Scale warnings (from today's analysis):
- Do **not** minimize over the whole packet (1536 B = 12K input bits). Fold first (Box 3),
  then run Box 1/2 per stage (≤ 512 input bits at 64 B flits).
- Many floating literals combined by AND → decision-diagram state explodes (2^k). Use `dd` to
  *measure* cut width; switch to one-hot sticky bits above a threshold.
- Do not materialize millions of rows for floating patterns if avoidable: accept a compact
  input form (see §6, Phase 0) and/or detect families lazily.

---

## 5. Output IR — "hardware plan" (sketch; refine in Phase 0)

The IR is a time-annotated dataflow graph (§4.1): `units` carry no time, `schedule` says in which
flits a unit's output is consumed, `edges` carry delays in flits.

```json
{
  "config": {"data_width": 512, "max_width": 12288, "num_flits": 24},
  "units": [
    {"id": "c3",  "kind": "cmp", "lane_bits": [96, 111], "pred": {"op": "EQ", "value": "0800"}},
    {"id": "h5",  "kind": "and", "inputs": ["c_l4_68", "c_l5_69"]},
    {"id": "fh",  "kind": "or_sticky", "inputs": ["h0", "h1", "...", "fh"]}
  ],
  "schedule": {"c3": [1], "h5": "all", "fh": "all"},
  "edges": [
    {"src": "c_l7_68", "dst": "h0", "delay": 1},
    {"src": "fh", "dst": "fh", "delay": 1, "loop": true}
  ],
  "atoms":  [{"id": "a17", "t": 1, "lane_bits": [96, 111], "pred": {"op": "EQ", "value": "0800"}, "unit": "c3"}],
  "banks": [
    {"id": "B0", "kind": "shared", "lanes": 64, "carry_bytes": 7,
     "families": ["F3", "F9"], "enable": {"kind": "tag|flit_mask|none", "ref": "T0"}},
    {"id": "S1", "kind": "per_flit", "flit": 1, "rows": ["r12", "r13"]}
  ],
  "tags":   [{"id": "T0", "markers": ["2265223a22"], "end_byte": "22", "bits": 2}],
  "state":  [{"boundary": 1, "encoding": "encoded|onehot", "width": 3}],
  "memories": [
    {"id": "M0", "kind": "rom|cuckoo|bitmap", "addr_bits": 13, "data_bits": 8,
     "depth": 6144, "copies": 32, "reads_per_cycle": 64, "purpose": "flit,byte->pseudo_id"}
  ],
  "output": {"mode": "priority_id|bitvector", "rows_or_terms": ["..."]},
  "cost": {"lut": 0, "ff": 0, "bram36": 0, "uram": 0, "breakdown": {}}
}
```

---

## 6. Phases

Each phase ends with pytest tests under `engine/pir_opt/tests/` and a short note appended to
`engine/pir_opt/NOTES.md` (what was measured, what surprised you).

### Phase 0 — Parsing, data model, golden evaluator, benchmarks
- Parse `.hex` + config (reuse `engine/utils/utils.py` helpers where sensible; do not modify them).
- Build atoms/rows/IDs, then map every atom to space-time coordinates `(t, lane_range)` (§4.1),
  splitting atoms that straddle a flit boundary. Golden evaluator: `eval_pir(packet_bytes) -> path_id` with exact PTP
  semantics (§3). Cross-check against the existing self-checking testbench vectors in
  `artifact_out/*/hex_to_rtl_top_tb.sv` (parse expected values out of them) — this anchors §3.
- Compact floating input (for benchmarks only, not a new user syntax yet): a generator that
  expands `literal @any [window] [field]` into PTP rows, plus a lazy family representation so
  RD-scale sets don't need millions of explicit rows.
- Benchmark set (`engine/pir_opt/bench/`):
  1. `hi` floating, 32 B / 8 B flits (31 rows) — smallest folding case.
  2. `hi` + 4 header rows (EtherType at bytes 12–13, proto at 23) — mixed case.
  3. Full and simple P4 parsers: `python3 engine/p4_to_rtl.py ...` (p4c is installed at
     `/usr/local/bin/p4c`); PIR lands in `engine/tmp/` — copy it.
  4. Snort 10-rule: `make snort` → `snort_out/`.
  5. Synthetic fig9: `synthetic/*.hex` with `synthetic/{one,multi}_group.json`.
  6. RD literal set: script that parses `../RapidDetect-RCC/hardware/include/tables/rid2sid_table.h`
     comments (`// sid=.. rid=.. 0x..(b'..') (len=..:kind:field) 1:(b'..'):field 0:(b'..'):field ...`)
     into literals + fields + conjuncts. Expected: 4,348 conjuncts, 1,542 literals, 744 fast patterns.
     Use field tags as a region predicate; packets = lines from `../RapidDetect-RCC/traces/*.50k.gz`.

### Phase 1 — Baseline cost model (today's PTP structure)
- Model the current structure: per-flit comparator banks (after `refine_char_per_col`), pseudo-ID
  latches, EOP lookup. Count comparators/bits/terms; convert to LUT/FF with simple constants.
- **Calibrate** constants against measured data: `PatternToParser/new_results/fig9_results.csv`,
  `table1_results.csv` (and `artifact_out/fig9` configs). Report fit error. If error is large, say so —
  the cost model is only as good as this calibration.

### Phase 2 — Box 3: time binding, families, folding, enables
- **Bind comparators across time first:** every `cmp(t, lane_range, pred)` OP → one UNIT per
  `(lane_range, pred)`; record its schedule. Report comparators before/after (baseline = one per
  `(t, lane_range, pred)`). This applies to anchored rows too, not only floating ones.
- Group rows by (ID, shape); compute anchor sets in (t, b).
- Classify: full lattice → shared bank; single flit → per-flit stage; near-full → shared bank + enable
  (derive the enable as a per-flit lane mask; if regions are data-dependent, emit a tag requirement).
- Carry requirement = shape length − 1 bytes; straddling rows must be correct.
- Fold consumers whose structure is identical across their schedule into one UNIT + enable; emit
  delay edges for carries; mark loops.
- Acceptance: bench 1 → one 8-lane bank, carry 1 B, no per-flit stages, 16 comparator units total
  (not 64). Bench 2 → one shared bank + per-flit consumers for header rows, and header columns are
  **not** split by the floating rows. Bench 3 (P4) → report how many comparators were shared across
  flits among anchored rows alone.

### Phase 3 — State between flits
- For per-flit stages: compute alive-set states per boundary (subset construction over rows) and/or a
  time-ordered MDD with `dd`; report cut width per boundary.
- Choose encoded vs one-hot per group (threshold configurable); priority resolved at the end (static
  mapping for encoded, priority encoder for one-hot); EOP masks for short packets.
- Acceptance: P4 parsers → small encoded state; RD set → one-hot sticky literal bits + AND-terms.

### Phase 4 — Box 1 / Box 2 within each stage
- Per-ID minimization (Espresso) and cross-row shared sub-product extraction (greedy rectangles; exact
  CP-SAT on small cases). Shared byte decoders per lane.
- Measure what this buys *after* folding; expect modest gains (synthesis tools already do some of this
  within a cycle) — report honestly.

### Phase 5 — Memory mapping pass (BRAM/URAM)
Implement a cost-driven chooser between LUT and memory realizations, with hard constraints:
address ≤ 15 bits (BRAM36) / 12 bits (URAM), 2 reads/cycle/copy, copies = ceil(reads_per_cycle / 2).
Candidates to evaluate per structure:
1. `{flit_counter, byte_lane}` → pseudo-ID ROMs for per-flit comparison stages.
2. Tuple-space + cuckoo (2-way, bucketized) tables with stored keys for large anchored groups
   (actually place keys; report load factor, seeds tried, BRAM count).
3. Filter + exact verify for floating literals: per-length hash bitmaps (replicated) + cuckoo verify
   lanes; estimate expected candidates/cycle from the traces (Phase 0 bench 6) to size lanes.
4. Chained transitions as ROM `T_i[state, pseudo-IDs]` only when the address fits; also the
   "state out of the address" variant (ROM returns next-state for every current state, LUT mux closes the loop).
- Report for bench 5 (fig9) and bench 6 (RD): LUT-only plan vs memory plan vs RD's measured resources (§1.2).

### Phase 6 — Cycle-level model and equivalence
- Python model that executes the plan's time-annotated graph flit by flit: in cycle `t`, evaluate
  every UNIT once, apply schedules/enables (`flit_counter ∈ S` or tag), read delayed edges from
  registers written in earlier cycles, update loops. It must never evaluate a unit more than once per
  cycle — that is the check that comparator reuse across time is real. Compare with the golden evaluator on: all directed vectors from testbenches,
  random packets, mutated packets (insert/remove pattern bytes at flit boundaries, short packets),
  and real trace lines for bench 6. Zero mismatches required.

### Phase 7 — Report
`engine/pir_opt/REPORT.md`: per benchmark, baseline vs optimized (LUT/FF/BRAM/URAM, structural counts,
state widths), what each phase contributed, where the cost model is uncalibrated, and recommendations
for the RTL backend (which templates are needed: shared bank, per-flit stage, tagger, state register,
ROM, cuckoo table).

---

## 7. Cost-model starting constants (calibrate in Phase 1; do not trust blindly)

| Item | Estimate |
|---|---|
| k-bit equality vs constant | ≈ ceil(k/6) LUT + reduction |
| byte decoder (lane == c), shared | ≈ 1–2 LUT per (lane, value) |
| AND of n inputs | ≈ ceil((n−1)/5) LUT |
| OR tree over n | ≈ n/5 LUT |
| floating literal, 64 lanes, ~8 B | ≈ 64 × 2 + 13 ≈ 140 LUT |
| BRAM36 | 36 Kb; ≤ 32K×1 … 1K×36 (512×72 SDP); 2 ports |
| URAM | 288 Kb; 4K × 72; 2 ports |
| FF | 1 per registered bit (latches, carries, state, sticky bits) |

---

## 8. Open questions to resolve along the way
- Exact PTP priority semantics in `tree` vs `unique` lookup modes (§3).
- Should floating/field intent be inferred from expanded rows only, or should the PIR grow syntax
  (`@any`, `@in(field)`, `@after(marker)`)? Prototype both input paths; recommend one.
- Literals longer than 8 B: RD splits into chunks and never checks adjacency; to stay exact we need
  chunk-position chaining or longer length classes — measure the cost.
- Hit bursts in filter+verify (adversarial inputs) — report worst case, not just trace average.
- Multiple events per flit (RD packs events into 2 KB Ethernet frames) — note, don't solve.

## 9. Ground rules
- Python only; do not touch `engine/tools/*`, `engine/templates/*`, or `artifact_out/`.
- Deterministic (seeded) everywhere; every number in `REPORT.md` reproducible by one command.
- Prefer clarity over cleverness; keep each phase runnable on its own.
- If a library is slow or wrong at scale, record it in `NOTES.md` and use the fallback.
