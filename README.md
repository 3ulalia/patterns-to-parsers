# From Patterns to Parsers: Automatic Generation of Synthesizable SystemVerilog Parsers

[![Paper](https://img.shields.io/badge/arXiv-2607.16058-b31b1b.svg)](https://arxiv.org/abs/2607.16058)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Automatically generates synthesizable SystemVerilog parsers from high-level specifications — P4 programs, synthetic pattern tables, or Snort rules — without writing RTL by hand.

📄 **Paper:** [*From Patterns to Parsers: Automatic Generation of Efficient Hardware Parsers for FPGAs*](https://arxiv.org/abs/2607.16058) (arXiv:2607.16058)
🔬 **Reproducing the paper?** See [`artifact_evaluation/`](artifact_evaluation/README.md).

---

## Why this tool exists

Hardware packet parsers on FPGAs are traditionally hand-written RTL: tedious to author, hard to verify, and painful to re-target when the protocol set, data-path width, or FPGA family changes. This tool replaces that manual effort with a **push-button flow** that takes a high-level description of *what patterns to match* and emits **pipelined, width-parameterized, self-checking SystemVerilog** ready for Vivado or Quartus.

The key idea is a single, small **intermediate representation shared by every frontend** — we call it the **PIR (Pattern Intermediate Representation)**. Rather than teaching the RTL generator about P4, Snort, or any other source language, each frontend *lowers* its input to the PIR (a table of match patterns), and one backend turns the PIR into hardware. Adding a new input language means writing a frontend that emits the PIR — the entire RTL-generation, extraction, and testbench machinery is reused unchanged.

```
   ┌─────────────┐
   │  P4 program │──┐
   └─────────────┘  │
   ┌─────────────┐  │     ┌───────────────────────┐     ┌──────────────────────┐
   │ Snort rules │──┼───▶ │  PIR (pattern table)  │───▶ │  SystemVerilog RTL    │
   └─────────────┘  │     │  hex-pattern │ id      │     │  + self-checking TBs  │
   ┌─────────────┐  │     │  (two fields)          │     └──────────────────────┘
   │ Synthetic   │──┘     └───────────────────────┘
   │ hex tables  │              (the shared IR)          comparison / lookup /
   └─────────────┘                                       extractor / top / tb
```

---

## The PIR — a worked example

The PIR is a plain-text **pattern table** with two fields per line. The first field is always a hex pattern to match; the second is *either* a constant output id *or* a field-extraction directive:

```text
<hex-pattern>   <id>                             # form A: emit a constant id when the pattern matches
<hex-pattern>   <field_name>:<startpos>:<endpos>  # form B: extract input bits into a named output port
```

| Field | Meaning |
|---|---|
| `hex-pattern` | The bytes to match, one nibble per hex digit. `x` is a **wildcard** (don't-care nibble). |
| `id` | A SystemVerilog literal (`W'dN` / `W'hX`) — the value the parser outputs when a packet matches this pattern. |
| `field_name` | A user-defined ASCII name; the generated RTL uses it to name the output port. |
| `startpos` | Integer bit position where the field to extract begins (in the input data). |
| `endpos` | Integer bit position where the field to extract ends (in the input data). |

Every frontend lowers its input to exactly this. The backend then builds up to three combinational stages:

- **comparison** — matches the pattern nibbles. It does *not* emit the final result directly; per match column it emits a compact **pseudo-ID** (a local code for "which literal matched in this column").
- **lookup** — folds the per-column pseudo-IDs into the final `id`.
- **extractor** *(only for form B)* — uses the pseudo-IDs to slice the requested bits out of the input data into the named output ports.

```
                comparison               lookup           (optional) extractor
 data_in ─▶ match pattern nibbles ─▶ fold pseudo-IDs ─▶ final id ─┬─▶ id_out
             → pseudo-ID per column     → final id                │
                                                                  └─▶ selects which
                                                                      bytes to extract
```

### Example — classify a pattern to an id

A minimal PIR (form A). The hex literals in the first field are the pattern to look for in the incoming data; the second field is the value the RTL outputs when that pattern matches. `x` nibbles are don't-cares:

```text
0800xxxxxxx24xxx   4'hA
86ddxxxxxxxxxxxx   4'hB
1100xxx4xxxxxxxx   4'hC
```

The comparison stage emits `data_out` — a 2-bit **pseudo-ID** the tool auto-generates, one code per distinct pattern (not the final result):

```systemverilog
// comparison_logic_0.sv  (generated) — data_out is a 2-bit pseudo-ID
always_comb begin
  data_out = 0;
  if      ((data_in[63-:16] == 16'h0800) && (data_in[19-:8] == 8'h24)) data_out = 1;
  else if  (data_in[63-:16] == 16'h86dd)                               data_out = 2;
  else if ((data_in[63-:16] == 16'h1100) && (data_in[35-:4] == 4'h4))  data_out = 3;
end
```

The lookup stage maps that pseudo-ID to the final result. Its `data_in` is the comparison stage's `data_out`:

```systemverilog
// lookup_logic.sv  (generated)
always_comb begin
  data_out = '0;
  if      (data_in[1-:2] == 2'b01) data_out = 4'hA;
  else if (data_in[1-:2] == 2'b10) data_out = 4'hB;
  else if (data_in[1-:2] == 2'b11) data_out = 4'hC;
end
```

Here the parser is a pure classifier: a matched pattern in, a user-defined id out. Running this PIR through the real backend is a one-liner — see [Generating RTL from a raw PIR](#generating-rtl-from-a-raw-pir).

### Adding data extraction

Form B reuses the same patterns but replaces the id with a `field_name:startpos:endpos` extraction directive. This PIR produces two output ports: `id1` (8 bits wide, valid only when pattern 1 matches) and `id2` (4 bits wide, valid when pattern 2 or 3 matches). The comparison logic is unchanged — it depends only on the first field of the PIR:

```text
0800xxxxxxx24xxx   id1:12:19
86ddxxxxxxxxxxxx   id2:60:63
1100xxx4xxxxxxxx   id2:32:35
```

The extraction stage takes two inputs — the raw input data stream and the comparison stage's pseudo-ID — and drives each field port from the requested bit range:

```systemverilog
// data_extraction.sv  (generated)
always_comb begin
  id1 = '0;
  id2 = '0;
  if      (comparison_stage_out[1-:2] == 2'b01) id1 = data_in[19:12];
  else if (comparison_stage_out[1-:2] == 2'b10) id2 = data_in[63:60];
  else if (comparison_stage_out[1-:2] == 2'b11) id2 = data_in[35:32];
end
```

The PIR also supports user-defined **custom tokens** — a powerful way to express non-equality checks (ranges, negation, set membership) or to add user-defined input ports for comparison in the generated RTL; see the paper for details. This same field machinery is how the P4 flow lifts header fields: tag a P4 field name with `__<name>__` and the frontend emits an extraction directive for it (see `p4/simple_parser.p4`). Alongside these stages the backend also emits a width-parameterized **top module** (`hex_to_rtl_top.sv` / `parser_top.sv`) and **self-checking testbenches** for every design.

### Frontends

Because the PIR is the only thing the backend understands, adding an input language just means writing a **frontend** that emits it. Two ship today:

- **P4 frontend** (`engine/tools/p4_to_ir.py`) — compiles a P4 program with `p4c` and walks the resulting parse graph into a PIR.
- **Snort frontend** (`snort_frontend/`) — converts a `.txt` file of Snort rules into a PIR.

(Pre-computed synthetic hex tables can also be fed straight to the backend, no frontend needed.) The paper describes both frontends in more detail.

---

## Repository Structure

```
PatternToParser/
├── p4/                   # P4 source files (inputs for Flow 1)
│   ├── full_parser.p4    # Full parser: Ethernet + VLAN + MPLS + IPv4/IPv6 + TCP/UDP
│   └── simple_parser.p4  # Simple parser: Ethernet + IPv4/IPv6 + TCP/UDP
├── engine/               # Core RTL generation engine (frontends + backend)
│   ├── gen_parser.py     # Graph JSON → PIR → RTL
│   ├── p4_to_rtl.py      # P4 → PIR → RTL (end-to-end)
│   ├── tools/            # build_mask.py (PIR builder), build_rtl_countluts.py (backend), p4_to_ir.py (P4 frontend)
│   ├── utils/            # graph traversal, encoding, Jinja2 helpers
│   └── templates/        # Jinja2 SystemVerilog templates (comparison, lookup, extractor, top, tb)
├── snort_frontend/       # Snort rule frontend (Flow 3)
│   ├── snort.txt         # Example Snort rules (5 rules)
│   └── snort_10_rule.txt # Larger example (10 rules)
├── synthetic/            # Pre-generated synthetic hex benchmarks (inputs for Flow 2)
├── artifact_evaluation/  # Paper-reproduction: instructions, result-extraction scripts, plots
│   ├── README.md         # Step-by-step reproduction guide
│   ├── plots/            # Figure-generation scripts for the paper
│   └── *_extract.py      # Per-table/figure result-extraction scripts
├── artifact_out/         # Pre-generated RTL for all paper results (committed)
├── vivado_extract_resources.py   # Parse Vivado reports (used by `make extract`)
├── quartus_extract_resources.py  # Parse Quartus reports (used by `make extract`)
├── Makefile              # Push-button flows
├── requirements.txt
└── LICENSE
```

**Where things live, conceptually:**

| Stage | Code |
|---|---|
| Frontends (source → PIR) | `engine/tools/p4_to_ir.py` (P4), `snort_frontend/` (Snort), `synthetic/main.py` (synthetic), `engine/tools/build_mask.py` (graph JSON) |
| Backend (PIR → RTL) | `engine/tools/build_rtl_countluts.py` + `engine/templates/` |

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

## Using the Tool

This section is for users who want to generate parsers from their own inputs, experiment with different configurations, or run synthesis outside the artifact evaluation flows. Reproducing the paper's tables and figures? See [`artifact_evaluation/`](artifact_evaluation/README.md).

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
- `axi_skid.sv` — AXI skid buffer instantiated by the top modules

> **Tip:** To extract a field in the P4 flow, tag the header field name with `__<name>__` (e.g. `bit<8> protocol__prot__;`). The frontend turns it into a `prot:msb:lsb` extraction tag in the PIR. See `p4/simple_parser.p4`.

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

### Generating RTL from a raw PIR

You can also drive the backend directly with a PIR (`.hex`) and a small config JSON — this is the path used by the [worked example](#the-pir--a-worked-example) above and by Flows 2 and 3 internally:

```bash
PYTHONPATH=engine python3 engine/tools/build_rtl_countluts.py \
    -c config.json \
    -m example.pir \
    -o out --rtl --tree1 --tree2
```

The config JSON describes how the PIR columns map onto the data path:

```json
{
  "num_cols": 1,          // number of match columns
  "char_per_col": 16,     // hex chars (nibbles) per column
  "col_per_file": 1,      // columns grouped per comparison_logic file
  "data_width": 64,       // input bus width (bits)
  "max_width": 64,        // max packet width to parse (bits)
  "custom": []            // custom token predicates (RANGE / NE / EQOR)
}
```

Swap `--tree1 --tree2` for `--unique1 --unique2` to emit parallel (flat) comparators instead of a priority tree.

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
make clean        # remove ALL generated outputs: p4_out/, synthetic_out/, snort_out/,
                  #   artifact_out/, engine/tmp/, generated Snort files, and result CSVs
make clean-sim    # remove sim_build/ directories inside artifact_out/
make clean-synth  # remove synthesis project directories and .done markers
```

> **Note:** `make clean` also deletes `artifact_out/` (the committed pre-generated RTL). Restore it with `git checkout artifact_out/`, or regenerate via the [`artifact_evaluation/`](artifact_evaluation/README.md) `*-rtl` targets.

### Configuration variables

All variables can be overridden on the `make` command line.

| Variable | Default | Description |
|---|---|---|
| `VIVADO` | `vivado` | Path to the Vivado executable |
| `QUARTUS` | `quartus_sh` | Path to the Quartus Shell executable |
| `VERILATOR` | `verilator` | Path to the Verilator executable |
| `CONFIG` | _(all)_ | Limit a synth or sim sweep to one named subdirectory |
| `PART` | _(tool default)_ | FPGA part passed to Vivado / Quartus for synthesis |
| `SNORT_INPUT` | `snort_frontend/snort_10_rule.txt` | Snort rule file for `make snort` |
| `SNORT_WIDTH` | `128` | Maximum packet width in bytes for the Snort flow |
| `DWS` | `64 128 256 320 512` | Data widths swept in `make p4` |

**Examples:**

```bash
# Synthesize a generated design on a specific FPGA part
make synth TOOL=vivado DIR=p4_out/full_parser \
    VIVADO=/opt/Xilinx/Vivado/2023.2/bin/vivado PART=xcku025-ffva1156-1-c

# Run the Snort flow with a custom rule file and narrower packet width
make snort SNORT_INPUT=snort_frontend/snort.txt SNORT_WIDTH=64

# Sweep only a subset of data widths in Flow 1
make p4 DWS="64 256 512"
```

---

## Tool Limitations

Known limitations — useful context for anyone evaluating the tool or considering a contribution.

1. **Custom tokens cannot be split across group/column boundaries.** Hierarchical decomposition splits fixed-width columns to resolve false-overlap cases between literal and don't-care regions, recovering the matching semantics that decomposition would otherwise lose. However, this splitting has no notion of a custom symbolic token's value space (port/opcode/constant), so overlap analysis cannot be extended across custom tokens — a custom token must be treated as an opaque whole.

2. **A custom token must reside entirely within a single group/column — it cannot straddle a group or data-width boundary.** Token discovery matches a token's `id` string against a column-confined segment; if a column boundary cuts through the middle of a token it will not be recognized in either fragment. This constrains the choice of `num_groups` / group datawidth for patterns containing wide tokens.

3. **Fixed nibble-level (4-bit) granularity.** The tool decomposes and matches patterns at 4-bit resolution, which is standard for networking protocols. Extending to bit-level resolution would broaden applicability to domains such as genomics or signal processing, but would require reworking the column/segment model throughout the pipeline.

Beyond these, the token system currently supports only a fixed set of predicates (`RANGE`, `NE`, `EQOR`). Extending it to operations such as bit counting, parity checks, or masked comparisons — together with relaxing the cross-group token constraint above — would broaden applicability beyond networking. Design-space exploration currently requires synthesizing each candidate with commercial FPGA tools; integrating analytical resource models that predict LUT/FF counts directly from the PIR would enable fast architectural search without repeated synthesis runs.

---

## Contributing

Contributions are welcome — whether it's a new frontend, a backend improvement, more tests, or documentation.

**Good first areas to contribute** (see [Tool Limitations](#tool-limitations) for the deeper open problems):

- **New frontends.** Any source language can be supported by lowering it to the PIR (`<hex-pattern> <id>`). Model a new frontend on `engine/tools/p4_to_ir.py` or `snort_frontend/` — no backend changes needed.
- **New token predicates.** The comparison backend currently supports `RANGE`, `NE`, and `EQOR` (see `parse_custom_field` in `engine/utils/utils.py` and the predicate emitters in `build_rtl_countluts.py`). Adding predicates like masked-compare or popcount is self-contained.
- **Backend / template improvements.** RTL is generated from Jinja2 templates in `engine/templates/`; QoR (LUT/FF/Fmax) improvements land there and in `build_rtl_countluts.py`.
- **Analytical cost models.** Predicting resource usage directly from the PIR (rather than synthesizing) would enable fast design-space exploration.

Before opening a PR, regenerate any affected RTL and run the bundled self-checking testbenches — every generated design ships with a `hex_to_rtl_top_tb` (and, where applicable, a `parser_top_tb`), and a green `PASS` with no `MISMATCH` is the bar for a correctness-preserving change (see the simulation flow in [`artifact_evaluation/`](artifact_evaluation/README.md)). For larger changes such as a new frontend or backend rework, please open an issue first so we can align on approach.

---

## Citation

If you use this tool or build on it in academic work, please cite the paper:

```bibtex
@misc{garg2026patternsparsersautomaticgeneration,
      title={From Patterns to Parsers: Automatic Generation of Efficient Hardware Parsers for FPGAs},
      author={Tushar Garg and Andrew Boutros},
      year={2026},
      eprint={2607.16058},
      archivePrefix={arXiv},
      primaryClass={cs.AR},
      url={https://arxiv.org/abs/2607.16058},
}
```

---

## License

MIT — see [LICENSE](LICENSE).
