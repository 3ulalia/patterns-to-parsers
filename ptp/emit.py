"""Verilog/testbench emitter (plain f.write, no templating).

Files:  form A -> parser.sv (module `parser`, valid/ready, id_out)
        form B -> parser.sv (core: one-hot select + done strobe), extraction.sv, parser_top.sv

Semantics: FIRST ROW WINS -- the PIR row order is the priority; overlapping rows are legal.

Core structure (old PTP, parallel encoders):
  shared nibble + token comparators -> per-column segment ANDs -> per-column pseudo-ID
  (exact column: binary, unique case or OR; non-exact column: ONE-HOT, one bit per segment)
  -> latch while the column's flit is on the bus (cleared at SOP)
  -> row = AND of (pid == segment id | pid[k]) -> eff = row & ~(OR of earlier rows with another
  id/signature that can co-match it) -> group wire = OR of eff (group = id / extraction signature;
  at most one group wire is high).

Valid/ready (both forms): see _handshake().
"""
import os
from typing import Dict, List, Sequence

from . import tokens as tk
from .design import Design, has_token
from .golden import field_order


def idw_of(d: Design) -> int:
    return max([r.id_width for r in d.rows] + [1])


def out_width(d: Design) -> int:
    """Width of the packed expected value / output bus used by the testbench."""
    if d.form == "A":
        return idw_of(d)
    return sum(d.config.field_widths[n] for n in field_order(d.rows))


def pack_expected(d: Design, value) -> int:
    if d.form == "A":
        return value
    off, acc = 0, 0
    for n, v in zip(field_order(d.rows), value):
        acc |= v << off
        off += d.config.field_widths[n]
    return acc


def _bits(n: int) -> int:
    return max(1, n.bit_length())


def _or_vec(width, items):
    """items: [(id_value, wire)] -> '{b_{w-1}, ..., b_0}' where bit b = OR of wires whose id has bit b set."""
    bits = []
    for b in range(width - 1, -1, -1):
        ws = [wire for k, wire in items if (k >> b) & 1]
        # wrap long OR chains: Verilator rejects lines with > 40000 preprocessor tokens
        bits.append(" |\n\t\t".join(" | ".join(ws[i:i + 16]) for i in range(0, len(ws), 16)) if ws else "1'b0")
    return "{" + ", ".join(bits) + "}"


def _or_chain(ws):
    return " |\n\t".join(" | ".join(ws[i:i + 16]) for i in range(0, len(ws), 16))


def port_decls(d: Design):
    """Static configuration inputs of the port tokens: [(name, width)]."""
    return sorted(tk.port_widths(d.config.tokens).items()) if any(
        t.is_port for t in d.config.tokens.values()) else []


def _used_ports(d: Design):
    used = {}
    for _j, t in d.tok_comps:
        for n in t.port_names():
            used[n] = t.bits
    return sorted(used.items())


def _tok_expr(t: tk.Token, hi: int, lo: int) -> str:
    sl, B = f"data_in[{hi}:{lo}]", t.bits
    if t.is_port:
        p = t.port_names()
        if t.opcode == "RANGE":
            return f"({sl} >= {p[0]}) && ({sl} < {p[1]})"
        if t.opcode == "NE":
            return f"({sl} != {p[0]})"
        return " || ".join(f"({sl} == {n})" for n in p)
    v = t.values
    if t.opcode == "RANGE":
        return f"({sl} >= {B}'h{v[0]:X}) && ({sl} < {B}'h{v[1]:X})"
    if t.opcode == "NE":
        return f"({sl} != {B}'h{v[0]:X})"
    return " || ".join(f"({sl} == {B}'h{x:X})" for x in v)


