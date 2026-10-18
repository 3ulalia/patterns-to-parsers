"""Custom tokens (RANGE / NE / EQOR) -- semantics of the old tool.

A token placeholder `id` (upper-case letters outside A-F) occupies len(id) nibbles of the
pattern.  Const tokens carry hex `values`; port tokens compare against runtime input ports.

  RANGE : v >= values[0] and v < values[1]      (upper bound EXCLUSIVE, as in the old RTL)
  NE    : v != values[0]                        (further values ignored)
  EQOR  : v equals any of the values

Port naming (as the old tool): NE -> `<prefix>`; RANGE -> `<prefix>_0`, `<prefix>_1`;
EQOR -> `<prefix>_0 .. <prefix>_{num_ports-1}`.  Port tokens are STATIC configuration inputs:
hold them stable while a packet is in flight.  Their overlap with other segments is unknown
at generation time (user's responsibility; treated as exclusive).
"""
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

OPCODES = ("RANGE", "NE", "EQOR")
_ID_RE = re.compile(r"^[G-Z]+$")          # upper-case letters outside A-F
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Token:
    id: str
    type: str                 # 'const' | 'port'
    opcode: str
    values: Tuple[int, ...] = ()
    prefix: str = ""
    num_ports: int = 0

    @property
    def nibbles(self) -> int:
        return len(self.id)

    @property
    def bits(self) -> int:
        return 4 * len(self.id)

    @property
    def is_port(self) -> bool:
        return self.type == "port"

    def port_names(self) -> List[str]:
        if not self.is_port:
            return []
        if self.opcode == "NE":
            return [self.prefix]
        if self.opcode == "RANGE":
            return [self.prefix + "_0", self.prefix + "_1"]
        return [f"{self.prefix}_{i}" for i in range(self.num_ports)]

    def key(self):
        """Comparator identity (width included); equal keys share one comparator."""
        if self.is_port:
            return (self.bits, self.opcode, "port", self.prefix, self.num_ports)
        return (self.bits, self.opcode, "const", self.values)

    # ---- value semantics -------------------------------------------------------------
    def matches(self, v: int, ports: Dict[str, int] = None) -> bool:
        vals = self.values
        if self.is_port:
            p = [ports[n] for n in self.port_names()]
            vals = tuple(p)
        if self.opcode == "RANGE":
            return vals[0] <= v < vals[1]
        if self.opcode == "NE":
            return v != vals[0]
        return v in vals

    def vset(self):
        """Const value set as ('range', lo, hi) | ('ne', c) | ('eqor', frozenset)."""
        if self.opcode == "RANGE":
            return ("range", self.values[0], self.values[1])
        if self.opcode == "NE":
            return ("ne", self.values[0])
        return ("eqor", frozenset(self.values))


