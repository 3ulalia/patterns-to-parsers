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

Output: `synthetic_out/{one_group,multi_group}_synthetic_u{256,512,1024}_uc{1,2,3,4}/`

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

## License

MIT — see [LICENSE](LICENSE).
