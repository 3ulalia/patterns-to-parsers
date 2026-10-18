"""Column layout, per-column segments / pseudo-IDs, comparators, overlap contract.

Port of the old PTP front end (refine_char_per_col, find_constrained_runs, per-column
distinct segments -> pseudo-IDs) with two additions: columns are cut at flit
boundaries, and every column is made EXACT when possible (distinct non-empty segments
pairwise conflicting, so at most one can match any data) -> binary pseudo-ID.  A column that
cannot be made exact (const/port token overlapping another segment, token-vs-token overlap)
is encoded ONE-HOT: one latched bit per segment, several may be set at once.

Semantics: the PIR row order is the priority -- for a packet the output is that of the FIRST
row (top to bottom) that matches.  Rows that can co-match an earlier row with a different
id / extraction signature are masked by that earlier row (see `build`); nothing is ever an
error because of overlaps.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import tokens as tk
from .pir import Config, Row


# ------------------------------------------------------------------ layout
HEXCH = "0123456789abcdef"


def find_constrained_runs(segment: str):
    """Maximal contiguous non-'x' runs as (start, end) (end exclusive); token letters count as constrained."""
    runs, start = [], None
    for i, ch in enumerate(segment):
        if ch != "x":
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(segment)))
    return runs


def has_token(seg: str) -> bool:
    return any(c.isupper() for c in seg)


def _distinct_segments(pats: List[str], s: int, e: int) -> List[str]:
    seen, out = set(), []
    for p in pats:
        seg = p[s:e]
        if seg.strip("x") and seg not in seen:   # skip all-wildcard segments
            seen.add(seg)
            out.append(seg)
    return out


def _cut_at(s: int, e: int, offsets, inside=frozenset()) -> List[Tuple[int, int]]:
    """Cut [s,e) at the given offsets; cuts that fall inside a token (`inside`: global
    boundaries) are dropped, so a token is never cut."""
    pts = sorted({0, e - s} | {o for o in offsets if 0 < o < e - s and (s + o) not in inside})
    return [(s + a, s + b) for a, b in zip(pts, pts[1:])]


def token_inside(rows_or_toks) -> frozenset:
    """Global nibble boundaries b (between nibble b-1 and b) lying strictly inside some token."""
    ins = set()
    for r in rows_or_toks:
        for st, t in r.toks:
            ins.update(range(st + 1, st + t.nibbles))
    return frozenset(ins)


def refine_columns(pats: List[str], cols: List[Tuple[int, int]], inside=frozenset()) -> List[Tuple[int, int]]:
    """Port of refine_char_per_col: if distinct segments of a column have different
    constrained-run shapes, cut at the union of all run boundaries (never inside a token)."""
    out = []
    for s, e in cols:
        segs = _distinct_segments(pats, s, e)
        shapes = {tuple(find_constrained_runs(g)) for g in segs}
        if len(shapes) <= 1:
            out.append((s, e))
            continue
        cuts = set()
        for sh in shapes:
            for a, b in sh:
                cuts.add(a)
                cuts.add(b)
        out.extend(_cut_at(s, e, cuts, inside))
    return out


def cut_at_flits(cols, npf: int):
    out = []
    for s, e in cols:
        cuts = [b - s for b in range(((s // npf) + 1) * npf, e, npf)]
        out.extend(_cut_at(s, e, cuts))
    return out


def column_exact(segs: List[str]) -> bool:
    """True iff the distinct (token-free) segments are pairwise conflicting.  Segments of equal
    shape always conflict (distinct); only cross-shape pairs need a check, done by projecting
    onto the shared constrained positions."""
    by_shape: Dict[Tuple[int, ...], List[str]] = {}
    for g in segs:
        by_shape.setdefault(tuple(i for i, c in enumerate(g) if c != "x"), []).append(g)
    shapes = list(by_shape)
    for a in range(len(shapes)):
        for b in range(a + 1, len(shapes)):
            common = sorted(set(shapes[a]) & set(shapes[b]))
            pa = {tuple(g[i] for i in common) for g in by_shape[shapes[a]]}
            if any(tuple(g[i] for i in common) in pa for g in by_shape[shapes[b]]):
                return False
    return True


# ---- pairwise conflict with tokens ------------------------------------------------------------
def _split(seg: str, tokens):
    lits = {i: int(c, 16) for i, c in enumerate(seg) if c in HEXCH}
    return lits, tk.scan_tokens(seg, tokens) if has_token(seg) else []


def _tok_vs_lits(st, t, lits):
    mask = val = 0
    hit = False
    for k in range(t.nibbles):
        if st + k in lits:
            sh = 4 * (t.nibbles - 1 - k)
            mask |= 0xF << sh
            val |= lits[st + k] << sh
            hit = True
    return hit, mask, val


def pair_conflict(a: str, b: str, tokens) -> bool:
    """True iff PROVABLY no data can match both segments/patterns.  Const tokens are decided
    exactly against literals and identical-span tokens.  Port tokens (runtime values) and
    partially overlapping tokens are never provably conflicting (unless a literal nibble
    outside the token already conflicts)."""
    la, ta = _split(a, tokens)
    lb, tb = _split(b, tokens)
    for i, v in la.items():
        if i in lb and lb[i] != v:
            return True
    for (ts, lo_) in ((ta, lb), (tb, la)):
        for st, t in ts:
            hit, mask, val = _tok_vs_lits(st, t, lo_)
            if hit and not t.is_port and not tk.vset_has_masked(t.vset(), mask, val, t.bits):
                return True
    for st, t in ta:
        for su, u in tb:
            if st + t.nibbles <= su or su + u.nibbles <= st:
                continue
            if (not t.is_port and not u.is_port and (st, t.nibbles) == (su, u.nibbles)
                    and not tk.vset_intersect(t.vset(), u.vset())):
                return True
    return False


def column_exact_tok(segs: List[str], tokens):
    """-> (ok, offending pair or None)."""
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if not pair_conflict(segs[i], segs[j], tokens):
                return False, (segs[i], segs[j])
    return True, None


def _bitpos(cfg: Config, nib: int, width_nib: int = 1) -> str:
    return f"[{cfg.max_width - 1 - 4 * nib}:{cfg.max_width - 4 * (nib + width_nib)}]"


def _describe(seg, s, rows, cfg, tokens):
    r = next(r for r in rows if r.pattern[s:s + len(seg)] == seg)
    ts = [f"{t.id}({t.opcode} {'port ' + t.prefix if t.is_port else [hex(v) for v in t.values]})"
          for off, t in tk.scan_tokens(seg, tokens)]
    return f"line {r.line_no} '{seg}'" + (f" [{', '.join(ts)}]" if ts else "")


def enforce_exact(pats: List[str], cols, tokens=None, inside=frozenset(), inexact=None):
    """Splitting rule: a non-exact column is cut at the union of the constrained-run
    boundaries of its distinct segments (every piece then holds only fully-constrained,
    equal-shape segments, which are pairwise conflicting).  Cuts never go inside a token and
    token edges are cut points.  A token-free piece still non-exact is cut per nibble.  A
    piece that cannot be split further without cutting a token stays as is; it is recorded in
    `inexact` {(s, e): (segment a, segment b)} and gets a ONE-HOT encoding."""
    tokens = tokens or {}
    work, out = list(cols), []
    while work:
        s, e = work.pop(0)
        segs = _distinct_segments(pats, s, e)
        bad = None
        if any(has_token(g) for g in segs):
            ok, bad = column_exact_tok(segs, tokens)
        else:
            ok = column_exact(segs)
        if ok:
            out.append((s, e))
            continue
        cuts = set()
        for g in segs:
            for a, b in find_constrained_runs(g):
                cuts.add(a)
                cuts.add(b)
            if has_token(g):
                for off, t in tk.scan_tokens(g, tokens):
                    cuts.add(off)
                    cuts.add(off + t.nibbles)
        pieces = _cut_at(s, e, cuts, inside)
        if len(pieces) < 2:
            pieces = _cut_at(s, e, range(1, e - s), inside)      # per nibble, outside tokens
        if len(pieces) < 2:
            if bad is None:
                _ok, bad = column_exact_tok(segs, tokens)
            out.append((s, e))
            if inexact is not None:
                inexact[(s, e)] = bad
            continue
        work = pieces + work
    return out


def layout_columns(pats: List[str], cfg: Config, rows=None, inexact=None) -> List[Tuple[int, int]]:
    N, npf = cfg.num_nibbles, cfg.nibbles_per_flit
    tokens = cfg.tokens
    inside = token_inside(rows) if rows else frozenset()
    if cfg.col_widths:
        cols, acc = [], 0
        for w in cfg.col_widths:
            cols.append((acc, acc + w))
            acc += w
    else:
        cols = [(a, min(a + npf, N)) for a in range(0, N, npf)]
    # a configured boundary inside a token is dropped (the neighbouring columns merge outward)
    merged = []
    for c in cols:
        if merged and merged[-1][1] in inside:
            merged[-1] = (merged[-1][0], c[1])
        else:
            merged.append(c)
    cols = refine_columns(pats, merged, inside)
    cols = cut_at_flits(cols, npf)
    cols = enforce_exact(pats, cols, tokens, inside, inexact)
    return cols


# ------------------------------------------------------------------ design
def clog2_plus1(k: int) -> int:
    """pseudo-ID width for k distinct segments: clog2(k+1)."""
    return max(1, k.bit_length())


@dataclass
class Column:
    index: int
    start: int            # first nibble (inclusive)
    end: int              # last nibble (exclusive)
    t: int                # flit carrying this column
    segs: List[str]       # distinct segments; segment k (0-based) has id k+1 (binary) / bit k (one-hot)
    seg_id: Dict[str, int]
    width: int            # latched bits: binary pseudo-ID width, or len(segs) when one-hot
    onehot: bool = False  # several segments may match at once (non-exact column)
    reason: str = ""      # why the column is one-hot

    @property
    def nibbles(self):
        return self.end - self.start


@dataclass
class Design:
    config: Config
    rows: List[Row]
    columns: List[Column]            # only columns that hold at least one segment
    all_cols: List[Tuple[int, int]]  # full layout (incl. all-wildcard columns)
    row_sig: List[Tuple[Tuple[int, int], ...]]   # per row: ((col idx, seg id), ...)
    comparators: List[Tuple[int, int]]           # nibble comparators (j, v)
    tok_comps: List[Tuple[int, "tk.Token"]] = field(default_factory=list)  # shared token comparators (local nibble j, token)
    groups: list = field(default_factory=list)   # lookup group keys: ids (form A) / signatures (form B)
    group_of: List[int] = field(default_factory=list)   # per row: index into groups (-1 = no output / id 0 / dead)
    rep: List[int] = field(default_factory=list)        # per row: first row with the same match (signature)
    live: List[bool] = field(default_factory=list)      # row can ever win (False: identical match to an earlier row)
    masks: List[List[int]] = field(default_factory=list)  # per row: earlier live rows (different id) that mask it
    comatch: object = None                       # static co-match test comatch(i, j) -> bool (False = provably disjoint)
    cm_count: int = 0                            # overlapping (earlier, later) row pairs with different ids / signatures
    cm_pairs: list = field(default_factory=list)  # grouped, capped: (line_win, line_lose, key_win, key_lose, count)
    dead: list = field(default_factory=list)     # rows shadowed by an identical earlier match
    info: list = field(default_factory=list)

    @property
    def form(self):
        return self.config.form

    def stats(self):
        return {
            "rows": len(self.rows), "T": self.config.num_flits, "comparators": len(self.comparators),
            "token_comparators": len(self.tok_comps), "groups": len(self.groups),
            "columns": len(self.columns), "col_widths": [c.nibbles for c in self.columns],
            "latched_bits": sum(c.width for c in self.columns),
            "latched_bits_binary": sum(clog2_plus1(len(c.segs)) for c in self.columns),
            "onehot_columns": sum(c.onehot for c in self.columns),
            "segments": sum(len(c.segs) for c in self.columns), "cm_pairs": self.cm_count,
            "mask_terms": sum(len(m) for m, l in zip(self.masks, self.live) if l),
            "masked_rows": sum(1 for m, l in zip(self.masks, self.live) if l and m),
            "dead_rows": len(self.dead),
        }


def make_comatch(rows: List[Row], cfg: Config):
    """-> comatch(i, j): False iff rows i and j are PROVABLY disjoint (no packet matches both).
    Literal nibbles: mask/value test.  Tokens: const tokens exact set intersection against
    literals / identical-span tokens; port tokens (runtime values) are assumed able to co-match."""
    N = cfg.num_nibbles
    mv = []
    for r in rows:
        mask = val = 0
        for n, ch in enumerate(r.litpat):
            if ch != "x":
                sh = 4 * (N - 1 - n)
                mask |= 0xF << sh
                val |= int(ch, 16) << sh
        mv.append((mask, val))
    toks = cfg.tokens
    pats = [r.pattern for r in rows]
    has_t = [bool(r.toks) for r in rows]

    def comatch(i: int, j: int) -> bool:
        mi, vi = mv[i]
        mj, vj = mv[j]
        if mi & mj & (vi ^ vj):
            return False
        if has_t[i] or has_t[j]:
            return not pair_conflict(pats[i], pats[j], toks)
        return True
    return comatch


def _key_str(r_or_key, form: str) -> str:
    k = r_or_key
    if form == "A":
        return f"id {k}"
    return "{" + ";".join(f"{n}:{m}:{l}" for n, m, l in k) + "}"


def build(rows: List[Row], cfg: Config, check_overlap: bool = True) -> Design:
    """Column layout + encodings + first-row-wins masks.  Never refuses because of overlaps."""
    pats = [r.pattern for r in rows]
    npf = cfg.nibbles_per_flit
    inexact: dict = {}
    all_cols = layout_columns(pats, cfg, rows, inexact)
    columns: List[Column] = []
    info: List[str] = []
    for s, e in all_cols:
        segs = _distinct_segments(pats, s, e)
        if not segs:
            continue
        ci = len(columns)
        oh = (s, e) in inexact and len(segs) > 1
        reason = ""
        if oh:
            a_, b_ = inexact[(s, e)]
            ports = sorted({t.id for g in (a_, b_) for _o, t in tk.scan_tokens(g, cfg.tokens) if t.is_port})
            kind = (f"port token{'s' if len(ports) > 1 else ''} {','.join(ports)} (overlap unknowable at generation "
                    f"time)") if ports else "const token overlap"
            reason = f"{kind}: {_describe(a_, s, rows, cfg, cfg.tokens)} vs {_describe(b_, s, rows, cfg, cfg.tokens)}"
        columns.append(Column(ci, s, e, s // npf, segs, {g: k + 1 for k, g in enumerate(segs)},
                              len(segs) if oh else clog2_plus1(len(segs)), oh, reason))
    row_sig = []
    for p in pats:
        sig = []
        for c in columns:
            g = p[c.start:c.end]
            if g.strip("x"):
                sig.append((c.index, c.seg_id[g]))
        row_sig.append(tuple(sig))
    comps = sorted({(n % npf, int(ch, 16)) for r in rows for n, ch in enumerate(r.litpat) if ch != "x"})
    tcs = {}
    for c in columns:
        for g in c.segs:
            if has_token(g):
                for off, t in tk.scan_tokens(g, cfg.tokens):
                    tcs.setdefault(((c.start + off) % npf, t.key()), (( c.start + off) % npf, t))
    tok_comps = sorted(tcs.values(), key=lambda x: (x[0], str(x[1].key())))

    # ---- first-row-wins: masks
    comatch = make_comatch(rows, cfg)
    n = len(rows)
    first_of_sig: Dict[tuple, int] = {}
    rep, live, dead = [], [], []
    for r, sig in zip(rows, row_sig):
        f = first_of_sig.setdefault(sig, r.index)
        rep.append(f)
        live.append(f == r.index)
        if f != r.index and rows[f].key != r.key:
            dead.append(r.index)             # same match, different id: can never win
    masks: List[List[int]] = [[] for _ in range(n)]
    pairs: Dict[tuple, list] = {}
    cm_count = 0
    live_idx: List[int] = []
    for r in rows:
        if live[r.index]:
            m = masks[r.index]
            for e in live_idx:
                if rows[e].key != r.key and comatch(e, r.index):
                    m.append(e)
                    cm_count += 1
                    pk = (rows[e].key, r.key)
                    if pk in pairs:
                        pairs[pk][2] += 1
                    else:
                        pairs[pk] = [rows[e].line_no, r.line_no, 1]
            live_idx.append(r.index)
    cm_pairs = [(v[0], v[1], k[0], k[1], v[2]) for k, v in pairs.items()]
    cm_pairs.sort(key=lambda x: (x[0], x[1]))

    # ---- lookup groups (live rows with an output only)
    groups, group_of = [], []
    for r in rows:
        k = r.key
        if not live[r.index] or (r.form == "A" and k == 0):
            group_of.append(-1)
            continue
        if k not in groups:
            groups.append(k)
        group_of.append(groups.index(k))
    if cfg.form == "A":
        groups_sorted = sorted(groups)
        remap = {groups.index(k): i for i, k in enumerate(groups_sorted)}
        group_of = [remap[g] if g >= 0 else -1 for g in group_of]
        groups = groups_sorted
    d = Design(cfg, rows, columns, all_cols, row_sig, comps, tok_comps, groups, group_of,
               rep, live, masks, comatch, cm_count, cm_pairs, dead, info)

    # ---- INFO lines
    oh_cols = [c for c in columns if c.onehot]
    if oh_cols:
        extra = sum(c.width - clog2_plus1(len(c.segs)) for c in oh_cols)
        info.append(f"INFO: {len(oh_cols)} column(s) encoded ONE-HOT (several segments can hit at once; "
                    f"+{extra} latched bit(s) vs a binary pseudo-ID):")
        for c in oh_cols[:12]:
            info.append(f"INFO:   column {c.index} nibbles [{c.start},{c.end}) {len(c.segs)} segments, {c.width} bits: {c.reason}")
        if len(oh_cols) > 12:
            info.append(f"INFO:   ... (+{len(oh_cols) - 12} more)")
    if cm_count:
        info.append(f"INFO: {cm_count} (earlier,later) row pair(s) with different "
                    f"{'ids' if cfg.form == 'A' else 'extraction signatures'} can both match; the earlier row wins "
                    f"(first-row-wins priority masks added). Grouped by {'id' if cfg.form == 'A' else 'signature'} pair:")
        for lw, ll, kw, kl, cnt in cm_pairs[:10]:
            info.append(f"INFO:   rows at lines {lw} and {ll} ({_key_str(kw, cfg.form)} / {_key_str(kl, cfg.form)}) "
                        f"can both match; row at line {lw} wins  [{cnt} pair(s)]")
        if len(cm_pairs) > 10:
            info.append(f"INFO:   ... (+{len(cm_pairs) - 10} more id/signature pairs)")
    if dead:
        info.append(f"INFO: {len(dead)} row(s) can never win (identical match to an earlier row with a different "
                    f"{'id' if cfg.form == 'A' else 'signature'}); lines: "
                    + ", ".join(str(rows[i].line_no) for i in dead[:10]) + (" ..." if len(dead) > 10 else ""))
    return d


def layout_summary(d: Design, limit: int = 60) -> str:
    items = [f"[{c.start}:{c.end})f{c.t}/{c.width}b{'h' if c.onehot else ''}" for c in d.columns]
    s = " ".join(items[:limit])
    if len(items) > limit:
        s += f" ... (+{len(items) - limit} more)"
    return s
