"""Tiny gate/latch netlist container + BLIF writer (analysis only, no RTL).

Nodes are single-output SOP tables (``.names``): ``Node(out, ins, cubes)`` where each
cube is a string over ``{'0','1','-'}`` of length ``len(ins)`` (output 1 when any cube
matches). Latches are ``Latch(d, q, init)`` with a single implicit clock.

No structural hashing is done here on purpose: the baseline model really has one
comparator per (t, lane, mask, value), and the point of the ABC experiments is to see what
the optimizers merge themselves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple


@dataclass(frozen=True)
class Node:
    out: str
    ins: Tuple[str, ...]
    cubes: Tuple[str, ...]


@dataclass(frozen=True)
class Latch:
    d: str
    q: str
    init: int = 0


@dataclass
class Netlist:
    name: str
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    nodes: List[Node] = field(default_factory=list)
    latches: List[Latch] = field(default_factory=list)
    _n: int = 0
    _const: Dict[int, str] = field(default_factory=dict)
    meta: Dict[str, object] = field(default_factory=dict)

    # ---- construction helpers (all return a net name)
    def fresh(self, prefix: str = "n") -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def add_input(self, name: str) -> str:
        self.inputs.append(name)
        return name

    def const(self, v: int) -> str:
        if v not in self._const:
            out = f"const{v}"
            self.nodes.append(Node(out, (), ("",) if v else ()))
            self._const[v] = out
        return self._const[v]

    def node(self, ins: Sequence[str], cubes: Sequence[str], out: str = "") -> str:
        out = out or self.fresh()
        self.nodes.append(Node(out, tuple(ins), tuple(cubes)))
        return out

    def AND(self, ins: Sequence[str], out: str = "") -> str:
        ins = list(ins)
        if not ins:
            return self.const(1)
        if len(ins) == 1 and not out:
            return ins[0]
        return self.node(ins, ["1" * len(ins)], out)

    def OR(self, ins: Sequence[str], out: str = "") -> str:
        ins = list(ins)
        if not ins:
            return self.const(0)
        if len(ins) == 1 and not out:
            return ins[0]
        cubes = ["-" * i + "1" + "-" * (len(ins) - i - 1) for i in range(len(ins))]
        return self.node(ins, cubes, out)

    def NOT(self, a: str, out: str = "") -> str:
        return self.node([a], ["0"], out)

    def BUF(self, a: str, out: str) -> str:
        return self.node([a], ["1"], out)

    def cube(self, ins: Sequence[str], pattern: str, out: str = "") -> str:
        """One product term with per-input polarity ``pattern`` ('1' true, '0' complemented)."""
        return self.node(ins, [pattern], out)

    def latch(self, d: str, q: str, init: int = 0) -> str:
        self.latches.append(Latch(d, q, init))
        return q

    def add_output(self, net: str, name: str) -> None:
        self.BUF(net, name)
        self.outputs.append(name)

    # ---- emit
    def to_blif(self) -> str:
        L = [f".model {self.name}", ".inputs " + " ".join(self.inputs),
             ".outputs " + " ".join(self.outputs)]
        for lt in self.latches:
            L.append(f".latch {lt.d} {lt.q} {lt.init}")
        for nd in self.nodes:
            L.append(".names " + " ".join(nd.ins + (nd.out,)))
            for c in nd.cubes:
                L.append((c + " 1") if c else "1")
        L.append(".end")
        return "\n".join(L) + "\n"

    def stats(self) -> Dict[str, int]:
        return {"inputs": len(self.inputs), "outputs": len(self.outputs),
                "nodes": len(self.nodes), "latches": len(self.latches)}
