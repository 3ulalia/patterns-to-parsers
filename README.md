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
│   ├── full_parser.json  # Graph representation of the full parser
│   ├── simple_parser.json
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

## Synthesis (Optional)

Requires Vivado or Quartus installed. Pass the directory containing RTL subdirectories to synthesize.

```bash
# First generate RTL, then synthesize
make p4_full

# Vivado
make synth TOOL=vivado DIR=p4_out/full_parser VIVADO=/path/to/vivado

# Quartus
make synth TOOL=quartus DIR=p4_out/full_parser QUARTUS=/path/to/quartus_sh

# Extract resource utilization after synthesis
make extract TOOL=vivado DIR=p4_out/full_parser
```

---

## Artifact Evaluation

Each paper result is reproducible with a single `make` command. All commands follow the same pattern: RTL generation → synthesis → CSV extraction.

| Artifact | Tool | Command | Output |
|---|---|---|---|
| Table 1 | Vivado | `make table1` | `table1_results.csv` |
| Table 3 | Vivado + Quartus | `make table3` | `table3_results.csv` |
| Figure 6 | Quartus | `make fig6` | `fig6_results.csv` |
| Figure 7 | Vivado | `make fig7` | `fig7_results.csv` |
| Figure 9 | Vivado | `make fig9` | `fig9_results.csv` |

RTL generation steps can be run without a synthesis tool installed (`make table1-rtl`, `make table3-rtl`, `make fig-rtl`, `make fig9-rtl`). Figure 6 and Figure 7 share the same RTL (`artifact_out/fig/`) — only the synthesis tool differs.

---

### Table 1

Table 1 in the paper reports LUT count, flip-flop count, and Fmax for:

| Parser | Data Width | Config |
|---|---|---|
| Simple Parser | 256 b, 320 b | Unique, Tree |
| Full Parser | 64 b, 320 b, 512 b | Unique, Tree |

```bash
make table1                      # full pipeline (Vivado)
```

Individual steps (optional):

```bash
make table1-rtl      # generate RTL only (no Vivado needed)
make table1-synth    # run Vivado on all 10 configs
make table1-extract  # parse reports → table1_results.csv
```

Output:
- `artifact_out/table1/{parser}_dw{width}_{mode}/` — generated RTL and Vivado project for each config
- `table1_results.csv` — aggregated results matching Table 1

---

### Table 3

Table 3 reports Fmax, LUT (Vivado), ALM (Quartus), and FF for the Snort 10-rule parser on both tools.

```bash
make table3                                          # full pipeline (both Vivado and Quartus)
```

Individual steps (optional):

```bash
make table3-rtl            # generate RTL (no tool needed)
make table3-synth-vivado   # Vivado synthesis
make table3-synth-quartus  # Quartus synthesis
make table3-extract        # parse both reports → table3_results.csv
```

Output: `artifact_out/table3/snort_10rule/` containing both `vivado_proj/` and `quartus_proj/`, and `table3_results.csv` with Vivado and Quartus results on separate rows.

---

### Figures 6 & 7

Both figures sweep Simple and Full parser across all data widths (64, 128, 256, 320, 512 b) in the **unique** configuration. Figure 6 uses Quartus (Intel/Altera), Figure 7 uses Vivado (AMD/Xilinx).

The RTL is identical for both figures — only the synthesis tool changes:

```bash
make fig-rtl   # generate RTL once (no synthesis tool needed)
make fig6      # Quartus synthesis + extract → fig6_results.csv
make fig7      # Vivado synthesis  + extract → fig7_results.csv
```

Individual steps (optional):

```bash
make fig6-synth    # Quartus synthesis only
make fig6-extract  # parse Quartus reports → fig6_results.csv
make fig7-synth    # Vivado synthesis only
make fig7-extract  # parse Vivado reports  → fig7_results.csv
```

---

### Figure 9

Figure 9 sweeps the synthetic benchmarks across two parser graph configurations and four uniqueness group counts, using the **unique** implementation style (unique1 + unique2).

| Dimension | Values |
|---|---|
| Config | flat (`one_group.json`), decomposed (`multi_group.json`) |
| Width | 256, 512, 1024 bits |
| Groups | ug1, ug2, ug3, ug4 |

```bash
make fig9          # full pipeline (Vivado)
```

Individual steps (optional):

```bash
make fig9-rtl      # generate RTL (no tool needed) → artifact_out/fig9/
make fig9-synth    # Vivado synthesis
make fig9-extract  # parse reports → fig9_results.csv
```

Note: the synthetic hex files use the naming convention `synthetic_u{width}_ug{n}.hex` (ug = uniqueness group).

> **Runtime:** `fig9-synth` runs Vivado on 24 configurations and takes approximately 6–7 hours to complete.

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
