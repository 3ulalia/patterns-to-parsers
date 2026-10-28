> **AI-generated document.** This plan was written by Claude (Anthropic) from a read-only review of the repository at commit `bbd8710` (branch `main`), compared against `origin/ptp_v1` and `origin/v2-pir-optimizer`. No code was run while writing it. Line counts and size limits are estimates, and the design choices need to be checked by a human before implementation.

# Vitis HLS backend for Patterns to Parsers

## 1. Goal and principles

**Goal.** Add a second backend that turns a PIR (via `Design`) into a Vitis HLS C++ kernel and lets Vitis HLS handle scheduling, pipelining, storage binding and dataflow. A design-space exploration (DSE) driver sweeps those choices and compares the results against the existing RTL backend and the `ptp_est` LUT estimates.

**Principles**
1. **Python decides *what* is computed; HLS decides *how* it is built.** `ptp.design.build()` stays the only place where the column layout, pseudo-ID encoding and first-row-wins masks are decided. HLS is not expected to find those; it is expected to schedule and bind them.
2. **Generated data, hand-written kernels.**
   - Python emits headers of `constexpr` tables plus a small generated function for custom tokens.
   - The kernels are hand-written, templated C++ that live in the repo.
   - A fully expanded "flat" emitter exists only as a fallback for very large rule sets (Phase 6).
3. **Optimization choices are tcl directives, not code.** Pragmas that a sweep varies go into generated `directives.tcl` (`set_directive_*`), attached to labelled loops and named arrays. Changing a DSE point never regenerates C++, except when switching kernel variants.
4. **Equal outputs, not equal timing.** The HLS kernel must produce the same result per packet as `golden.expected`. Latency and initiation interval (II) are free variables to explore, not constraints.
5. **Every piece is checkable without Vitis.** A Python evaluator over the exported tables is checked against `golden`, so table bugs are caught by `pytest` before any HLS run.

## 2. How the work splits between Python and C++ (estimates)

| Part | Language | Size | Location |
|---|---|---|---|
| Table/header exporter | Python | ~250 lines | `ptp/emit_hls.py` |
| Table-level reference evaluator (for tests) | Python | ~100 lines | `ptp/hls_ref.py` |
| Project/directive tcl generator, report parser | Python | ~200 lines | `ptp/hls_run.py` |
| DSE driver | Python | ~250 lines | `ptp/hls_dse.py` |
| Kernels (K1, K2) + lookup/priority options | C++ | ~400–600 lines | `ptp/hls/*.h`, `ptp/hls/parser_top.cpp` |
| C simulation testbench | C++ | ~150 lines | `ptp/hls/tb.cpp` |
| Co-simulation adapter for `parser_tb.sv` | SV | ~60 lines | emitted by `ptp/emit_hls.py` |
| Flat fallback emitter | Python | ~250 lines | `ptp/emit_hls_flat.py` (Phase 6) |

## 3. Generated header format (`ptp_design.h`)

All sizes are `constexpr`, so loops can be fully unrolled and constant-folded. Names below are the proposed C++ identifiers and come from `Design` fields:

```cpp
// --- sizes
constexpr int DW = ...;  T = ...;  MW = ...;          // cfg.data_width, num_flits, max_width
constexpr int NCMP = ...;                            // len(d.comparators) + len(d.tok_comps)
constexpr int NCOL = ...;  NSEG = ...;  NROW = ...;  NGRP = ...;
constexpr int PID_BITS = ...;                        // sum(c.width) = d.stats()["latched_bits"]
constexpr int IDW = ...;                             // emit.idw_of(d) (form A)

// --- nibble comparators (shared by all flits): data nibble j == v
constexpr int  CMP_J[...], CMP_V[...];
// token comparators: generated function (variable opcodes/value counts), from emit._tok_expr
ap_uint<NTOK> token_bits(const ap_uint<DW>& flit, const ports_t& p);

// --- segments: hit = (cmp_vec & SEG_MASK[s]) == SEG_MASK[s]
constexpr ap_uint<NCMP> SEG_MASK[NSEG];
constexpr int SEG_COL[NSEG], SEG_ID[NSEG];           // column, 1-based id (binary) / bit (one-hot)
// --- columns
constexpr int  COL_FLIT[NCOL], COL_WIDTH[NCOL], COL_PID_OFF[NCOL];
constexpr bool COL_ONEHOT[NCOL];
// --- rows (only rows that matter: live rows with a group + rows used as masks, as in emit._core `raw`)
constexpr ap_uint<PID_BITS> ROW_CARE[NROW], ROW_VAL[NROW];   // binary columns: (pid & care) == val
constexpr ap_uint<PID_BITS> ROW_OH[NROW];                    // one-hot columns: (pid & oh) == oh
constexpr int ROW_GRP[NROW];                                 // d.group_of, -1 = mask-only row
// --- priority (first-row-wins), ragged list flattened
constexpr int MASK_OFF[NROW + 1], MASK_IDX[NMASK];           // d.masks
// --- groups
constexpr ap_uint<IDW> GRP_ID[NGRP];                         // form A: d.groups
// form B: per (group, field) msb/lsb, from d.groups signatures and golden.field_order
```

