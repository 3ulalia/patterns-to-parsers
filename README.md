# From Patterns to Parsers: Automatic Generation of Synthesizable SystemVerilog

Automatically generates synthesizable SystemVerilog parsers from high-level specifications — P4 programs, synthetic pattern tables, or Snort rules — without writing RTL by hand.

## Repository Structure

```
PatternToParser/
├── p4/                   # P4 source files (inputs for Flow 1)
│   ├── full_parser.p4    # Full parser: Ethernet + VLAN + MPLS + IPv4/IPv6 + TCP/UDP
│   └── simple_parser.p4  # Simple parser: Ethernet + IPv4/IPv6 + TCP/UDP
├── engine/               # Core RTL generation engine
│   ├── gen_parser.py     # JSON graph → RTL
│   ├── p4_to_rtl.py      # P4 → RTL (end-to-end)
│   ├── tools/            # build_mask.py, build_rtl_countluts.py, p4_to_ir.py
│   ├── utils/
│   └── templates/        # Jinja2 SystemVerilog templates
├── synthetic/            # Pre-generated synthetic hex benchmarks (inputs for Flow 2)
├── snort_frontend/       # Snort rule parser (inputs for Flow 3)
│   ├── snort.txt         # Example Snort rules (5 rules)
│   └── snort_10_rule.txt # Larger example (10 rules)
├── Makefile              # Push-button flows
├── requirements.txt
└── LICENSE
```

**Generated outputs** (created by `make`, not committed):

| Directory | Contents |
|---|---|
| `p4_out/` | RTL from Flow 1 (P4 input) |
| `synthetic_out/` | RTL from Flow 2 (synthetic input) |
| `snort_out/` | RTL from Flow 3 (Snort input) |

**Pre-generated artifact RTL** (committed, ready for synthesis):

| Directory | Contents |
|---|---|
| `artifact_out/table1/` | RTL for all 10 Table 1 configurations |
| `artifact_out/fig_6_7/` | RTL for Figures 6 & 7 (all parsers × data widths, unique mode) |
| `artifact_out/table3/` | RTL for Table 3 (Snort 10-rule parser) |
| `artifact_out/fig9/` | RTL for Figure 9 (synthetic benchmarks, 24 configurations) |

---

## Requirements