# ------------------------------------------------------------------ core
def _handshake(w, d: Design, eop_sig: str, extra_out_reset: str = "") -> None:
    """Valid/ready protocol (shared by form A parser and form B parser_top).

    * `mid`  : a packet has been accepted (SOP) and its EOP has not been accepted yet.
    * A new packet is accepted (data_in_ready=1 at its SOP) only when no packet is in flight
      after EOP (eop_d == 0: the result is still in the lookup pipeline) and the output register
      is empty or being consumed this cycle (out_valid==0 || out_ready).  Nothing else can fill
      the output register, so the result of an accepted packet always finds it free.
    * Once accepted, data_in_ready stays 1 (mid) until the EOP flit is accepted.
    * The result is written to the output register on eop_d (2 cycles after the EOP flit) and
      held with out_valid until out_ready.
    * A valid flit without SOP while idle is ignored (consumed, not part of any packet).
    """
    w("// ---- valid/ready handshake\n")
    w("reg mid;\n")
    w("wire out_free = ~out_valid | out_ready;\n")
    w(f"assign data_in_ready = mid | (~{eop_sig} & out_free);\n")
    w("wire acc_valid = data_in_valid & data_in_ready & (mid | data_in_sop);\n")
    w("always @(posedge clk) begin\n\tif (rst) mid <= 1'b0;\n\telse if (acc_valid) mid <= ~data_in_eop;\nend\n\n")


def _term(d: Design, ci: int, sid: int) -> str:
    c = d.columns[ci]
    if c.onehot:
        return f"pid_{ci}[{sid - 1}]"
    return f"(pid_{ci} == {c.width}'d{sid})"