Notes:
- The row matcher uses pid masks `(care, val, oh)` rather than `(col, sid)` lists, so each row test is a single wide AND/compare. That suits unrolling and lets Vivado do the logic minimization.
- `ROW_*` arrays are `NROW × PID_BITS` bits. Above a size threshold (roughly 10⁶ bits, to be measured in Phase 6) the exporter warns and suggests the flat emitter.

## 4. Kernel interface

```cpp
void parser_top(hls::stream<ap_axiu<DW,0,0,0>>& in,   // TLAST = end of packet; start of packet = first beat after TLAST/reset
                hls::stream<result_t>& out,           // form A: ap_uint<IDW>; form B: struct of field outputs
                const ports_t& ports);                // port tokens; s_axilite or ap_none (directive)
```
- Uses `ap_ctrl_none` with a free-running `while(1)`-style loop in synthesis, and a bounded loop in C simulation via a macro. Each loop iteration handles one flit.
- Flits beyond `T` are ignored, as in the RTL. Packets with no SOP have no direct equivalent, because SOP is implied, so this is documented as a semantic difference in the README.
- Port tokens are `ports_t`, a generated struct named after `tokens.port_widths`. Like the RTL, they are assumed static while a packet is in flight.

## 5. Kernel variants (selected by `--hls-kernel`, swept by the DSE)

| Id | Structure | Notes |
|---|---|---|
| **K1** `pid` | The paper's scheme: comparators → segment hits → per-column pid captured when `flit == COL_FLIT` → at end of packet, row match → priority → group → output | Closest to the RTL; the reference variant |
| **K2** `accum` | A "still matching" bit per row, `alive[r] &= row_matches_flit(r, t)`, updated every flit; at end of packet only priority → group | Spreads the lookup work across flits; idea taken from `v2-pir-optimizer` (needs per-flit row tables `ROW_*_F[t]`) |

Options that apply to either kernel:
- **Lookup `L`:**
  - `logic`: unrolled row compares.
  - `rom`: K1 only, when `PID_BITS ≤ ~18`. Python precomputes `LUT[2^PID_BITS] → group+1` with `model.Model.row_hit`/`lookup` logic, and the table is bound to LUTRAM, BRAM or URAM with `BIND_STORAGE`.
  - `rom-staged`: splits the pid vector into ≤ 2 ROMs plus a small final AND (stretch goal).
- **Priority `P`:**
  - `mask`: explicit `d.masks`, then one-hot OR.
  - `chain`: an unrolled early-exit `for r in rows: if (hit[r]) return grp[r]`. Simpler, but may produce a long serial chain; the DSE decides.
- **Encoding `E`:** binary pid via `unique`-style select vs OR-encode, mirroring `--or-encode`. It is a code-level `if constexpr`.