### Python dependencies
- Python 3.8+
- [jinja2](https://pypi.org/project/Jinja2/) — for RTL template rendering

```bash
pip install -r requirements.txt
```

### p4c (required only for Flow 1 — P4 → RTL)
Install the P4 reference compiler:

```bash
# Ubuntu/Debian
sudo apt-get install p4lang-p4c

# macOS (Homebrew)
brew install p4lang/p4/p4c

# Or build from source: https://github.com/p4lang/p4c
```

Verify: `p4c --version`

---

## Reproducing Paper Results

### Flow 1 — P4 → RTL

Compiles the full and simple P4 parsers to SystemVerilog, sweeping data widths of 64, 128, 256, 320, and 512 bits.

```bash
make p4
```

Or individually:

```bash
make p4_full     # full_parser.p4  → p4_out/full_parser/dw_{64,128,256,320,512}/
make p4_simple   # simple_parser.p4 → p4_out/simple_parser/dw_{64,128,256,320,512}/
```

Each output directory contains:
- `comparison_logic_*.sv` — per-column pattern comparators
- `lookup_logic.sv` — path ID lookup
- `mask_extractor.sv` — field extractor
- `hex_to_rtl_top.sv` + `hex_to_rtl_top_tb.sv` — top-level and testbench
- `parser_top.sv` — full parser top connecting all modules

### Flow 2 — Synthetic Benchmarks → RTL

Generates RTL from pre-computed synthetic hex tables (no p4c needed).

```bash
make synthetic
```

Output: `synthetic_out/{one_group,multi_group}_synthetic_u{256,512,1024}_ug{1,2,3,4}/`

### Flow 3 — Snort Rules → RTL

Converts a Snort rule text file to a packet-matching RTL.

```bash
make snort
```

To use a different rule file:

```bash
make snort SNORT_INPUT=snort_frontend/snort_10_rule.txt
```

Output: `snort_out/`

### Run all flows

```bash
make all
```

### Clean generated outputs

```bash
make clean
```

---

## Artifact Evaluation

`artifact_out/` is committed to the repository with pre-generated RTL for all paper results — no p4c needed to reach the synthesis step. If Vivado or Quartus is already on your `PATH`, omit the `VIVADO=` / `QUARTUS=` overrides below.

| Artifact | Tool | Output |
|---|---|---|
| Table 1 | Vivado | `table1_results.csv` |
| Table 3 | Vivado + Quartus | `table3_results.csv` |
| Figure 6 | Quartus | `fig6_results.csv` |
| Figure 7 | Vivado | `fig7_results.csv` |
| Figure 9 | Vivado | `fig9_results.csv` |

---

### Table 1

LUT count, flip-flop count, and Fmax for Simple Parser (256 b, 320 b) and Full Parser (64 b, 320 b, 512 b) in both unique and tree configurations (10 configs total), synthesized with Vivado.

```bash
make table1-rtl                              # generate RTL → artifact_out/table1/
make table1-synth  VIVADO=/path/to/vivado   # synthesize all 10 configs
make table1-extract                          # parse reports → table1_results.csv
```

---

### Table 3

Fmax, LUT (Vivado), ALM (Quartus), and FF for the Snort 10-rule parser on both FPGA toolchains.

```bash
make table3-rtl                                # generate RTL → artifact_out/table3/snort_10rule/
make table3-synth-vivado  VIVADO=/path/to/vivado
make table3-synth-quartus QUARTUS=/path/to/quartus_sh
make table3-extract                            # parse both reports → table3_results.csv
```

---

### Figure 6

ALM count and Fmax (Quartus / Intel Agilex 5) sweeping Simple and Full parser across all data widths (64, 128, 256, 320, 512 b) in the unique configuration.

```bash
make fig_6_7-rtl                                  # generate RTL → artifact_out/fig_6_7/
make fig6-synth  QUARTUS=/path/to/quartus_sh  # synthesize all 10 configs
make fig6-extract                             # parse reports → fig6_results.csv
```

---

### Figure 7

LUT count and Fmax (Vivado / AMD Versal) sweeping the same configurations as Figure 6. Shares the same RTL — skip `fig-rtl` if already generated for Figure 6.

```bash
make fig_6_7-rtl                               # skip if already done for Figure 6
make fig7-synth  VIVADO=/path/to/vivado   # synthesize all 10 configs
make fig7-extract                          # parse reports → fig7_results.csv
```

---

### Figure 9

LUT count and Fmax (Vivado) for synthetic benchmarks across two parser graph configurations (flat, decomposed), three data widths (256, 512, 1024 b), and four uniqueness group counts (ug1–ug4) — 24 configurations total.

```bash
make fig9-rtl                              # generate RTL → artifact_out/fig9/
make fig9-synth  VIVADO=/path/to/vivado   # synthesize 24 configs (~6–7 hours)
make fig9-extract                          # parse reports → fig9_results.csv
```

---

## Simulation

Each generated design includes self-checking testbenches that can be run with [Verilator](https://www.veripool.org/verilator/) — no commercial tools required.

Install via [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) (recommended) or your system package manager, then verify:

```bash
verilator --version
```

Run all artifact simulations:

```bash
make sim VERILATOR=/path/to/verilator
```

Or per artifact:

```bash
make sim-table1   # hex_to_rtl_top_tb + parser_top_tb for all 10 Table 1 configs
make sim-table3   # hex_to_rtl_top_tb for the Snort 10-rule parser
make sim-fig_6_7  # hex_to_rtl_top_tb + parser_top_tb for all 10 Fig 6 & 7 configs
make sim-fig9     # hex_to_rtl_top_tb for all 24 Fig 9 configs (large files, slow compile)
```

Pass `CONFIG=<subdir-name>` to run a single configuration instead of the whole sweep:

```bash
make sim-table1  CONFIG=full_dw64_tree
make sim-fig_6_7 CONFIG=simple_dw128_unique
make sim-fig9    CONFIG=flat_u256_ug1
```

Each config is compiled into `<design_dir>/sim_build/` and run immediately. Any `MISMATCH` in the output causes a non-zero exit. Remove build artifacts with `make clean-sim`.

If Verilator is already on your `PATH`, omit the `VERILATOR=` override.

---

## Configuration

All variables can be overridden on the `make` command line.

| Variable | Default | Description |
|---|---|---|
| `VIVADO` | `vivado` | Path to the Vivado executable |
| `QUARTUS` | `quartus_sh` | Path to the Quartus Shell executable |
| `VERILATOR` | `verilator` | Path to the Verilator executable |
| `TABLE1_PART` | `xcvc1902-vsva2197-3HP-e-S` | Target FPGA part string passed to Vivado for Table 1 synthesis |
| `SNORT_INPUT` | `snort_frontend/snort_10_rule.txt` | Snort rule file used by `make snort` and `make table3-rtl` |
| `SNORT_WIDTH` | `128` | Maximum packet width in bytes for the Snort flow |
| `DWS` | `64 128 256 320 512` | Data widths swept in `make p4` (Flow 1) |

**Examples:**

```bash
# Synthesize Table 1 on a different FPGA part
make table1-synth VIVADO=/opt/Xilinx/Vivado/2023.2/bin/vivado TABLE1_PART=xcku025-ffva1156-1-c

# Run the Snort flow with a custom rule file and packet width
make snort SNORT_INPUT=snort_frontend/snort.txt SNORT_WIDTH=64

# Sweep a different set of data widths in Flow 1
make p4 DWS="64 256 512"
```

**Quartus device override:** The default Quartus device (`A5DD064AB32BI1V`, Agilex 5) is set in `run_quartus.tcl`. Pass a different device as the second argument when invoking the script directly:

```bash
quartus_sh -t run_quartus.tcl artifact_out/table3/snort_10rule 10AX115S2F45I1SG
```

**Generic synthesis target:** The `synth` and `extract` make targets accept any directory of RTL subdirectories and are not tied to the artifact flows:

```bash
make synth   TOOL=vivado  DIR=p4_out/full_parser VIVADO=/path/to/vivado
make extract TOOL=vivado  DIR=p4_out/full_parser
make synth   TOOL=quartus DIR=p4_out/full_parser QUARTUS=/path/to/quartus_sh
```

---

## Tool Limitations

This section documents known limitations of the current implementation —
useful context for anyone evaluating the tool or considering a contribution
that addresses one of these areas.

1. **Custom (user-defined) tokens cannot be split across group/column
   boundaries.** Hierarchical decomposition splits fixed-width columns to
   resolve false-overlap cases between literal and don't-care regions (see
   `refine_char_per_col` in `engine/utils/utils.py`), recovering the matching
   semantics that decomposition would otherwise lose. However, this splitting
   only operates on hex/wildcard content — it has no notion of a custom
   symbolic token's value space (port/opcode/constant), so overlap analysis
   cannot be extended across custom tokens, and a custom token must be
   decomposed/considered as an opaque whole.

2. **A custom token must reside entirely within a single group/column —
   it cannot straddle a group or data-width boundary.** Token discovery
   matches a token's `id` string against a column-confined segment
   (`extract_segments_and_tokens` in `engine/tools/build_rtl_countluts.py`);
   if a column boundary cuts through the middle of a token, it will not be
   recognized in either fragment. This constrains the choice of `num_groups`
   / group datawidth for patterns containing wide tokens.

3. **Fixed nibble-level (4-bit) granularity.** The tool currently decomposes
   and matches patterns at a 4-bit resolution, which is the standard
   granularity for networking protocols. Extending to bit-level resolution
   would broaden applicability to domains such as genomics or signal
   processing, but would require reworking the column/segment model
   throughout the pipeline.

Beyond these, the token system currently supports only a fixed set of
predicates (`RANGE`, `NE`, `EQOR`); extending it to operations such as bit
counting, parity checks, or masked comparisons — together with relaxing the
cross-group token constraint above — would broaden applicability to domains
beyond networking. Likewise, design-space exploration currently requires
synthesizing each candidate configuration with commercial FPGA tools;
integrating analytical resource models that predict LUT/FF counts directly
from the PIR would enable fast architectural search without repeated
synthesis runs. Contributions in any of these areas are very welcome.

---

## License

MIT — see [LICENSE](LICENSE).
