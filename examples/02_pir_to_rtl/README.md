# Example 2: hand-written PIR to RTL

`headers.pir` (form A, `<hex> <id>`) is a 48-byte window (384 bits):

| id | rule |
|----|------|
| 1 / 2 | EtherType IPv4 + protocol TCP / UDP |
| 3 / 4 | EtherType IPv6 + next header TCP / UDP |
| 5 / 6 | any other IPv4 / IPv6 |
| 7 | the ASCII string `hi` at any byte offset 14..46 (one row per offset) |

Rows are ordered: the first matching row wins, so a TCP/IPv4 packet containing "hi" reports id 1.
`headers.json` holds the configuration (`data_width` 64, `max_width` 384).

From the repository root:

```bash
python3 -m ptp.run examples/02_pir_to_rtl/headers.pir examples/02_pir_to_rtl/headers.json \
    -o examples/02_pir_to_rtl/out/rtl --sim

# estimated LUT count (no synthesis needed)
python3 -m ptp_est.estimate examples/02_pir_to_rtl/headers.pir examples/02_pir_to_rtl/headers.json
```

Generated files (git-ignored) are in `out/rtl/`: `parser.sv`, `parser_tb.sv`, `vec_*.hex`. `--sim` needs Verilator.