def parse_tokens(raw) -> Dict[str, Token]:
    """Validate the config `custom` list -> {id: Token}."""
    out: Dict[str, Token] = {}
    for e in raw or []:
        if not isinstance(e, dict):
            raise ValueError(f"custom entry must be a dict, got {type(e).__name__}")
        tid = e.get("id")
        if not isinstance(tid, str) or not _ID_RE.match(tid):
            raise ValueError(f"custom token id {tid!r} must be upper-case letters outside A-F")
        if tid in out:
            raise ValueError(f"duplicate custom token id {tid}")
        typ, op = e.get("type"), e.get("opcode")
        if typ not in ("const", "port"):
            raise ValueError(f"token {tid}: type must be 'const' or 'port'")
        if op not in OPCODES:
            raise ValueError(f"token {tid}: opcode must be one of {OPCODES}, got {op!r}")
        bits = 4 * len(tid)
        if typ == "const":
            raw_vals = e.get("values")
            if not isinstance(raw_vals, list) or not raw_vals or not all(isinstance(v, str) for v in raw_vals):
                raise ValueError(f"token {tid}: const token needs a non-empty list of hex string 'values'")
            need = {"RANGE": 2, "NE": 1}.get(op, len(raw_vals))
            if len(raw_vals) < need or any(v == "" for v in raw_vals[:need]):
                raise ValueError(f"token {tid}: opcode {op} needs {need} value(s)")
            vals = []
            for v in raw_vals[:need]:
                try:
                    iv = int(v, 16)
                except ValueError:
                    raise ValueError(f"token {tid}: value {v!r} is not hex")
                if iv >= (1 << bits):
                    raise ValueError(f"token {tid}: value {v} does not fit in {bits} bits")
                vals.append(iv)
            if op == "RANGE" and vals[0] >= vals[1]:
                raise ValueError(f"token {tid}: RANGE [{raw_vals[0]}, {raw_vals[1]}) is empty (lo >= hi)")
            out[tid] = Token(tid, typ, op, tuple(vals))
        else:
            pre = e.get("prefix")
            if not isinstance(pre, str) or not _IDENT.match(pre):
                raise ValueError(f"token {tid}: port token needs an identifier 'prefix'")
            n = 0
            if op == "EQOR":
                n = e.get("num_ports")
                if not isinstance(n, int) or n <= 0:
                    raise ValueError(f"token {tid}: EQOR port token needs positive integer 'num_ports'")
            out[tid] = Token(tid, typ, op, (), pre, n)
    ids = list(out)
    for a in ids:
        for b in ids:
            if a != b and b.startswith(a):
                raise ValueError(f"token id {a} is a prefix of token id {b}: ambiguous placeholders")
    # port names: unique width per name
    widths: Dict[str, int] = {}
    for t in out.values():
        for n in t.port_names():
            if widths.setdefault(n, t.bits) != t.bits:
                raise ValueError(f"port {n} used with different widths")
    return out


def port_widths(tokens) -> Dict[str, int]:
    w: Dict[str, int] = {}
    for t in tokens.values():
        for n in t.port_names():
            w[n] = t.bits
    return w


def match_token_at(text: str, i: int, tokens: Dict[str, Token]):
    for tid, t in tokens.items():
        if text.startswith(tid, i):
            return t
    return None


def scan_tokens(text: str, tokens: Dict[str, Token]) -> List[Tuple[int, Token]]:
    """[(offset, Token)] of the placeholders in `text` (hex/x chars skipped)."""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch.isupper() and ch not in "ABCDEF":
            t = match_token_at(text, i, tokens)
            if t is None:
                raise ValueError(f"unknown token placeholder at offset {i}: {text[i:i+8]!r}")
            out.append((i, t))
            i += t.nibbles
        else:
            i += 1
    return out


# ---- set algebra on constants ---------------------------------------------------------------
def _next_match(lo: int, mask: int, val: int, w: int):
    """Smallest v >= lo (< 2^w) with v & mask == val & mask, or None."""
    val &= mask
    if lo & mask == val:
        return lo
    for p in range(w):
        if not (lo >> p) & 1:
            hi_mask = mask & ~((1 << p) - 1)
            prefix = ((lo >> (p + 1)) << (p + 1)) | (1 << p)
            if prefix & hi_mask == val & hi_mask:
                return prefix | (val & ((1 << p) - 1))
    return None


def vset_has_masked(vs, mask: int, val: int, w: int) -> bool:
    """Is there a v in the set with v & mask == val?"""
    val &= mask
    k = vs[0]
    if k == "range":
        m = _next_match(vs[1], mask, val, w)
        return m is not None and m < vs[2]
    if k == "ne":
        full = (1 << w) - 1
        return (val != vs[1]) if mask == full else True
    return any(v & mask == val for v in vs[1])


def vset_intersect(a, b) -> bool:
    if a[0] > b[0]:
        a, b = b, a            # order: eqor < ne < range
    ka, kb = a[0], b[0]
    if ka == "eqor" and kb == "eqor":
        return bool(a[1] & b[1])
    if ka == "eqor" and kb == "ne":
        return any(v != b[1] for v in a[1])
    if ka == "eqor" and kb == "range":
        return any(b[1] <= v < b[2] for v in a[1])
    if ka == "ne" and kb == "ne":
        return True
    if ka == "ne" and kb == "range":
        n = b[2] - b[1]
        return n > 1 or (n == 1 and b[1] != a[1])
    return max(a[1], b[1]) < min(a[2], b[2])      # range / range
