# Patterns to Parsers

[![Paper](https://img.shields.io/badge/arXiv-2607.16058-b31b1b.svg)](https://arxiv.org/abs/2607.16058)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Patterns to Parsers turns a table of match patterns (the **PIR**, a plain-text pattern table) into a synthesizable
Verilog packet parser for FPGAs, together with a self-checking testbench. The PIR can come from a P4 parser
(`ptp.p4_to_pir`), from Snort rules (`snort_frontend/`), or be written by hand.

**Paper:** [*From Patterns to Parsers: Automatic Generation of Efficient Hardware Parsers for FPGAs*](https://arxiv.org/abs/2607.16058) (arXiv:2607.16058).
The implementation used in the paper is on branch `ptp_v1`.

## What's new

- Correct results when patterns overlap: the first matching rule wins.
- Field extraction: pull fields (addresses, ports, ...) out of the packet into named output ports.
- Custom tokens: ranges, not-equal, and value sets inside a pattern.
- Valid/ready streaming interface with start/end-of-packet markers.
- Smaller, cleaner generated RTL, produced directly (no templates).
- Built-in LUT estimator that predicts area without running synthesis.
- Self-checking testbench with random packets, run with Verilator.

## Dependencies

- Python >= 3.9 and `pip install -r requirements.txt`
- [p4c](https://github.com/p4lang/p4c): only for P4 input (`ptp.p4_to_pir`); set `P4C` if not on `PATH`.
- [Verilator](https://www.veripool.org/verilator/): optional, for `--sim` and the simulation tests; set `VERILATOR` if not on `PATH`.
- [Yosys](https://github.com/YosysHQ/yosys) / Vivado: optional, to synthesize the generated RTL.

## PIR format

One rule per line; the **first matching rule wins**. The first field is a hex pattern, one character per nibble
(4 bits), in wire order starting at the first byte of the packet; `x` is a don't-care. The second field is either:

- form A: `<hex> <id>`, where `<id>` is a Verilog literal such as `4'd3`; the parser outputs this id.
- form B: `<hex> <name>:<msb>:<lsb>;<name>:<msb>:<lsb>...`; the parser extracts those bit ranges to output
  ports (bit `max_width-1` is the first bit on the wire).

A file uses one form only. The config is a JSON file:

```json
{
  "data_width": 64,
  "max_width": 384,
  "custom": [
    {"id": "GHIJ", "type": "const", "opcode": "RANGE", "values": ["1000", "2000"]}
  ]
}
```

`data_width` is the bits per input beat and `max_width` the pattern window in bits (both can also come from
`--data-width`). `char_per_col` / `num_cols` optionally control how the pattern is split into comparison columns.
`custom` declares tokens: an upper-case letter string in a pattern stands for a comparison (`RANGE` low/high,
`NE` value, `EQOR` any of a list of values). A tiny form-A example (window 32 bits, 16-bit EtherType-like field):

```text
xxxx0800 4'd1
xxxx86dd 4'd2
xxxxxxxx 4'd3
```

## Usage

```bash
python3 -m ptp.run <pir> <config.json> -o <outdir> [--sim] [--or-encode] [--char-per-col N] [--data-width N]
```

Writes to `<outdir>`: `parser.sv` (the parser), `parser_tb.sv` (self-checking testbench), `vec_*.hex`
(test vectors), and for form B also `extraction.sv` and `parser_top.sv` (use this as the top module).
`--sim` builds and runs the testbench with Verilator and prints `SIM PASS` or the number of mismatches.
`--or-encode` selects an alternative OR-based encoding of the result.

P4 frontend: `python3 -m ptp.p4_to_pir <prog.p4> --w <max_width_bits> [--dw 64] -o <outdir>` writes `<name>.pir` and `<name>.ptp.json`.

LUT estimator (analytical, no synthesis): `python3 -m ptp_est.estimate <pir> <config.json> [--or-encode]`.

## Examples

Run from the repository root (outputs go to the git-ignored `out/` folder of each example).

1. P4 to RTL with field extraction ([examples/01_p4_to_rtl](examples/01_p4_to_rtl)):
   ```bash
   python3 -m ptp.p4_to_pir examples/01_p4_to_rtl/simple_parser.p4 --w 768 --dw 64 -o examples/01_p4_to_rtl/out/pir
   python3 -m ptp.run examples/01_p4_to_rtl/out/pir/simple_parser.pir examples/01_p4_to_rtl/out/pir/simple_parser.ptp.json -o examples/01_p4_to_rtl/out/rtl --sim
   ```
2. Hand-written PIR ([examples/02_pir_to_rtl](examples/02_pir_to_rtl)):
   ```bash
   python3 -m ptp.run examples/02_pir_to_rtl/headers.pir examples/02_pir_to_rtl/headers.json -o examples/02_pir_to_rtl/out/rtl --sim
   python3 -m ptp_est.estimate examples/02_pir_to_rtl/headers.pir examples/02_pir_to_rtl/headers.json
   ```
3. Snort rules with custom tokens ([examples/03_custom_tokens](examples/03_custom_tokens)):
   ```bash
   python3 snort_frontend/create_snort_config.py --i examples/03_custom_tokens/rules.txt --o examples/03_custom_tokens/out/snort.pir --w 128
   python3 -m ptp.run examples/03_custom_tokens/out/snort.pir examples/03_custom_tokens/out/snort_config.json --data-width 64 -o examples/03_custom_tokens/out/rtl --sim
   ```

## Tests

```bash
python3 -m pytest ptp ptp_est
```

Tests that need Verilator or p4c are skipped if the tool is not found.

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

MIT, see [LICENSE](LICENSE).
