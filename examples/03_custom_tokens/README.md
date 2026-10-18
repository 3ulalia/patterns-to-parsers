# Example 3: Snort rules with custom tokens

`rules.txt` holds 5 Snort rules (copy of `snort_frontend/snort.txt`). The Snort frontend turns IP/port
ranges (`RANGE`) and negations (`NE`) into custom tokens: placeholder letters in the PIR pattern that the
generated RTL compares with a range / not-equal comparator. Its config JSON lists the tokens.

From the repository root:

```bash
# 1. Snort rules -> PIR + config (+ msg map); --w is the packet window in bytes
python3 snort_frontend/create_snort_config.py --i examples/03_custom_tokens/rules.txt \
    --o examples/03_custom_tokens/out/snort.pir --w 128

# 2. PIR -> RTL (+ self-checking Verilator simulation)
python3 -m ptp.run examples/03_custom_tokens/out/snort.pir examples/03_custom_tokens/out/snort_config.json \
    --data-width 64 -o examples/03_custom_tokens/out/rtl --sim
```

Written to `out/` (git-ignored): `snort.pir`, `snort_config.json`, `snort_msg_map.txt`, and `rtl/` with
`parser.sv`, `parser_tb.sv`, `vec_*.hex`. Token placeholder letters are random per run. `--sim` needs Verilator.
