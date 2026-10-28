> **AI-generated document.** Written by Claude (Anthropic) from a read-only review of `main` at `bbd8710`, compared with `origin/ptp_v1` and `origin/v2-pir-optimizer`. No code or tests were run. Check it against the code before relying on it. The implementation plan built on this is in [PLAN.md](PLAN.md).

# Codebase analysis: the rewrite, and fit for a Vitis HLS backend

## Summary
- Adding a Vitis HLS backend is still a reasonably sized job, and probably easier than on `ptp_v1`. The pipeline is now cleanly split, and a backend only needs to read one in-memory object, `Design`.
- The harder parts are elsewhere: stricter semantics, no backend switch to plug into, and the synthesis/evaluation flow from `ptp_v1` was deleted.

## Current pipeline (`main`)
```
pir.load_pir() -> (rows, Config) -> design.build() -> Design -> emit.* -> parser.sv, extraction.sv, parser_top.sv, parser_tb.sv
                                                           \-> golden / model / vectors (expected results, test vectors)
```
| Module | Role |
|---|---|
| `ptp/pir.py` | Parses the PIR and the JSON config into `Row` and `Config` |
| `ptp/tokens.py` | Custom tokens (RANGE / NE / EQOR; constant or port) |
| `ptp/design.py` | Column layout, pseudo-IDs, first-row-wins masks; produces `Design` |
| `ptp/emit.py` | Writes Verilog and the testbench with plain `f.write`; RTL-specific |
| `ptp/golden.py`, `ptp/model.py` | Reference semantics, and a cycle-level model of the RTL |
| `ptp/vectors.py` | Seeded packet generator |
| `ptp/run.py` | Command-line entry point; hard-codes the `emit.*` calls and Verilator |
| `ptp_est/` | Analytical LUT estimator that works on `Design` (new) |
| `ptp/p4_to_pir.py`, `snort_frontend/` | Frontends |

`Design` fields a backend would use:
- `comparators` and `tok_comps`;
- `columns`, each with `segs`, `seg_id`, `width`, `onehot` and `t` (the flit it is captured on);
- `row_sig`, `masks`, `live`, `rep`, `dead`, `group_of` and `groups`.

## What changed from `ptp_v1`
**Removed**
- `engine/` (Jinja templates; `build_rtl_countluts.py` about 1,050 lines; `utils.py` about 800 lines).
- The Makefile, `run_vivado.tcl`, `vivado.xdc`, the Quartus scripts and the `*_extract_resources.py` scripts.
- `artifact_evaluation/` and `artifact_out/`.
- `synthetic/`, the graph-JSON frontend (`build_mask.py`) and `engine/p4_to_rtl.py`.
- The generator options `tree`, `unique`, `retime`, `col_per_file` and `cm`. Only `--or-encode`, `char_per_col` and `--data-width` remain.

**Semantics**
- **First row wins.** Overlapping rows are legal; earlier rows mask later ones (`Design.masks`).
- **Columns** are cut at flit boundaries. A column whose segments can be proven not to overlap stores a binary pseudo-ID; otherwise it stores one bit per segment (one-hot).
- **PIR format** is stricter:
  - a file must be all form A or all form B, and a row can't carry both an id and tags;
  - a token may not straddle a flit;
  - RANGE is `lo <= v < hi`;
  - port tokens are static inputs.
- **Interface:** streamed input with valid/ready and start/end-of-packet markers. The result appears 2 cycles after end-of-packet, and a new packet is accepted only once the output register is free. The old `axi_skid` / `hex_to_rtl_top` wrapper is gone.

**Frontends**
- Snort: essentially unchanged.
- P4: moved to `ptp/p4_to_pir.py`. A tag repeated on one path now keeps its last occurrence (it used to keep the first). The id width is now clog2(N+1).

**Branches**
- `origin/v2-pir-optimizer` (the PIR optimizer prototype, with per-row alive bits) is built on the old `engine/` layout. It has diverged from `main` and would need porting.

## Implications for an HLS backend
- **Where it plugs in:** a sibling of `emit.py` that reads `Design`. `emit._tok_expr` ports almost directly to `ap_uint` C++.
- **Testing:** reuse `golden.expected`, `vectors.make_packets` and `emit.emit_vector_files` to build a C++ simulation testbench. `parser_tb.sv` checks only output order and values, not cycle timing, so it can drive co-simulation through a thin port adapter.
- **Needed:**
  - a `--backend` switch in `run.py` (`generate()` hard-codes the calls today);
  - a `vitis_hls` runner;
  - synthesis/QoR scripts, rebuilt or recovered from `ptp_v1`.

## Where HLS can and can't help
- **It can't help much with logic minimization.** Vivado synthesis does that either way, and the column/pseudo-ID decomposition (the paper's contribution) should stay in Python.
- **It can help with:**
  - pipelining to a target clock (the RTL backend has no retiming option any more);
  - throughput: II per flit, and processing packets back to back (the RTL stalls between packets);
  - doing the lookup as unrolled logic or as a ROM (LUTRAM/BRAM/URAM), which is feasible when the total pseudo-ID width is small;
  - how state is stored (array partitioning or reshaping);
  - running form-B extraction as a separate dataflow stage;
  - different microarchitectures: pseudo-ID per column with a final lookup, vs a running per-row match bit.
- **How to keep these explorable:**
  - Python emits `constexpr` tables, and the C++ kernels are hand-written and templated;
  - the choices being swept live in tcl directives;
  - kernel variants are written in from the start.
- **Caveat:** fully unrolling loops over very large tables (Snort-sized) may make HLS compile time explode, so a flat-code fallback emitter is needed.

## Risks
- HLS compile time on large rule sets.
- The RTL's exact latency and ready behaviour can't be reproduced in HLS. Only output equivalence is realistic, and that is all the existing testbench checks.
- Windows wider than 1024 bits need `AP_INT_MAX_W` raised.