def _core(w, d: Design, or_encode: bool) -> None:
    """Comparators ... group wires grp_<g>.  Needs acc_valid, acc_sop, acc_eop, data_in, clk, rst."""
    cfg = d.config
    DW, T, npf = cfg.data_width, cfg.num_flits, cfg.nibbles_per_flit
    TW = _bits(T)
    w("// ---- comparators: one per distinct (nibble position j, value v), shared by all flits\n")
    for j, v in d.comparators:
        hi = DW - 1 - 4 * j
        w(f"wire c_j{j}_v{v} = (data_in[{hi}:{hi-3}] == 4'h{v:X});\n")
    tokname = {}
    if d.tok_comps:
        w("// ---- custom-token comparators (RANGE: lo <= v < hi; NE; EQOR), shared by all flits\n")
    for k, (j, t) in enumerate(d.tok_comps):
        hi = DW - 1 - 4 * j
        lo = hi - t.bits + 1
        tokname[(j, t.key())] = f"tk{k}"
        w(f"wire tk{k} = {_tok_expr(t, hi, lo)};  // {t.id} {t.opcode} {'port ' + t.prefix if t.is_port else ''}"
          f"{'' if t.is_port else ' '.join(format(x, 'X') for x in t.values)}\n")
    w("\n")

    w("// ---- flit index within the packet (saturates at T)\n")
    w(f"reg [{TW-1}:0] t_cur;\n")
    w(f"wire [{TW-1}:0] t_eff = acc_sop ? {TW}'d0 : t_cur;\n")
    w(f"wire [{TW-1}:0] t_inc = (t_eff >= {TW}'d{T}) ? {TW}'d{T} : t_eff + {TW}'d1;\n")
    w("always @(posedge clk) begin\n\tif (rst) t_cur <= 0;\n\telse if (acc_valid) t_cur <= t_inc;\nend\n\n")
    w("wire clr = acc_valid & acc_sop;  // new packet: forget old pseudo-IDs\n")
    for t in sorted({c.t for c in d.columns}):
        w(f"wire cap_t{t} = acc_valid & (t_eff == {TW}'d{t});\n")
    w("\n")

    for c in d.columns:
        W = c.width
        w(f"// ---- column {c.index}: nibbles [{c.start},{c.end}) in flit {c.t}, "
          f"{len(c.segs)} segments, {W}-bit {'ONE-HOT (non-exact column: several segments may hit)' if c.onehot else 'pseudo-ID'}\n")
        for k, g in enumerate(c.segs, start=1):
            terms = []
            i = 0
            toks = dict(tk.scan_tokens(g, cfg.tokens)) if has_token(g) else {}
            while i < len(g):
                ch = g[i]
                if i in toks:
                    t = toks[i]
                    terms.append(tokname[((c.start + i) % npf, t.key())])
                    i += t.nibbles
                    continue
                if ch != "x":
                    terms.append(f"c_j{(c.start + i) % npf}_v{int(ch, 16)}")
                i += 1
            w(f"wire s_{c.index}_{k} = {' & '.join(terms)};  // {g}\n")
        if c.onehot:
            w(f"wire [{W-1}:0] pn_{c.index} = {{{', '.join(f's_{c.index}_{k}' for k in range(len(c.segs), 0, -1))}}};\n")
        else:
            w(f"reg [{W-1}:0] pn_{c.index};\n")
        if c.onehot:
            pass
        elif or_encode:
            vec = _or_vec(W, [(k, f"s_{c.index}_{k}") for k in range(1, len(c.segs) + 1)])
            w(f"always @(*) begin\n\tpn_{c.index} = {vec};\nend\n")
        else:
            w("always @(*) begin\n\tunique case (1'b1)\n")
            for k in range(1, len(c.segs) + 1):
                w(f"\t\ts_{c.index}_{k}: pn_{c.index} = {W}'d{k};\n")
            w(f"\t\tdefault: pn_{c.index} = {W}'d0;\n\tendcase\nend\n")
        # Not a state machine: stops Yosys fsm_detect from re-encoding pid regs (costs ~2x LUTs).
        w(f"(* fsm_encoding = \"none\" *) reg [{W-1}:0] pid_{c.index};\n")
        # FDRE-friendly: sync clear has priority over clock enable; no async/global reset.
        if c.t == 0:
            w(f"always @(posedge clk) if (cap_t0) pid_{c.index} <= pn_{c.index};\n\n")
        else:
            w("always @(posedge clk) begin\n")
            w(f"\tif (clr) pid_{c.index} <= 0;\n")
            w(f"\telse if (cap_t{c.t}) pid_{c.index} <= pn_{c.index};\nend\n\n")

    w("// ---- lookup: rows -> groups (registered pseudo-IDs only).  FIRST ROW WINS: a row is masked by every\n"
      "// earlier row with another id/signature that can co-match it (provably disjoint rows need no mask).\n")
    by_grp: Dict[int, List[int]] = {}
    need = [r.index for r in d.rows if d.live[r.index] and d.group_of[r.index] >= 0]
    raw = sorted(set(need) | {e for i in need for e in d.masks[i]})
    for i in raw:
        sig = d.row_sig[i]
        if sig:
            expr = " & ".join(_term(d, ci, sid) for ci, sid in sig)
        else:
            expr = "1'b1"
        w(f"wire row_{i} = {expr};\n")
    w("\n")
    for i in need:
        if d.masks[i]:
            w(f"wire eff_{i} = row_{i} & ~({_or_chain([f'row_{e}' for e in d.masks[i]])});  // masked by {len(d.masks[i])} earlier row(s)\n")
        by_grp.setdefault(d.group_of[i], []).append(f"eff_{i}" if d.masks[i] else f"row_{i}")
    w("\n")
    for g in range(len(d.groups)):
        rs = by_grp.get(g, [])
        rhs = _or_chain(rs) if rs else "1'b0"
        w(f"wire grp_{g} = {rhs};\n")
    w("always @(posedge clk) begin\n\tif (rst) eop_d <= 1'b0;\n\telse eop_d <= acc_valid & acc_eop;\nend\n\n")


