"""PIR parsing and config for ptp.

Two row forms (exactly one per file):
  Form A: ``<pattern> <id>``                      id = sized SV literal (``4'd3``)
  Form B: ``<pattern> <f1>:<msb>:<lsb>;<f2>:...``  no id; field extraction

Pattern nibble n (n = 0 is the leftmost char) is the n-th nibble on the wire; ``x`` = don't
care; upper-case letters outside A-F are custom-token placeholders (see tokens.py).
Bit index convention of form B: the packet window has ``max_width`` bits, bit
``max_width-1`` is the FIRST bit on the wire (same as the old p4_to_ir / extractor).
"""
import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import tokens as tk

_ID_RE = re.compile(r"^(\d+)'([sS]?)([hHdDbBoO])([0-9a-fA-F_]+)$")
_BASES = {"h": 16, "d": 10, "b": 2, "o": 8}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED = {"clk", "rst", "data_in", "data_in_valid", "data_in_ready", "data_in_sop", "data_in_eop",
            "out_valid", "out_ready", "id_out"}


@dataclass(frozen=True)
class Config:
    data_width: int  # DW, bits per flit
    max_width: int   # bits of pattern / packet window
    # column request (old-PTP semantics): widths in nibbles; None -> one column per flit
    col_widths: Optional[Tuple[int, ...]] = None
    tokens: dict = field(default_factory=dict, compare=False)        # id -> Token
    form: str = "A"
    field_widths: dict = field(default_factory=dict, compare=False)  # form B: name -> width (max over rows)

    @property
    def num_flits(self) -> int:  # T
        return -(-self.max_width // self.data_width)

    @property
    def num_nibbles(self) -> int:
        return self.max_width // 4

    @property
    def nibbles_per_flit(self) -> int:
        return self.data_width // 4

    def locate(self, n: int):
        """Nibble n -> (flit t, local nibble j)."""
        return (4 * n) // self.data_width, n % self.nibbles_per_flit


@dataclass(frozen=True)
class Row:
    index: int
    pattern: str    # hex lower-case / 'x' / token-id letters (upper-case)
    id_width: int = 0
    id_value: int = 0
    line_no: int = 0
    litpat: str = ""                         # pattern with token nibbles replaced by 'x'
    toks: Tuple[Tuple[int, tk.Token], ...] = ()   # (start nibble, token)
    fields: Tuple[Tuple[str, int, int], ...] = ()  # form B: (name, msb, lsb)
    form: str = "A"

    @property
    def key(self):
        """Lookup group: id (form A) / extraction signature (form B)."""
        return self.id_value if self.form == "A" else tuple(sorted(self.fields))


def parse_sv_literal(text: str):
    m = _ID_RE.match(text)
    if not m:
        raise ValueError(f"not a sized SystemVerilog literal: {text!r}")
    width, _s, base, digits = m.groups()
    value = int(digits.replace("_", ""), _BASES[base.lower()])
    if value >= (1 << int(width)):
        raise ValueError(f"id value does not fit in width: {text!r}")
    return int(width), value


def _normalize_pattern(pat: str, tokens: Dict[str, tk.Token], line_no: int):
    """-> (pattern, litpat, toks).  Hex digits any case, 'x' wildcard, token placeholders."""
    out, lit, toks, i = [], [], [], 0
    while i < len(pat):
        ch = pat[i]
        if ch in "0123456789abcdefABCDEFx":
            out.append(ch.lower())
            lit.append(ch.lower())
            i += 1
        elif ch.isupper() and ch.isalpha():
            t = tk.match_token_at(pat, i, tokens)
            if t is None:
                raise ValueError(f"line {line_no}: unknown custom token / bad pattern character at {pat[i:i+8]!r}")
            toks.append((i, t))
            out.append(t.id)
            lit.append("x" * t.nibbles)
            i += t.nibbles
        else:
            raise ValueError(f"line {line_no}: bad pattern character {ch!r}")
    return "".join(out), "".join(lit), tuple(toks)


def parse_fields(text: str, line_no: int, max_width: Optional[int] = None):
    fields, seen = [], set()
    for item in text.split(";"):
        if not item:
            continue
        p = item.split(":")
        if len(p) != 3 or not _IDENT.match(p[0]):
            raise ValueError(f"line {line_no}: malformed extraction tag {item!r} (expected name:msb:lsb)")
        try:
            msb, lsb = int(p[1]), int(p[2])
        except ValueError:
            raise ValueError(f"line {line_no}: malformed extraction tag {item!r}")
        if not (0 <= lsb <= msb) or (max_width is not None and msb >= max_width):
            raise ValueError(f"line {line_no}: tag {item!r} out of range (need 0 <= lsb <= msb < max_width"
                             f"{'=' + str(max_width) if max_width else ''})")
        if p[0] in seen:
            raise ValueError(f"line {line_no}: field {p[0]!r} appears more than once in the row")
        if p[0] in RESERVED or p[0] + "_out" in RESERVED:
            raise ValueError(f"line {line_no}: field name {p[0]!r} collides with a port name")
        seen.add(p[0])
        fields.append((p[0], msb, lsb))
    if not fields:
        raise ValueError(f"line {line_no}: empty extraction tag list")
    return tuple(fields)


def parse_rows(lines: List[str], tokens: Optional[Dict[str, tk.Token]] = None,
               max_width: Optional[int] = None) -> List[Row]:
    tokens = tokens or {}
    rows: List[Row] = []
    form = None
    for line_no, line in enumerate(lines, start=1):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        parts = s.split()
        if len(parts) == 1:
            raise ValueError(f"line {line_no}: row has neither an id (form A) nor extraction tags (form B): {s!r}")
        if len(parts) > 2:
            raise ValueError(f"line {line_no}: rows carrying both id and extraction tags are not supported; "
                             f"use form A (<hex> <id>) or form B (<hex> <tags>): {s!r}")
        pat, lit, toks = _normalize_pattern(parts[0], tokens, line_no)
        if "'" in parts[1]:
            f = "A"
        elif ":" in parts[1]:
            f = "B"
        else:
            raise ValueError(f"line {line_no}: second column is neither an id literal nor tags: {parts[1]!r}")
        if form is None:
            form = f
        elif form != f:
            raise ValueError(f"line {line_no}: mixed PIR forms (file started as form {form}, this row is form {f}); "
                             f"a file must be all form A or all form B")
        if f == "A":
            w, v = parse_sv_literal(parts[1])
            rows.append(Row(len(rows), pat, w, v, line_no, lit, toks, (), "A"))
        else:
            rows.append(Row(len(rows), pat, 0, 0, line_no, lit, toks, parse_fields(parts[1], line_no, max_width), "B"))
    return rows


def check_tokens(rows: List[Row], cfg: Config) -> None:
    """A token must lie inside a single flit."""
    for r in rows:
        for st, t in r.toks:
            a, b = 4 * st, 4 * (st + t.nibbles)       # bit offsets from the first wire bit
            if a // cfg.data_width != (b - 1) // cfg.data_width:
                m = cfg.max_width
                raise ValueError(
                    f"line {r.line_no}: token {t.id} occupies bits [{m-1-a}:{m-b}] (window bit indices, nibbles "
                    f"{st}..{st+t.nibbles-1}) and straddles the flit boundary at bit "
                    f"{(a // cfg.data_width + 1) * cfg.data_width} (data_width={cfg.data_width}); "
                    f"tokens must not be split across flits")


def load_pir(hex_path: str, json_path: Optional[str] = None, data_width: Optional[int] = None,
             char_per_col: Optional[int] = None):
    """Return (rows, Config).  Missing config keys default as documented in the task."""
    raw = {}
    if json_path:
        with open(json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    tokens = tk.parse_tokens(raw.get("custom"))
    with open(hex_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    first = next((l.split()[0] for l in lines if l.strip() and not l.strip().startswith(("#", "//"))), None)
    if first is None:
        raise ValueError("PIR has no rows")
    max_width = int(raw.get("max_width", 4 * len(first)))
    rows = parse_rows(lines, tokens, max_width)
    dw = int(data_width if data_width else raw.get("data_width", max_width))
    if dw <= 0 or dw % 4 or max_width <= 0 or max_width % 4:
        raise ValueError(f"data_width/max_width must be positive multiples of 4 (got {dw}, {max_width})")
    nnib = max_width // 4
    widths = None
    if char_per_col:  # CLI override: uniform columns over the whole pattern
        widths = [char_per_col] * (-(-nnib // char_per_col))
    elif "char_per_col" in raw:
        cpc = raw["char_per_col"]
        if isinstance(cpc, list):
            widths = [int(x) for x in cpc]
        else:
            widths = [int(cpc)] * int(raw.get("num_cols", -(-nnib // int(cpc))))
    if widths is not None:
        if any(w <= 0 for w in widths):
            raise ValueError("char_per_col entries must be positive")
        tot = sum(widths)
        if tot < nnib:       # uncovered tail becomes one extra column
            widths.append(nnib - tot)
        elif tot > nnib:     # clip to the pattern
            out, acc = [], 0
            for w in widths:
                if acc >= nnib:
                    break
                out.append(min(w, nnib - acc))
                acc += out[-1]
            widths = out
        widths = tuple(widths)
    fw: Dict[str, int] = {}
    for r in rows:
        for n, msb, lsb in r.fields:
            fw[n] = max(fw.get(n, 0), msb - lsb + 1)
    cfg = Config(dw, max_width, widths, tokens, rows[0].form, fw)
    for r in rows:
        if len(r.pattern) != cfg.num_nibbles:
            raise ValueError(f"line {r.line_no}: pattern has {len(r.pattern)} nibbles, expected {cfg.num_nibbles}")
    check_tokens(rows, cfg)
    return rows, cfg
