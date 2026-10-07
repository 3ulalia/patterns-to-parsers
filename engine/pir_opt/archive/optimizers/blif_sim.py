"""Independent bit-parallel BLIF simulator (parses the emitted BLIF text).

Each net value is a Python int whose bit ``i`` is the value in simulation lane ``i``
(e.g. packet ``i``), so N packets are simulated at once. Supports ``.inputs``,
``.outputs``, ``.names`` (on-set cubes), ``.latch <d> <q> [<init>]``.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple


class BlifModel:
    def __init__(self, text: str) -> None:
        self.inputs: List[str] = []
        self.outputs: List[str] = []
        self.names: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {}
        self.latches: List[Tuple[str, str, int]] = []
        cur = None
        for raw in text.splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            tok = s.split()
            if tok[0] == ".model" or tok[0] == ".end":
                cur = None
            elif tok[0] == ".inputs":
                self.inputs += tok[1:]
            elif tok[0] == ".outputs":
                self.outputs += tok[1:]
            elif tok[0] == ".latch":
                init = int(tok[3]) if len(tok) > 3 and tok[3] in "01" else 0
                self.latches.append((tok[1], tok[2], init))
            elif tok[0] == ".names":
                cur = (tuple(tok[1:-1]), tok[-1], [])
                self.names[tok[-1]] = (cur[0], cur[2])
            else:
                if cur is None:
                    raise ValueError(f"unexpected line {s!r}")
                if len(cur[0]) == 0:
                    cur[2].append("")
                else:
                    cur[2].append(tok[0])
        self.order = self._toposort()

    def _toposort(self) -> List[str]:
        sources = set(self.inputs) | {q for _, q, _ in self.latches}
        order: List[str] = []
        state: Dict[str, int] = {}
        roots = list(self.outputs) + [d for d, _, _ in self.latches]
        for r in roots:
            stack = [(r, 0)]
            while stack:
                n, i = stack.pop()
                if n in sources or (i == 0 and state.get(n) == 2):
                    continue
                if i == 0:
                    if state.get(n) == 1:
                        raise ValueError(f"combinational loop at {n}")
                    state[n] = 1
                    stack.append((n, 1))
                    for x in self.names[n][0]:
                        if x not in sources and state.get(x) != 2:
                            stack.append((x, 0))
                else:
                    state[n] = 2
                    order.append(n)
        return order

    def simulate(self, cycles: Sequence[Dict[str, int]], nlanes: int) -> List[Dict[str, int]]:
        """Run ``cycles`` (dict input -> lane bitmask). Returns per-cycle outputs (pre-clock-edge)."""
        full = (1 << nlanes) - 1
        q = {qq: (full if init else 0) for _, qq, init in self.latches}
        outs: List[Dict[str, int]] = []
        for inp in cycles:
            val: Dict[str, int] = dict(q)
            for k in self.inputs:
                val[k] = inp.get(k, 0) & full
            for n in self.order:
                ins, cubes = self.names[n]
                acc = 0
                for c in cubes:
                    t = full
                    for ch, x in zip(c, ins):
                        if ch == "1":
                            t &= val[x]
                        elif ch == "0":
                            t &= ~val[x] & full
                    acc |= t
                val[n] = acc
            outs.append({o: val[o] for o in self.outputs})
            q = {qq: val[d] for d, qq, _ in self.latches}
        return outs
