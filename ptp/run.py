"""FIRST-ROW-WINS parser generator: the PIR row order is the priority; overlaps are legal, never an error.
CLI (form A: parser.sv; form B: parser.sv + extraction.sv + parser_top.sv; plus parser_tb.sv and vec_*.hex):
python3 -m ptp.run <hex> <json> -o <outdir> [--data-width N] [--char-per-col N] [--packets N] [--seed S] [--or-encode] [--sim] [--backend hls|rtl] [--hls-run csim|synth|cosim|impl] [--part part] [--clock ns]"""

import argparse
import os
import shutil
import subprocess
import sys
import time

from . import design, emit, emit_hls, golden, hls_ref, hls_run, model, pir, vectors

VERILATOR = os.environ.get("VERILATOR", "verilator")


def generate(
    hex_path,
    json_path,
    outdir,
    data_width=None,
    packets=2000,
    seed=0,
    char_per_col=None,
    or_encode=False,
    backend="rtl",
    hls_part="xcu280-fsvh2892-2L-e",
    hls_clock=10,
):
    """Generate all files; returns (design, stats dict, nflits).  Cross-checks model vs golden."""
    t0 = time.time()
    outdir = os.path.abspath(outdir)
    rows, cfg = pir.load_pir(hex_path, json_path, data_width, char_per_col)
    d = design.build(rows, cfg)
    ports = vectors.choose_ports(d, seed)
    pkts, dropped = vectors.make_packets(d, packets, seed, ports)
    os.makedirs(outdir, exist_ok=True)
    nflits = sum(len(p) for p in pkts)
    exp = [emit.pack_expected(d, golden.expected(rows, cfg, p, ports)[0]) for p in pkts]
    if backend == "rtl":
        emit.emit_parser(d, os.path.join(outdir, "parser.sv"), or_encode=or_encode)
        if cfg.form == "B":
            emit.emit_extraction(d, os.path.join(outdir, "extraction.sv"))
            emit.emit_parser_top(d, os.path.join(outdir, "parser_top.sv"))
        emit.emit_tb(d, nflits, len(pkts), os.path.join(outdir, "parser_tb.sv"), ports)
    elif backend == "hls":
        tables = emit_hls.hls_tables(d)
        emit_hls.emit_hls_header(d, os.path.join(outdir, "ptp_design.h"))
        hls_run.write_project(d, outdir, or_encode, hls_part, hls_clock)
    else:
        raise ValueError(f"Unknown backend {backend}")
    emit.emit_vector_files(d, pkts, exp, outdir)

    gen_time = time.time() - t0
    bad = 0
    for p in pkts:
        if golden.evaluate(rows, cfg, p, ports) != model.evaluate(d, p, ports):
            bad += 1

    hls_mismatches = 0
    if backend == "hls":
        for p in pkts:
            if golden.evaluate(rows, cfg, p, ports) != hls_ref.evaluate(tables, p):
                hls_mismatches += 1
    st = d.stats()
    st.update(
        DW=cfg.data_width,
        form=cfg.form,
        or_encode=or_encode,
        packets=len(pkts),
        dropped=dropped,
        flits=nflits,
        model_mismatches=bad,
        hls_mismatches=hls_mismatches,
        comatch_packets=vectors.count_comatch(d, pkts, ports),
        gen_time=round(gen_time, 2),
        ports=ports,
    )
    return d, st, nflits


def _sources(outdir):
    """Generated .sv files (DUT first, testbench last)."""
    names = [
        n
        for n in ("parser.sv", "extraction.sv", "parser_top.sv")
        if os.path.exists(os.path.join(outdir, n))
    ]
    return names + ["parser_tb.sv"]


def simulate(outdir):
    """Build + run with Verilator.  Returns (passed, mismatches, build_s, run_s, log)."""
    outdir = os.path.abspath(outdir)
    exe = shutil.which(VERILATOR)
    if not exe:
        return None, 0, 0, 0, "verilator not found"
    obj = os.path.join(outdir, "obj_dir")
    t0 = time.time()
    b = subprocess.run(
        [
            exe,
            "--binary",
            "--timing",
            "--assert",
            "-Wno-fatal",
            "-Wno-lint",
            "-Wno-style",
            "-O1",
            "--top-module",
            "parser_tb",
            "-Mdir",
            obj,
            "-o",
            "sim",
            *_sources(outdir),
        ],
        cwd=outdir,
        capture_output=True,
        text=True,
    )
    bt = time.time() - t0
    if b.returncode != 0:
        return False, -1, bt, 0, b.stdout + b.stderr
    t0 = time.time()
    r = subprocess.run(
        [os.path.join("obj_dir", "sim")], cwd=outdir, capture_output=True, text=True
    )
    rt = time.time() - t0
    log = r.stdout + r.stderr
    mism = log.count("MISMATCH")
    for line in log.splitlines():
        if line.startswith("FAIL:"):
            try:
                mism = int(line.split()[1])
            except ValueError:
                pass
    passed = r.returncode == 0 and "PASS" in log
    return passed, mism, bt, rt, log