def _header(w, d: Design, what: str, or_encode: bool) -> None:
    cfg = d.config
    w(f"// Generated by ptp.emit -- do not edit.   ({what})\n")
    w(f"// form={d.form} rows={len(d.rows)} T={cfg.num_flits} DW={cfg.data_width} comparators={len(d.comparators)} "
      f"token_comparators={len(d.tok_comps)} columns={len(d.columns)} latched_bits={sum(c.width for c in d.columns)}\n")
    w("// Semantics: FIRST ROW WINS.  The PIR row order is the priority: the output is that of the first row\n"
      "// (top to bottom) that matches; overlapping rows are legal.  Group selects are one-hot.\n")
    oh = sum(1 for c in d.columns if c.onehot)
    if oh:
        w(f"// {oh} column(s) are non-exact (token overlap) and use a ONE-HOT latch (one bit per segment).\n")
    if or_encode:
        w("// Encoding: exact columns use OR of one-hot wires (no case); the lookup id bit b = OR of the\n"
          "// (priority-masked, hence one-hot) group wires with bit b set.\n")
    ports = _used_ports(d)
    if ports:
        w("// Port-token inputs are STATIC configuration: hold them stable while a packet is in flight.\n")
    w("\n")


def _port_lines(d: Design) -> str:
    return "".join(f"\tinput [{wd-1}:0] {n},\n" for n, wd in _used_ports(d))


def emit_parser(d: Design, path: str, or_encode: bool = False) -> None:
    """Form A: module parser with the full valid/ready interface.  Form B: parser core exposing
    sel (one-hot per extraction signature), done and flit_idx."""
    cfg = d.config
    DW, T = cfg.data_width, cfg.num_flits
    TW = _bits(T)
    IDW = idw_of(d)
    G = len(d.groups)
    with open(path, "w") as f:
        w = f.write
        _header(w, d, "parser" if d.form == "A" else "parser core", or_encode)
        if d.form == "A":
            w("module parser(\n\tinput clk,\n\tinput rst,\n")
            w(_port_lines(d))
            w(f"\tinput [{DW-1}:0] data_in,\n\tinput data_in_valid,\n\toutput data_in_ready,\n")
            w("\tinput data_in_sop,\n\tinput data_in_eop,\n")
            w(f"\toutput reg [{IDW-1}:0] id_out,\n\toutput reg out_valid,\n\tinput out_ready\n);\n\n")
            w("wire acc_sop = data_in_sop;\nwire acc_eop = data_in_eop;\nreg eop_d;\n")
            _handshake(w, d, "eop_d")
        else:
            w("module parser(\n\tinput clk,\n\tinput rst,\n")
            w(_port_lines(d))
            w(f"\tinput [{DW-1}:0] data_in,\n\tinput acc_valid,   // flit accepted by the wrapper (valid & ready)\n")
            w("\tinput acc_sop,\n\tinput acc_eop,\n")
            w(f"\toutput [{max(G,1)-1}:0] sel,     // one-hot-per-signature select, valid in the cycle `done` is high\n")
            w("\toutput done,             // 2 cycles after the accepted EOP flit\n")
            w(f"\toutput [{TW-1}:0] flit_idx  // index of the current flit (0 at SOP)\n);\n\n")
            w("reg eop_d;\n")
        _core(w, d, or_encode)
        if d.form == "A":
            ids = list(range(len(d.groups)))
            w(f"reg [{IDW-1}:0] id_c;\n")
            if ids and or_encode:
                w(f"always @(*) begin\n\tid_c = {_or_vec(IDW, [(d.groups[g], f'grp_{g}') for g in ids])};\nend\n\n")
            elif ids:
                w("always @(*) begin\n\tunique case (1'b1)\n")
                for g in ids:
                    w(f"\t\tgrp_{g}: id_c = {IDW}'d{d.groups[g]};\n")
                w(f"\t\tdefault: id_c = {IDW}'d0;\n\tendcase\nend\n\n")
            else:
                w(f"always @(*) id_c = {IDW}'d0;\n\n")
            w("// ---- output register: written on eop_d (2 cycles after the EOP flit), held until out_ready\n")
            w("always @(posedge clk) begin\n")
            w("\tif (rst) begin\n\t\tout_valid <= 1'b0;\n\t\tid_out <= 0;\n\tend else begin\n")
            w("\t\tif (out_valid & out_ready) out_valid <= 1'b0;\n")
            w("\t\tif (eop_d) begin\n\t\t\tout_valid <= 1'b1;\n\t\t\tid_out <= id_c;\n\t\tend\n")
            w("\tend\nend\n\nendmodule\n")
        else:
            w(f"assign sel = {{{', '.join(f'grp_{g}' for g in range(G - 1, -1, -1))}}};\n" if G else
              "assign sel = 1'b0;\n")
            w("assign done = eop_d;\nassign flit_idx = t_eff;\n\nendmodule\n")


