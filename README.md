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
├── artifact_out/         # Pre-generated RTL for all paper results (committed)
├── Makefile              # Push-button flows
├── requirements.txt
└── LICENSE
```

---

## Requirements

### Python dependencies

```bash
pip install -r requirements.txt   # jinja2 + snort frontend deps
```

### p4c (required only for Flow 1 — P4 → RTL)

```bash
# Ubuntu/Debian
sudo apt-get install p4lang-p4c

# macOS (Homebrew)
brew install p4lang/p4/p4c

# Or build from source: https://github.com/p4lang/p4c
```

Verify: `p4c --version`

### Verilator (required for simulation)

Install via [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) (recommended) or your system package manager.

Verify: `verilator --version`

### Vivado / Quartus (required for synthesis)

If already on your `PATH`, omit the `VIVADO=` / `QUARTUS=` overrides in the commands below.

---

## Artifact Evaluation

`artifact_out/` is committed with pre-generated RTL and self-checking testbenches for all paper results. The typical flow is: **RTL → Simulate → Synthesize**.

| Artifact | Contents | Synthesis tool | Output CSV |
|---|---|---|---|
| `artifact_out/table1/` | Simple Parser (256b, 320b) + Full Parser (64b, 320b, 512b) × {tree, unique} — 10 configs | Vivado | `table1_results.csv` |
| `artifact_out/fig_6_7/` | Simple + Full parser × 5 data widths, unique mode — 10 configs | Quartus (Fig 6) / Vivado (Fig 7) | `fig6_results.csv`, `fig7_results.csv` |
| `artifact_out/table3/` | Snort 10-rule parser | Vivado + Quartus | `table3_results.csv` |
| `artifact_out/fig9/` | Synthetic benchmarks: flat + decomposed × 3 widths × 4 ug — 24 configs | Vivado | `fig9_results.csv` |

### Step 1 — RTL Generation

`artifact_out/` is already committed and ready to use. Regenerate it only if needed (e.g. after modifying templates):

```bash
make table1-rtl    # → artifact_out/table1/
make table3-rtl    # → artifact_out/table3/snort_10rule/
make fig_6_7-rtl   # → artifact_out/fig_6_7/   (shared by Fig 6 & 7)
make fig9-rtl      # → artifact_out/fig9/
```

### Step 2 — Simulation

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

### Step 3 — Synthesis

#### Table 1

LUT count, FF count, and Fmax for Simple Parser (256b, 320b) and Full Parser (64b, 320b, 512b) in both unique and tree configurations.

```bash
make table1-synth   VIVADO=/path/to/vivado PART=xc7vx690tffg1761-2 # all 10 configs, Virtex 7 FPGA Part to match previous papers
make table1-synth   VIVADO=/path/to/vivado CONFIG=full_dw64_tree  # single config
make table1-extract                                               # → table1_results.csv
```

#### Table 3

Fmax, LUT (Vivado), ALM (Quartus), and FF for the Snort 10-rule parser on both toolchains.

```bash
make table3-synth-vivado   VIVADO=/path/to/vivado
make table3-synth-quartus  QUARTUS=/path/to/quartus_sh
make table3-extract                                                # → table3_results.csv
```

#### Figure 6

ALM count and Fmax (Quartus / Intel Agilex 5) sweeping Simple and Full parser across all data widths.

```bash
make fig6-synth   QUARTUS=/path/to/quartus_sh                          # all 10 configs
make fig6-synth   QUARTUS=/path/to/quartus_sh CONFIG=full_dw128_unique # single config
make fig6-extract                                                        # → fig6_results.csv
```

#### Figure 7

LUT count and Fmax (Vivado / AMD Versal) for the same configurations as Figure 6. Shares the same RTL as Figure 6.

```bash
make fig7-synth   VIVADO=/path/to/vivado                          # all 10 configs
make fig7-synth   VIVADO=/path/to/vivado CONFIG=full_dw128_unique # single config
make fig7-extract                                                  # → fig7_results.csv
```

#### Figure 9

LUT count and Fmax (Vivado) for synthetic benchmarks across two graph configs (flat, decomposed), three data widths (256, 512, 1024b), and four uniqueness groups — 24 configurations total.

```bash
make fig9-synth   VIVADO=/path/to/vivado                       # all 24 configs (~6–7 hours)
make fig9-synth   VIVADO=/path/to/vivado CONFIG=flat_u256_ug1  # single config
make fig9-extract                                               # → fig9_results.csv
```

---

## Using the Tool

This section is for users who want to generate parsers from their own inputs, experiment with different configurations, or run synthesis outside the artifact evaluation flows.

### Flow 1 — P4 → RTL

Compiles the bundled P4 parsers to SystemVerilog, sweeping data widths of 64, 128, 256, 320, and 512 bits.

```bash
make p4                # both parsers, all widths → p4_out/
make p4_full           # full_parser.p4  → p4_out/full_parser/dw_{64,128,256,320,512}/
make p4_simple         # simple_parser.p4 → p4_out/simple_parser/dw_{64,128,256,320,512}/
```

Each output directory contains:
- `comparison_logic_*.sv` — per-column pattern comparators
- `lookup_logic.sv` — path ID lookup
- `mask_extractor.sv` — field extractor
- `hex_to_rtl_top.sv` + `hex_to_rtl_top_tb.sv` — top-level and testbench
- `parser_top.sv` + `parser_top_tb.sv` — full parser top and testbench

### Flow 2 — Synthetic Benchmarks → RTL

Generates RTL from pre-computed synthetic hex tables. No p4c required.

```bash
make synthetic
```

Output: `synthetic_out/{one_group,multi_group}_synthetic_u{256,512,1024}_ug{1,2,3,4}/`

### Flow 3 — Snort Rules → RTL

Converts a Snort rule text file to a packet-matching RTL.

```bash
make snort                                               # uses snort_frontend/snort_10_rule.txt
make snort SNORT_INPUT=snort_frontend/snort.txt          # custom rule file
```

Output: `snort_out/`

### Run all generation flows

```bash
make all
```

### Synthesis on custom output directories

The `synth` and `extract` targets are not tied to the artifact flows — point them at any directory of RTL subdirectories:

```bash
make synth   TOOL=vivado  DIR=p4_out/full_parser VIVADO=/path/to/vivado
make extract TOOL=vivado  DIR=p4_out/full_parser
make synth   TOOL=quartus DIR=p4_out/full_parser QUARTUS=/path/to/quartus_sh
```

### Clean

```bash
make clean        # remove generated RTL output directories (p4_out/, synthetic_out/, snort_out/)
make clean-sim    # remove sim_build/ directories inside artifact_out/
make clean-synth  # remove synthesis project directories and .done markers
```

### Configuration variables

All variables can be overridden on the `make` command line.

| Variable | Default | Description |
|---|---|---|
| `VIVADO` | `vivado` | Path to the Vivado executable |
| `QUARTUS` | `quartus_sh` | Path to the Quartus Shell executable |
| `VERILATOR` | `verilator` | Path to the Verilator executable |
| `CONFIG` | _(all)_ | Limit a synth or sim sweep to one named subdirectory |
| `TABLE1_PART` | `xcvc1902-vsva2197-3HP-e-S` | FPGA part passed to Vivado for Table 1 |
| `SNORT_INPUT` | `snort_frontend/snort_10_rule.txt` | Snort rule file for `make snort` / `make table3-rtl` |
| `SNORT_WIDTH` | `128` | Maximum packet width in bytes for the Snort flow |
| `DWS` | `64 128 256 320 512` | Data widths swept in `make p4` |

**Examples:**

```bash
# Synthesize Table 1 on a different FPGA part
make table1-synth VIVADO=/opt/Xilinx/Vivado/2023.2/bin/vivado TABLE1_PART=xcku025-ffva1156-1-c