def main(argv=None):
    ap_ = argparse.ArgumentParser(
        prog="ptp.run",
        description=__doc__
        + """
PIR forms (one per file): A '<hex> <id>' (id = sized SV literal) ; B '<hex> f1:msb:lsb;f2:msb:lsb' (field extraction,
bit msb = max_width-1 is the first wire bit; a field may appear once per row).  Custom tokens come from the config
'custom' list.  PORT tokens compare against module input ports that are STATIC configuration (hold them stable while a
packet is in flight); their overlap with other segments cannot be checked at generation time and is the user's
responsibility.  FIRST ROW WINS: for a packet the output is that of the first row (top to bottom) that matches; rows
that can overlap an earlier row with another id/signature are masked by it.  Overlaps are never an error (reported as
INFO); a column whose segments cannot be proven exclusive (token overlap, or a port token next to other segments)
is latched ONE-HOT (one bit per segment).  The only hard errors are syntax errors and a token cut by a flit boundary.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap_.add_argument("hex")
    ap_.add_argument("json", nargs="?")
    ap_.add_argument("-o", "--outdir", required=True)
    ap_.add_argument("--data-width", type=int)
    ap_.add_argument("--char-per-col", type=int, dest="char_per_col")
    ap_.add_argument("--packets", type=int, default=2000)
    ap_.add_argument("--seed", type=int, default=0)
    ap_.add_argument(
        "--or-encode",
        action="store_true",
        dest="or_encode",
        help="encode with OR of one-hot wires instead of `unique case (1'b1)` (equivalent: the priority-masked "
        "group selects are one-hot)",
    )
    ap_.add_argument("--sim", action="store_true")
    ap_.add_argument("--backend", choices=["rtl", "hls"], default="rtl")
    ap_.add_argument(
        "--hls-run", choices=["csim", "synth", "cosim", "impl"], default="csim"
    )
    ap_.add_argument("--part", default="xcu280-fsvh2892-2L-e")
    ap_.add_argument("--clock", default="10", type=float)
    a = ap_.parse_args(argv)
    try:
        d, st, _n = generate(
            a.hex,
            a.json,
            a.outdir,
            a.data_width,
            a.packets,
            a.seed,
            a.char_per_col,
            a.or_encode,
            a.backend,
            a.part,
            a.clock,
        )
    except ValueError as e:  # PIR syntax / token-straddles-flit errors
        print(f"ERROR: {e}")
        return 3
    except NotImplementedError as e:
        print(f"HLS ERROR: {e}")
        return 38  # errno 38: function not implemented
    print(
        f"rows={st['rows']} T={st['T']} DW={st['DW']} encoding={'OR' if a.or_encode else 'unique case'}"
    )
    print(f"form {st['form']}: lookup groups = {st['groups']}")
    print(
        f"comparators (j,v)   = {st['comparators']}   token comparators = {st['token_comparators']}"
    )
    ws = st["col_widths"]
    print(
        f"columns             = {st['columns']}  (widths in nibbles: min={min(ws)} max={max(ws)})"
    )
    print("column layout       = " + design.layout_summary(d))
    print(
        f"pseudo-ID bits latched = {st['latched_bits']}   distinct segments = {st['segments']}"
    )
    print(
        f"onehot columns = {st['onehot_columns']}  (latched bits if all were binary: {st['latched_bits_binary']})"
    )
    print(
        f"first-row-wins: overlapping (earlier,later) row pairs = {st['cm_pairs']}  priority mask terms = {st['mask_terms']} "
        f"on {st['masked_rows']} rows  dead rows = {st['dead_rows']}"
    )
    for msg in d.info:
        print(msg)
    hm = st["hls_mismatches"]
    print(
        f"packets={st['packets']} co-match packets={st['comatch_packets']} dropped(undefined)={st['dropped']} flits={st['flits']} golden-vs-model mismatches={st['model_mismatches']} {f'hls_mismatches={hm}' if a.backend == 'hls' else ''} gen={st['gen_time']}s"
    )
    if st["model_mismatches"]:
        print("FAIL: python model disagrees with golden")
        return 2
    if st["hls_mismatches"]:
        print(f"FAIL: hls mismatches = {st['hls_mismatches']}")
        return 2
    if not a.sim:
        return 0
    if a.backend == "rtl":
        ok, mism, bt, rt, log = simulate(a.outdir)
    elif a.backend == "hls":
        ok, mism, rt, log = hls_run.run(a.outdir, a.hls_run)
        bt = None
    if ok is None:
        print("SIM SKIPPED:", log)
        return 0
    print("\n".join(l for l in log.splitlines() if l.startswith(("PASS", "FAIL"))))
    fbt = lambda t: f"{t:.1f}"
    print(
        f"SIM {'PASS' if ok else 'FAIL'} mismatches={mism} build={fbt(bt) if bt is not None else 'n/a'}s run={rt:.1f}s"
    )
    if not ok:
        print(log[-3000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
