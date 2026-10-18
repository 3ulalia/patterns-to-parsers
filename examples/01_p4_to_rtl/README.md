# Example 1: P4 to RTL (with field extraction)

`simple_parser.p4` parses Ethernet / IPv4 / IPv6 (with up to two extension headers) / TCP / UDP / ICMP.
Fields tagged in the P4 source (`__sip__`, `__dip__`, `__prot__`, `__sp__`, `__dp__`) become output ports.
Requires `p4c`; `--sim` additionally requires Verilator.

From the repository root:

```bash
# 1. P4 -> PIR (max packet window 768 bits, 64-bit data path)
python3 -m ptp.p4_to_pir examples/01_p4_to_rtl/simple_parser.p4 --w 768 --dw 64 -o examples/01_p4_to_rtl/out/pir

# 2. PIR -> RTL (+ self-checking Verilator simulation)
python3 -m ptp.run examples/01_p4_to_rtl/out/pir/simple_parser.pir \
    examples/01_p4_to_rtl/out/pir/simple_parser.ptp.json -o examples/01_p4_to_rtl/out/rtl --sim
```

Outputs go to `out/` (git-ignored): `out/pir/simple_parser.pir` (form B rows) and `.ptp.json`, and in
`out/rtl/`: `parser.sv`, `extraction.sv`, `parser_top.sv` (use this as the top module), `parser_tb.sv`, `vec_*.hex`.
The run ends with `SIM PASS`.