Form B adds an extraction stage that keeps the flits of the window needed by `d.groups` (as in `emit.emit_parser_top`'s `need`). Option `D` = `dataflow` puts matching and extraction in separate `DATAFLOW` processes connected by a stream.

## 6. DSE space

| Axis | Where it lives | Values |
|---|---|---|
| Clock target | `create_clock -period` | e.g. 2.5, 3.33, 4, 5 ns |
| Flit loop II | `set_directive_pipeline -II` | 1, 2, 4 |
| Overlapping packets | kernel macro | stall-at-EOP (RTL-equivalent) / free |
| Kernel | `--hls-kernel` | K1, K2 |
| Lookup `L` | kernel macro + `set_directive_bind_storage` | logic, rom (LUTRAM/BRAM/URAM) |
| Priority `P` | kernel macro | mask, chain |
| State storage | `set_directive_array_partition` / `array_reshape` | complete / cyclic factor N on `pid`, `alive`, window |
| Extraction (form B) | `set_directive_dataflow` | on/off |
| Front-end knobs | `ptp.run` arguments | `char_per_col`, `data_width`, `or_encode` |

**Driver:** `python3 -m ptp.hls_dse <pir> <json> --space space.json -o dse_out [--part P] [--jobs N] [--prune K]`.
1. Expand the cartesian product, filtering out invalid combinations (e.g. `rom` with large `PID_BITS`, `K2` with `rom`).
2. Prune with `ptp_est` LUT estimates plus a structural score, keeping the top K per front-end setting.
3. Run `vitis_hls` with csim + csynth for each surviving point, in parallel jobs. Optionally run a Vivado out-of-context implementation (`export_design -flow impl`) for real LUT/FF/Fmax numbers.
4. Collect results into `dse_out/results.csv`:
   - the point's settings;
   - estimated and achieved latency and II;
   - LUT, FF, BRAM, URAM;
   - estimated clock and, with impl, post-route Fmax;
   - csim pass/fail.

   Also report the Pareto front of area vs throughput.
5. Baseline: the same PIR through the RTL backend, synthesized out-of-context with a recovered/adapted `run_vivado.tcl` from `origin/ptp_v1`. Default part `xcku025-ffva1156-1-c`, overridable.

## 7. Command-line and file layout

- `python3 -m ptp.run <pir> <json> -o <out> --backend hls [--hls-kernel K1] [--hls-opts L=logic,P=mask] [--hls-run csim|synth|cosim|impl] [--part P] [--clock NS]`
  - `generate()` gets a `backend` argument. The RTL path is unchanged and remains the default.
  - Vectors still come from `vectors.make_packets` / `golden.expected` / `emit.emit_vector_files`.
- Output `<out>/`:
  - `ptp_design.h`, `ptp_tokens.h`;
  - copies of `ptp/hls/*.{h,cpp}`, `tb.cpp`;
  - `vec_*.hex`;
  - `run_hls.tcl`, `directives.tcl`;
  - `hls_wrap.sv` (cosim adapter).
- `vitis_hls` is found on `PATH` or via `$VITIS_HLS`, mirroring `VERILATOR`.

## 8. Phases

**Phase 1: tables and the reference evaluator (Python only)**
- `ptp/emit_hls.py`: export §3 for form A, no tokens.
- `ptp/hls_ref.py`: evaluate packets using only the exported tables (K1 + logic + mask semantics).
- Tests: `hls_ref` matches `golden.evaluate` on `ptp/tests/data/*` (reuse the `test_ptp.py` parametrization).

**Phase 2: K1 kernel, form A, C simulation**
- `ptp/hls/parser_top.cpp` K1 + `tb.cpp` reading `vec_*.hex`.
- `ptp/hls_run.py`: write `run_hls.tcl`, run csim, parse the pass/fail output.
- `--backend hls --hls-run csim` passes on `examples/02_pir_to_rtl` and the test data.
- Tests needing `vitis_hls` are skipped when it isn't found.

**Phase 3: synthesis and reports**
- Run csynth and parse `csynth.xml` (latency, II, resources, estimated clock).
- Generate `directives.tcl` from an options dict.
- Recover the out-of-context Vivado script for the RTL baseline.

**Phase 4: tokens, ports, form B**
- `ptp_tokens.h` generated from `d.tok_comps`, using `emit._tok_expr` semantics; `ports_t`.
- Extraction stage, `result_t` struct, `D=dataflow`.
- csim parity on the token/form-B test data (`tok_*`, `full_parser`, `simple_parser`).

**Phase 5: variants and DSE**
- K2 (with the `ROW_*_F[t]` tables in the exporter and in `hls_ref`), `L=rom`, `P=chain`, packet overlap.
- `ptp/hls_dse.py` with `ptp_est` pruning, a results CSV and a Pareto summary.
- A first sweep on `full_parser` (dw 64/256/512) and `snort_10_rule.txt`, compared to the RTL.

**Phase 6: scale and co-simulation**
- `emit_hls_flat.py` fallback and a size threshold.
- Measure HLS compile time on `snort_frontend/snort.txt`.
- `hls_wrap.sv` adapter (AXIS ↔ data_in/valid/ready/sop/eop; port names from the exported RTL) so that `parser_tb.sv` runs against the HLS-exported RTL in Verilator and in Vitis cosim.

## 9. Decisions to confirm before or during implementation

1. Vitis HLS version: this determines `ap_axiu` template arity, `hls::stream` behaviour and `AP_INT_MAX_W`. Windows wider than 1024 bits need `#define AP_INT_MAX_W` before the includes.
2. Target part(s) and clock targets for the DSE.
3. Whether overlapping packets (higher throughput than the RTL) is allowed, or the RTL's one-packet-at-a-time behaviour is required.
4. Whether TLAST-only framing is acceptable, i.e. dropping the explicit SOP and the RTL's ignore-without-SOP behaviour.
5. Whether `v2-pir-optimizer` should be ported to the new layout first. K2 overlaps with its per-row alive bits idea.

## 10. Verification

- `python3 -m pytest ptp ptp_est` passes, including the new `hls_ref` parity tests (no Vitis needed).
- With `vitis_hls` on `PATH`: `pytest -m hls`, i.e. csim parity for every kernel variant × option combination on the test data.
- For each DSE point, csim pass is required before its synthesis results are recorded.
- Phase 6: `parser_tb.sv` passes against the HLS-exported RTL (Verilator and cosim).
- Results from the final sweep are compared with the RTL backend's out-of-context synthesis and with `ptp_est` for the same PIRs.