def emit_extraction(d: Design, path: str) -> None:
    cfg = d.config
    MW = cfg.max_width
    G = len(d.groups)
    order = field_order(d.rows)
    with open(path, "w") as f:
        w = f.write
        w("// Generated by ptp.emit -- do not edit.\n")
        w("// field_out = OR over select bits g of ({W{sel[g]}} & zero_extended(win[msb:lsb])).\n")
        w("// sel is one-hot (first-row-wins priority is resolved in parser.sv), so no priority is needed here.\n")
        w("// win bit MW-1 is the first bit on the wire.\n")
        w(f"module extraction(\n\tinput [{MW-1}:0] win,\n\tinput [{max(G,1)-1}:0] sel")
        for n in order:
            w(f",\n\toutput [{cfg.field_widths[n]-1}:0] {n}_out")
        w("\n);\n\n")
        for n in order:
            W = cfg.field_widths[n]
            terms = []
            for g, sig in enumerate(d.groups):
                for fn, msb, lsb in sig:
                    if fn == n:
                        wd = msb - lsb + 1
                        sl = f"win[{msb}:{lsb}]"
                        val = sl if wd == W else f"{{{W-wd}'b0, {sl}}}"
                        terms.append(f"({{{W}{{sel[{g}]}}}} & {val})")
            rhs = " |\n\t".join(terms) if terms else f"{W}'d0"
            w(f"assign {n}_out = {rhs};\n")
        w("\nendmodule\n")


