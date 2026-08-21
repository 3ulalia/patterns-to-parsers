# Artifact Evaluation

Reproduction instructions for the results in *[From Patterns to Parsers: Automatic Generation of Efficient Hardware Parsers for FPGAs](https://arxiv.org/abs/2607.16058)* (arXiv:2607.16058).

> **Run all commands from the repository root** (one level up from this folder). The `make` targets, the committed pre-generated RTL in `artifact_out/`, and the result CSVs all live at the repo root. See the top-level [README](../README.md) for what the tool does and how it works.

---

## Prerequisites

| Tool | Needed for | Notes |
|---|---|---|
| Python + `jinja2` | RTL generation | `pip install -r ../requirements.txt` |
| p4c | Regenerating the P4-derived artifacts (Table 1, Fig 6/7) | Only if you rebuild RTL; `artifact_out/` is committed |
| Verilator | Simulation (Step 2) | Pass `VERILATOR=/path/to/verilator` if not on `PATH` |
| Vivado | Synthesis (Table 1, Fig 7, Fig 9, Table 3) | Pass `VIVADO=/path/to/vivado` if not on `PATH` |
| Quartus | Synthesis (Fig 6, Table 3) | Pass `QUARTUS=/path/to/quartus_sh` if not on `PATH` |

See the top-level README's [Requirements](../README.md#requirements) section for installation details.

---

## Overview

`artifact_out/` (at the repo root) is committed with pre-generated RTL and self-checking testbenches for all paper results. The typical flow is: **RTL → Simulate → Synthesize**.

| Artifact | Contents | Synthesis tool | Output CSV |
|---|---|---|---|
| `artifact_out/table1/` | Simple Parser (256b, 320b) + Full Parser (64b, 320b, 512b) × {tree, unique} — 10 configs | Vivado | `table1_results.csv` |
| `artifact_out/fig_6_7/` | Simple + Full parser × 5 data widths, unique mode — 10 configs | Quartus (Fig 6) / Vivado (Fig 7) | `fig6_results.csv`, `fig7_results.csv` |
| `artifact_out/table3/` | Snort 10-rule parser | Vivado + Quartus | `table3_results.csv` |
| `artifact_out/fig9/` | Synthetic benchmarks: flat + decomposed × 3 widths × 4 ug — 24 configs | Vivado | `fig9_results.csv` |

---

## Step 1 — RTL Generation

`artifact_out/` is already committed and ready to use. Regenerate it only if needed (e.g. after modifying templates):

```bash
make table1-rtl    # → artifact_out/table1/
make table3-rtl    # → artifact_out/table3/snort_10rule/
make fig_6_7-rtl   # → artifact_out/fig_6_7/   (shared by Fig 6 & 7)
make fig9-rtl      # → artifact_out/fig9/
```

## Step 2 — Simulation

Each design ships with self-checking testbenches (`hex_to_rtl_top_tb` and `parser_top_tb`). Run all artifact simulations:

```bash
make sim VERILATOR=/path/to/verilator
```

Or per artifact:

```bash
make sim-table1   VERILATOR=/path/to/verilator
make sim-table3   VERILATOR=/path/to/verilator
make sim-fig_6_7  VERILATOR=/path/to/verilator
make sim-fig9     VERILATOR=/path/to/verilator
```

Pass `CONFIG=<subdir-name>` to run a single configuration instead of the full sweep:

```bash
make sim-table1  CONFIG=full_dw64_tree      VERILATOR=/path/to/verilator
make sim-fig_6_7 CONFIG=simple_dw128_unique VERILATOR=/path/to/verilator
make sim-fig9    CONFIG=flat_u256_ug1       VERILATOR=/path/to/verilator
```

Each config compiles into `<design_dir>/sim_build/` (not committed). Any `MISMATCH` in the output causes a non-zero exit. A VCD waveform (`<tb_name>.vcd`) is written to `sim_build/` alongside the binary.

## Step 3 — Synthesis

### Table 1

LUT count, FF count, and Fmax for Simple Parser (256b, 320b) and Full Parser (64b, 320b, 512b) in both unique and tree configurations.

```bash
make table1-synth   VIVADO=/path/to/vivado PART=xc7vx690tffg1761-2 # all 10 configs, Virtex 7 FPGA Part to match previous papers
make table1-synth   VIVADO=/path/to/vivado CONFIG=full_dw64_tree  # single config
make table1-extract                                               # → table1_results.csv
```

### Table 3

Fmax, LUT (Vivado), ALM (Quartus), and FF for the Snort 10-rule parser on both toolchains.

```bash
make table3-synth-vivado   VIVADO=/path/to/vivado
make table3-synth-quartus  QUARTUS=/path/to/quartus_sh
make table3-extract                                                # → table3_results.csv
```

### Figure 6

ALM count and Fmax (Quartus / Intel Agilex 5) sweeping Simple and Full parser across all data widths.

```bash
make fig6-synth   QUARTUS=/path/to/quartus_sh                          # all 10 configs
make fig6-synth   QUARTUS=/path/to/quartus_sh CONFIG=full_dw128_unique # single config
make fig6-extract                                                        # → fig6_results.csv
```

### Figure 7

LUT count and Fmax (Vivado / AMD Versal) for the same configurations as Figure 6. Shares the same RTL as Figure 6.

```bash
make fig7-synth   VIVADO=/path/to/vivado                          # all 10 configs
make fig7-synth   VIVADO=/path/to/vivado CONFIG=full_dw128_unique # single config
make fig7-extract                                                  # → fig7_results.csv
```

### Figure 9

LUT count and Fmax (Vivado) for synthetic benchmarks across two graph configs (flat, decomposed), three data widths (256, 512, 1024b), and four uniqueness groups — 24 configurations total.

```bash
make fig9-synth   VIVADO=/path/to/vivado                       # all 24 configs (~6–7 hours)
make fig9-synth   VIVADO=/path/to/vivado CONFIG=flat_u256_ug1  # single config
make fig9-extract                                               # → fig9_results.csv
```

---

For generating parsers from your own inputs (P4, Snort, synthetic, or a raw PIR), see [Using the Tool](../README.md#using-the-tool) in the top-level README.
