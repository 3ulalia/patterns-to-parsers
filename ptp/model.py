"""Cycle-level Python model mirroring the RTL core (without the valid/ready wrapper):
per-column pseudo-IDs from the current flit, latches with flit-index enables (cleared at SOP),
registered lookup with first-row-wins masks, result two cycles after the valid EOP flit.  Form B also latches the flits
of the window and builds the extraction OR."""
from typing import Dict, Sequence

from . import golden
from . import tokens as tk
from .design import Design, has_token


class Model:
    def __init__(self, d: Design, ports: Dict[str, int] = None):
        self.d = d
        self.cfg = d.config
        self.ports = ports or {}
        npf = self.cfg.nibbles_per_flit
        # per column: list of (literal positions, {literal key: seg id}) plus token segments
        self.col_tabs = []
        for c in d.columns:
            shapes = {}
            tok_segs = []
            for g in c.segs:
                if has_token(g):
                    tok_segs.append((g, c.seg_id[g]))
                    continue
                pos = tuple(i for i, ch in enumerate(g) if ch != "x")
                key = 0
                for i in pos:
                    key |= int(g[i], 16) << (4 * i)
                shapes.setdefault(pos, {})[key] = c.seg_id[g]
            self.col_tabs.append(([(pos, tab) for pos, tab in shapes.items()], tok_segs))
        self.npf = npf
        self.pid = [0] * len(d.columns)
        self.t_cur = 0
        self.eop_d = 0
        self.fl = [0] * self.cfg.num_flits
        self.need = [r.index for r in d.rows if d.live[r.index] and d.group_of[r.index] >= 0]
        self.raw_rows = sorted(set(self.need) | {e for i in self.need for e in d.masks[i]})
        self.result = None

    def _nib(self, flit, c, i):
        j = (c.start + i) % self.npf
        return (flit >> (self.cfg.data_width - 4 - 4 * j)) & 0xF

    def column_hits(self, flit: int, ci: int):
        c = self.d.columns[ci]
        lits, toks = self.col_tabs[ci]
        hits = []
        for pos, tab in lits:
            key = 0
            for i in pos:
                key |= self._nib(flit, c, i) << (4 * i)
            if key in tab:
                hits.append(tab[key])
        for g, sid in toks:
            ok = True
            for i, ch in enumerate(g):
                if ch in golden_hex and self._nib(flit, c, i) != int(ch, 16):
                    ok = False
                    break
            if ok:
                for off, t in tk.scan_tokens(g, self.cfg.tokens):
                    v = 0
                    for k in range(t.nibbles):
                        v = (v << 4) | self._nib(flit, c, off + k)
                    if not t.matches(v, self.ports):
                        ok = False
                        break
            if ok:
                hits.append(sid)
        return hits

    def column_pid(self, flit: int, ci: int) -> int:
        """Latched value: binary pseudo-ID (exact column) or hit bit-mask (one-hot column)."""
        hits = self.column_hits(flit, ci)
        if self.d.columns[ci].onehot:
            v = 0
            for h in hits:
                v |= 1 << (h - 1)
            return v
        assert len(hits) <= 1, "column not one-hot (exactness violated)"
        return hits[0] if hits else 0

    def row_hit(self, i: int) -> bool:
        for ci, sid in self.d.row_sig[self.d.rep[i]]:
            p = self.pid[ci]
            if not ((p >> (sid - 1)) & 1 if self.d.columns[ci].onehot else p == sid):
                return False
        return True

    def lookup(self):
        """Set of selected group indices (row wire & ~(earlier rows' wires), OR per group)."""
        raw = {i: self.row_hit(i) for i in self.raw_rows}
        sel = {self.d.group_of[i] for i in self.need if raw[i] and not any(raw[e] for e in self.d.masks[i])}
        assert len(sel) <= 1, "group selects not one-hot (priority masks broken)"
        return sel

    def value(self, sel):
        """Output value for a selected group set (same encoding as golden.expected)."""
        if self.cfg.form == "A":
            ids = sorted(self.d.groups[g] for g in sel)
            return ids[0] if ids else 0
        DW, T, MW = self.cfg.data_width, self.cfg.num_flits, self.cfg.max_width
        w = 0
        for t in range(T):
            w = (w << DW) | self.fl[t]
        win = w >> (DW * T - MW)
        order = golden.field_order(self.d.rows)
        vals = {n: 0 for n in order}
        for g in sel:
            for n, msb, lsb in self.d.groups[g]:
                vals[n] |= (win >> lsb) & ((1 << (msb - lsb + 1)) - 1)
        return tuple(vals[n] for n in order)

    def step(self, flit: int, valid: int, sop: int, eop: int):
        """One clock edge (accepted-flit strobes).  Returns the result visible after the edge
        (None when no result beat)."""
        res = self.value(self.lookup()) if self.eop_d else None   # registered pids / latches, before the edge
        new_eop_d = int(valid and eop)
        if valid:
            T = self.cfg.num_flits
            t_eff = 0 if sop else self.t_cur
            for ci, c in enumerate(self.d.columns):
                if t_eff == c.t:
                    self.pid[ci] = self.column_pid(flit, ci)
                elif sop:
                    self.pid[ci] = 0
            if t_eff < T:
                self.fl[t_eff] = flit
            self.t_cur = min(t_eff + 1, T)
        self.eop_d = new_eop_d
        return res


golden_hex = "0123456789abcdef"


def evaluate(d: Design, packet: Sequence[int], ports: Dict[str, int] = None):
    m = Model(d, ports)
    n = len(packet)
    res = None
    for i, f in enumerate(packet):
        r = m.step(f, 1, int(i == 0), int(i == n - 1))
        if r is not None:
            res = r
    for _ in range(2):
        r = m.step(0, 0, 0, 0)
        if r is not None:
            res = r
    assert res is not None
    return res
