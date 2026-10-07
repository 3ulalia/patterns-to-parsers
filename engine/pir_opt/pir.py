"""PIR (Pattern Intermediate Representation) data model and loader.

A ``.hex`` PIR file has one row per line::

    <hex-pattern> <id>                 form A: classify, e.g.  0800xxxxxxxxxxxx 4'h3
    <hex-pattern> <name:start:end>...  form B: field extraction (no id)

* ``x`` / ``X`` nibbles are don't-cares; the pattern is ``max_width/4`` nibbles.
* The pattern reads left to right in wire order: nibbles 0-1 are byte 0 (the
  first byte on the wire), nibbles 2-3 are byte 1, and so on.
* Blank lines and ``#`` / ``//`` comment lines are skipped.
* Form-B rows are parsed and flagged ``is_extraction``; matching ignores them.

Config JSON needs at least ``data_width`` (bits per flit) and ``max_width``
(bits examined per packet). Other keys (num_cols, custom, ...) are ignored here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional

_ID_RE = re.compile(r"^(\d+)'([sS]?)([hHdDbBoO])([0-9a-fA-F_xXzZ]+)$")
_BASES = {"h": 16, "d": 10, "b": 2, "o": 8}
_EXTRACTION_RE = re.compile(r"^[A-Za-z_]\w*:\d+:\d+(;[A-Za-z_]\w*:\d+:\d+)*;?$")


@dataclass(frozen=True)
class PirConfig:
    data_width: int  # bits per flit
    max_width: int   # bits of packet considered

    def __post_init__(self) -> None:
        if self.data_width <= 0 or self.data_width % 8:
            raise ValueError(f"data_width must be a positive multiple of 8, got {self.data_width}")
        if self.max_width <= 0 or self.max_width % 8:
            raise ValueError(f"max_width must be a positive multiple of 8, got {self.max_width}")

    @property
    def bytes_per_flit(self) -> int:  # B
        return self.data_width // 8

    @property
    def max_bytes(self) -> int:
        return self.max_width // 8

    @property
    def num_flits(self) -> int:  # T = ceil(max_width / data_width)
        return -(-self.max_width // self.data_width)

    @property
    def num_nibbles(self) -> int:
        return self.max_width // 4


@dataclass(frozen=True)
class PirRow:
    index: int             # 0-based position among the rows of the file (file order)
    pattern: str           # hex pattern, num_nibbles chars, 'x' = don't care (lower-cased)
    id_width: int          # bits in the SV literal (0 for extraction rows)
    id_value: int          # path id (0 for extraction rows)
    is_extraction: bool = False
    extraction: Optional[str] = None  # raw form-B directive, if any
    line_no: int = 0


def parse_sv_literal(text: str) -> "tuple[int, int]":
    """Parse an SV literal like ``4'h3`` / ``4'd3`` / ``3'b101`` -> (width, value)."""
    m = _ID_RE.match(text)
    if not m:
        raise ValueError(f"not a sized SystemVerilog literal: {text!r}")
    width, _signed, base, digits = m.groups()
    if any(c in "xXzZ" for c in digits):
        raise ValueError(f"x/z in id literal not supported: {text!r}")
    value = int(digits.replace("_", ""), _BASES[base.lower()])
    if value >= (1 << int(width)):
        raise ValueError(f"id value does not fit in width: {text!r}")
    return int(width), value


def load_config(config_path: str) -> PirConfig:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    for key in ("data_width", "max_width"):
        if key not in raw:
            raise ValueError(f"{config_path}: missing required key {key!r}")
    return PirConfig(data_width=int(raw["data_width"]), max_width=int(raw["max_width"]))


def parse_pir_lines(lines: "List[str]", config: PirConfig) -> "List[PirRow]":
    """Parse PIR text lines into rows (see module docstring for the format)."""
    rows: List[PirRow] = []
    for line_no, line in enumerate(lines, start=1):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        parts = s.split()
        if len(parts) < 2:
            raise ValueError(f"line {line_no}: expected '<hex> <id>', got {s!r}")
        pattern = parts[0].lower()
        if len(pattern) != config.num_nibbles:
            raise ValueError(
                f"line {line_no}: pattern has {len(pattern)} nibbles, "
                f"expected {config.num_nibbles} (max_width/4)")
        for ch in pattern:
            if ch not in "0123456789abcdefx":
                raise NotImplementedError(
                    f"line {line_no}: non-hex/non-x token {ch!r} in pattern "
                    "(custom tokens are not supported in this prototype)")
        second = parts[1]
        if "'" in second:
            id_width, id_value = parse_sv_literal(second)
            row = PirRow(len(rows), pattern, id_width, id_value, False, None, line_no)
        elif _EXTRACTION_RE.match(second):
            row = PirRow(len(rows), pattern, 0, 0, True, " ".join(parts[1:]), line_no)
        else:
            raise ValueError(f"line {line_no}: cannot parse id/extraction field {second!r}")
        rows.append(row)
    return rows


def load_pir(hex_path: str, config_path: str) -> "tuple[List[PirRow], PirConfig]":
    """Load ``(rows, config)`` from a ``.hex`` PIR and its config JSON."""
    config = load_config(config_path)
    with open(hex_path, "r", encoding="utf-8") as f:
        rows = parse_pir_lines(f.readlines(), config)
    return rows, config