def emit_parser_top(d: Design, path: str) -> None:
    cfg = d.config
    DW, T, MW = cfg.data_width, cfg.num_flits, cfg.max_width
    TW = _bits(T)
    G = len(d.groups)
    order = field_order(d.rows)
    # flits that hold any extracted bit
    need = set()
    for sig in d.groups:
        for _n, msb, lsb in sig:
            need.update(range((MW - 1 - msb) // DW, (MW - 1 - lsb) // DW + 1))
    with open(path, "w") as f:
        w = f.write
        w("// Generated by ptp.emit -- do not edit.\n")
        w("// parser_top: valid/ready owner + flit buffer + parser core + extraction.\n")
        if _used_ports(d):
            w("// Port-token inputs are STATIC configuration: hold them stable while a packet is in flight.\n")
        w("module parser_top(\n\tinput clk,\n\tinput rst,\n")
        w(_port_lines(d))
        w(f"\tinput [{DW-1}:0] data_in,\n\tinput data_in_valid,\n\toutput data_in_ready,\n")
        w("\tinput data_in_sop,\n\tinput data_in_eop,\n")
        for n in order:
            w(f"\toutput reg [{cfg.field_widths[n]-1}:0] {n}_out,\n")
        w("\toutput reg out_valid,\n\tinput out_ready\n);\n\n")
        w("wire done;\nwire acc_valid;\n")
        w(f"wire [{max(G,1)-1}:0] sel;\nwire [{TW-1}:0] flit_idx;\n")
        w("reg mid;\nwire out_free = ~out_valid | out_ready;\n")
        w("assign data_in_ready = mid | (~done & out_free);\n")
        w("assign acc_valid = data_in_valid & data_in_ready & (mid | data_in_sop);\n")
        w("always @(posedge clk) begin\n\tif (rst) mid <= 1'b0;\n\telse if (acc_valid) mid <= ~data_in_eop;\nend\n\n")
        w("parser u_parser(\n\t.clk(clk), .rst(rst),\n")
        for n, _wd in _used_ports(d):
            w(f"\t.{n}({n}),\n")
        w("\t.data_in(data_in), .acc_valid(acc_valid), .acc_sop(data_in_sop), .acc_eop(data_in_eop),\n")
        w("\t.sel(sel), .done(done), .flit_idx(flit_idx)\n);\n\n")
        w("// ---- flit buffer: every accepted flit of the window is latched (flits beyond the window ignored)\n")
        for t in range(T):
            if t in need:
                w(f"reg [{DW-1}:0] fl_{t};\n")
                w(f"always @(posedge clk) if (acc_valid && flit_idx == {TW}'d{t}) fl_{t} <= data_in;\n")
            else:
                w(f"wire [{DW-1}:0] fl_{t} = {DW}'d0;  // no extracted bit in this flit\n")
        w(f"wire [{DW*T-1}:0] accum = {{{', '.join(f'fl_{t}' for t in range(T))}}};\n")
        w(f"wire [{MW-1}:0] win = accum[{DW*T-1}:{DW*T-MW}];\n\n")
        for n in order:
            w(f"wire [{cfg.field_widths[n]-1}:0] {n}_c;\n")
        w("extraction u_ext(.win(win), .sel(sel)")
        for n in order:
            w(f", .{n}_out({n}_c)")
        w(");\n\n")
        w("always @(posedge clk) begin\n\tif (rst) out_valid <= 1'b0;\n\telse begin\n")
        w("\t\tif (out_valid & out_ready) out_valid <= 1'b0;\n\t\tif (done) out_valid <= 1'b1;\n\tend\nend\n")
        w("always @(posedge clk) begin\n\tif (done) begin\n")
        for n in order:
            w(f"\t\t{n}_out <= {n}_c;\n")
        w("\tend\nend\n\nendmodule\n")


# ------------------------------------------------------------------ vectors / testbench
def emit_vector_files(d: Design, packets, expected_vals, outdir: str) -> None:
    """packets: list of flit lists; expected_vals: packed expected output per packet."""
    DW = d.config.data_width
    EW = out_width(d)
    with open(os.path.join(outdir, "vec_flits.hex"), "w") as ff, \
         open(os.path.join(outdir, "vec_ctrl.hex"), "w") as fc, \
         open(os.path.join(outdir, "vec_exp.hex"), "w") as fe:
        for pkt, exp in zip(packets, expected_vals):
            for i, flit in enumerate(pkt):
                ff.write(f"{flit:0{(DW + 3) // 4}x}\n")
                fc.write(f"{(int(i == 0) << 1) | int(i == len(pkt) - 1):x}\n")
            fe.write(f"{exp:0{(EW + 3) // 4}x}\n")


def dut_name(d: Design) -> str:
    return "parser" if d.form == "A" else "parser_top"


def emit_tb(d: Design, nflits: int, npkts: int, path: str, ports: Dict[str, int] = None) -> None:
    """Scoreboard testbench: random input valid gaps, random out_ready backpressure (phases),
    in-order check of every output beat, protocol checks, no lost/duplicated flit or beat."""
    cfg = d.config
    DW = cfg.data_width
    EW = out_width(d)
    ports = ports or {}
    order = field_order(d.rows)
    with open(path, "w") as f:
        w = f.write
        w("`timescale 1ns/1ps\nmodule parser_tb;\n")
        w(f"\tlocalparam NF = {nflits};\n\tlocalparam NP = {npkts};\n\tlocalparam EW = {EW};\n")
        w("\treg clk = 0, rst = 1;\n")
        w(f"\treg [{DW-1}:0] data_in = 0;\n\treg data_in_valid = 0, data_in_sop = 0, data_in_eop = 0, out_ready = 0;\n")
        w("\twire data_in_ready, out_valid;\n\twire [EW-1:0] out_data;\n")
        for n, wd in _used_ports(d):
            w(f"\treg [{wd-1}:0] {n} = {wd}'h{ports[n]:X};  // static port-token value\n")
        if d.form == "B":
            for n in order:
                w(f"\twire [{cfg.field_widths[n]-1}:0] {n}_w;\n")
            w("\tassign out_data = {" + ", ".join(f"{n}_w" for n in reversed(order)) + "};\n")
        w(f"\treg [{DW-1}:0] flits [0:NF-1];\n\treg [1:0] ctrl [0:NF-1];\n\treg [EW-1:0] expv [0:NP-1];\n")
        w("\tinteger pos = 0, outidx = 0, errors = 0, eops = 0, flits_acc = 0, cyc = 0;\n")
        w("\treg presenting = 0, acc_now = 0, inpkt = 0, stall = 0, drain = 0;\n")
        w("\treg [EW-1:0] prev_data = 0;\n\treg [31:0] rnd = 32'h2545F491;\n")
        w("\treg [7:0] vprob = 8'd255, rprob = 8'd255;\n\n")
        w(f"\t{dut_name(d)} dut(.clk(clk), .rst(rst),\n")
        for n, _wd in _used_ports(d):
            w(f"\t\t.{n}({n}),\n")
        w("\t\t.data_in(data_in), .data_in_valid(data_in_valid), .data_in_ready(data_in_ready),\n")
        w("\t\t.data_in_sop(data_in_sop), .data_in_eop(data_in_eop),\n")
        if d.form == "A":
            w("\t\t.id_out(out_data), .out_valid(out_valid), .out_ready(out_ready));\n\n")
        else:
            for n in order:
                w(f"\t\t.{n}_out({n}_w),\n")
            w("\t\t.out_valid(out_valid), .out_ready(out_ready));\n\n")
        w("\talways #5 clk = ~clk;\n\n")
        w("\ttask next_rnd;\n\t\tbegin\n\t\t\trnd = rnd ^ (rnd << 13);\n\t\t\trnd = rnd ^ (rnd >> 17);\n"
          "\t\t\trnd = rnd ^ (rnd << 5);\n\t\tend\n\tendtask\n\n")
        w("\ttask err(input [255:0] code);\n\t\tbegin\n\t\t\terrors = errors + 1;\n"
          "\t\t\tif (errors <= 20) $display(\"ERROR cycle %0d: %0s (out_data=%h outidx=%0d pos=%0d)\", cyc, code, out_data, outidx, pos);\n"
          "\t\tend\n\tendtask\n\n")
        # ---- monitor: samples the values the DUT sees at the clock edge
        w("\talways @(posedge clk) if (!rst) begin\n\t\tcyc = cyc + 1;\n")
        w("\t\tif (inpkt && !data_in_ready) err(\"ready dropped mid-packet\");\n")
        w("\t\tif (data_in_valid && data_in_ready) begin\n\t\t\tacc_now = 1;\n\t\t\tflits_acc = flits_acc + 1;\n")
        w("\t\t\tif (data_in_eop) begin eops = eops + 1; inpkt = 0; end else inpkt = 1;\n\t\tend\n")
        w("\t\tif (stall && (!out_valid || out_data !== prev_data)) err(\"output changed/dropped while stalled\");\n")
        w("\t\tif (out_valid && outidx >= eops) err(\"output beat before its EOP\");\n")
        w("\t\tif (out_valid && out_ready) begin\n")
        w("\t\t\tif (outidx >= NP) err(\"extra output beat\");\n")
        w("\t\t\telse if (out_data !== expv[outidx]) begin\n\t\t\t\terr(\"output mismatch\");\n"
          "\t\t\t\tif (errors <= 20) $display(\"   packet %0d: got %h expected %h\", outidx, out_data, expv[outidx]);\n\t\t\tend\n")
        w("\t\t\toutidx = outidx + 1;\n\t\tend\n")
        w("\t\tstall = out_valid && !out_ready;\n\t\tprev_data = out_data;\n\tend\n\n")
        # ---- driver
        w("\talways @(negedge clk) if (!rst) begin\n")
        w("\t\tif (acc_now) begin presenting = 0; pos = pos + 1; acc_now = 0; end\n")
        w("\t\tnext_rnd;\n")
        w("\t\tif (cyc % 150 == 0) begin\n\t\t\tcase (rnd[9:8])\n\t\t\t\t2'd0: vprob = 8'd255;\n\t\t\t\t2'd1: vprob = 8'd190;\n"
          "\t\t\t\t2'd2: vprob = 8'd80;\n\t\t\t\t2'd3: vprob = 8'd25;\n\t\t\tendcase\n")
        w("\t\t\tcase (rnd[13:12])\n\t\t\t\t2'd0: rprob = 8'd255;\n\t\t\t\t2'd1: rprob = 8'd128;\n"
          "\t\t\t\t2'd2: rprob = 8'd40;\n\t\t\t\t2'd3: rprob = 8'd10;\n\t\t\tendcase\n\t\tend\n")
        w("\t\tif (!presenting) begin\n")
        w("\t\t\tif (pos < NF && (rnd[23:16] <= vprob)) begin\n")
        w("\t\t\t\tdata_in = flits[pos];\n\t\t\t\tdata_in_valid = 1;\n\t\t\t\tdata_in_sop = ctrl[pos][1];\n"
          "\t\t\t\tdata_in_eop = ctrl[pos][0];\n\t\t\t\tpresenting = 1;\n")
        w("\t\t\tend else begin\n\t\t\t\tdata_in_valid = 0;\n")
        w(f"\t\t\t\tdata_in = {{{', '.join(['rnd'] * ((DW + 31) // 32))}}} ^ {{{DW}{{rnd[3]}}}};\n" if DW > 32 else
          "\t\t\t\tdata_in = rnd;\n")
        w("\t\t\t\tdata_in_sop = rnd[5];\n\t\t\t\tdata_in_eop = rnd[6];\n\t\t\tend\n\t\tend\n")
        w("\t\tout_ready = drain | (rnd[31:24] <= rprob);\n\tend\n\n")
        w("\tinitial begin\n")
        w("\t\t$readmemh(\"vec_flits.hex\", flits);\n\t\t$readmemh(\"vec_ctrl.hex\", ctrl);\n\t\t$readmemh(\"vec_exp.hex\", expv);\n")
        w("\t\trepeat (3) @(negedge clk);\n\t\trst = 0;\n")
        w("\t\twhile (outidx < NP && cyc < (NF + NP) * 80 + 20000) @(posedge clk);\n")
        w("\t\tdrain = 1;\n\t\trepeat (30) @(posedge clk);\n")
        w("\t\tif (pos != NF || flits_acc != NF) err(\"flits lost/duplicated\");\n")
        w("\t\tif (outidx != NP) err(\"missing output beats\");\n")
        w("\t\tif (errors == 0) $display(\"PASS (%0d packets, %0d flits, %0d cycles)\", outidx, flits_acc, cyc);\n")
        w("\t\telse $display(\"FAIL: %0d errors (%0d of %0d packets checked)\", errors, outidx, NP);\n")
        w("\t\tif (errors != 0) $fatal(1, \"parser_tb FAIL\");\n\t\t$finish;\n\tend\nendmodule\n")