# Run the Snort flow with a custom rule file and narrower packet width
make snort SNORT_INPUT=snort_frontend/snort.txt SNORT_WIDTH=64

# Sweep only a subset of data widths in Flow 1
make p4 DWS="64 256 512"

# Override the Quartus target device (Agilex 5 is the default)
quartus_sh -t run_quartus.tcl artifact_out/table3/snort_10rule 10AX115S2F45I1SG
```

---

## Tool Limitations

Known limitations — useful context for anyone evaluating the tool or considering a contribution.

1. **Custom tokens cannot be split across group/column boundaries.** Hierarchical decomposition splits fixed-width columns to resolve false-overlap cases between literal and don't-care regions, recovering the matching semantics that decomposition would otherwise lose. However, this splitting has no notion of a custom symbolic token's value space (port/opcode/constant), so overlap analysis cannot be extended across custom tokens — a custom token must be treated as an opaque whole.

2. **A custom token must reside entirely within a single group/column — it cannot straddle a group or data-width boundary.** Token discovery matches a token's `id` string against a column-confined segment; if a column boundary cuts through the middle of a token it will not be recognized in either fragment. This constrains the choice of `num_groups` / group datawidth for patterns containing wide tokens.

3. **Fixed nibble-level (4-bit) granularity.** The tool decomposes and matches patterns at 4-bit resolution, which is standard for networking protocols. Extending to bit-level resolution would broaden applicability to domains such as genomics or signal processing, but would require reworking the column/segment model throughout the pipeline.

Beyond these, the token system currently supports only a fixed set of predicates (`RANGE`, `NE`, `EQOR`). Extending it to operations such as bit counting, parity checks, or masked comparisons — together with relaxing the cross-group token constraint above — would broaden applicability beyond networking. Design-space exploration currently requires synthesizing each candidate with commercial FPGA tools; integrating analytical resource models that predict LUT/FF counts directly from the PIR would enable fast architectural search without repeated synthesis runs.

---

## License

MIT — see [LICENSE](LICENSE).
