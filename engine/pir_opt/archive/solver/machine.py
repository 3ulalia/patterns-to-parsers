"""Derived sequential machine (formulation step 4) and its simulation (step 5).

From the explicit decision diagram ``TDD``:

* ``boundary[t+1]``  (``t = -1..T-1``) = the distinct cofactors crossing the cut after flit ``t`` = the machine's states
  after flit ``t`` (``boundary[0] = [root]``, the reset state).  Moore machine: one flit per cycle, output =
  ``end_output(state)`` = F with every remaining byte absent (class 0).
* ``transitions[t]`` = for each state ``s`` of ``boundary[t]`` the list of ``(cube, dst)`` paths through flit ``t``'s
  lane variables; a cube is ``{lane: frozenset(classes)}`` (unconstrained lanes omitted = don't care).
* Logic: a lane class-set ``C`` becomes literals over the valid-gated class indicators ``z(lane, c)``, ``c >= 1``
  (class 0 has no indicator):  ``0 in C`` -> AND of ``~z_c`` for ``c`` not in ``C`` (a single cube);  ``0 not in C`` ->
  OR of ``z_c``, ``c in C`` (one cube each).  Mutual exclusivity of the indicators is thereby exploited by
  construction (no don't-care set is passed to Espresso).
* State encodings: ``binary`` (minimal bits, codes kept stable across boundaries for states that persist) and
  ``onehot`` (one bit per state; a boundary with a single state needs 0 bits in both).
* Each next-state bit and each output-id bit is an independent single-output cover minimized by Espresso
  (``logic.minimize``), no unused-code don't cares.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterator, List, Optional, Sequence, Set, Tuple

from . import logic
from .classes import LaneClasses
from .tdd import TDD, Node

LaneCond = Tuple[int, FrozenSet[int]]            # (lane, set of classes)
PathCube = Tuple[LaneCond, ...]                  # sorted by lane

MAX_PATHS = 40000


class PathBlowup(RuntimeError):
    pass


def zvar(lane: int, cid: int) -> str:
    return f"z{lane}_{cid}"


@dataclass
class Encoding:
    kind: str                                    # "binary" | "onehot"
    codes: List[Dict[int, int]]                  # per boundary index: node id -> code (binary) or one-hot index
    bits: List[int]                              # per boundary index: number of state bits


@dataclass
class Machine:
    tdd: TDD
    boundary: List[List[Node]]                   # index b = t+1, b = 0..T
    transitions: List[Dict[int, List[Tuple[PathCube, Node]]]]   # index t = 0..T-1: src node id -> paths
    end_out: List[Dict[int, int]]                # index b: node id -> output id if the packet ends now
    enc: Dict[str, Encoding] = field(default_factory=dict)

    @property
    def T(self) -> int:
        return self.tdd.T

    def n_states(self) -> List[int]:
        """states after flit t, t = 0..T-1."""
        return [len(b) for b in self.boundary[1:]]


def derive_machine(tdd: TDD) -> Machine:
    T, B = tdd.T, tdd.B
    boundary = [tdd.cut(t) for t in range(-1, T)]
    transitions: List[Dict[int, List[Tuple[PathCube, Node]]]] = []
    for t in range(T):
        per_src: Dict[int, List[Tuple[PathCube, Node]]] = {}
        memo: Dict[Tuple[int, int], List[Tuple[Tuple[LaneCond, ...], Node]]] = {}
        budget = [MAX_PATHS]

        def paths(u: Node, lane: int):
            if lane == B:
                return [((), u)]
            key = (u.id, lane)
            r = memo.get(key)
            if r is not None:
                return r
            if not u.is_leaf and tdd.var_pos[u.var] == (t, lane):
                groups: Dict[int, Tuple[Node, List[int]]] = {}
                for c, ch in enumerate(u.ch):
                    groups.setdefault(ch.id, (ch, []))[1].append(c)
                r = []
                for ch, cls in groups.values():
                    cond = (lane, frozenset(cls))
                    for cube_, dst in paths(ch, lane + 1):
                        r.append(((cond,) + cube_, dst))
                        budget[0] -= 1
                        if budget[0] < 0:
                            raise PathBlowup(f"more than {MAX_PATHS} paths at flit {t}")
            else:
                r = paths(u, lane + 1)
            memo[key] = r
            return r

        for s in boundary[t]:
            per_src[s.id] = paths(s, 0)
        transitions.append(per_src)
    end_out = [{s.id: tdd.end_output(s) for s in b} for b in boundary]
    return Machine(tdd, boundary, transitions, end_out)


# ----------------------------------------------------------------- encodings
def _zero_nodes(m: Machine) -> List[Optional[Node]]:
    """Per boundary: the state with the most incoming paths (it gets the all-zero code / no one-hot bit, so the
    'nothing happened' product-of-negations never has to be written as a cover)."""
    res: List[Optional[Node]] = [None]
    for b in range(1, len(m.boundary)):
        w: Dict[int, int] = {}
        for paths_ in m.transitions[b - 1].values():
            for _, dst in paths_:
                w[dst.id] = w.get(dst.id, 0) + 1
        nodes = m.boundary[b]
        res.append(max(nodes, key=lambda s: (w.get(s.id, 0), -nodes.index(s))) if len(nodes) > 1 else None)
    return res


SEARCH_MAX_STATES = 12
SEARCH_MAX_PATHS = 2500      # skip the zero-state search when the cycles involved have more paths than this


def _binary_codes(nodes: List[Node], zero: Optional[Node], prev: Dict[int, int]) -> Dict[int, int]:
    nb = (len(nodes) - 1).bit_length()
    cap = 1 << nb
    cur: Dict[int, int] = {}
    used: Set[int] = set()
    if zero is not None:
        cur[zero.id] = 0
        used.add(0)
    for s in nodes:                                   # persistent states keep their code when it fits
        c = prev.get(s.id)
        if s.id not in cur and c is not None and c < cap and c not in used:
            cur[s.id] = c
            used.add(c)
    free = (c for c in range(cap) if c not in used)
    for s in nodes:
        if s.id not in cur:
            cur[s.id] = next(free)
    return cur


def _onehot0_codes(nodes: List[Node], zero: Optional[Node]) -> Dict[int, int]:
    cur, i = {}, 0
    for s in nodes:
        if len(nodes) > 1 and zero is not None and s.id == zero.id:
            cur[s.id] = -1
        else:
            cur[s.id] = i
            i += 1
    if len(nodes) <= 1:
        cur = {s.id: -1 for s in nodes}
    return cur


def assign_encodings(m: Machine, search: bool = True) -> None:
    """Fill ``m.enc`` with binary / onehot / onehot0.  For binary and onehot0 the 'zero state' (all-zero code / no
    bit) of each boundary is chosen greedily left to right: the candidate minimizing Espresso literals of the
    incoming next-state logic + output logic (``search``), else the state with the most incoming paths."""
    nb_ = len(m.boundary)
    n = [len(b) for b in m.boundary]
    default_zero = _zero_nodes(m)
    codes_o = [{s.id: i for i, s in enumerate(nodes)} for nodes in m.boundary]
    m.enc["onehot"] = Encoding("onehot", codes_o, [k if k > 1 else 0 for k in n])
    for kind in ("binary", "onehot0"):
        enc = Encoding(kind, [dict() for _ in range(nb_)],
                       [(k - 1).bit_length() if kind == "binary" else (k - 1 if k > 1 else 0) for k in n])
        m.enc[kind] = enc
        prev: Dict[int, int] = {}
        for b, nodes in enumerate(m.boundary):
            cands = [default_zero[b]]
            if search and 1 < len(nodes) <= SEARCH_MAX_STATES and b > 0:
                npaths = sum(len(v) for v in m.transitions[b - 1].values())
                if b < len(m.transitions):
                    npaths += sum(len(v) for v in m.transitions[b].values())
                if npaths <= SEARCH_MAX_PATHS:
                    cands = list(nodes)
            best = None
            for z in cands:
                enc.codes[b] = (_binary_codes(nodes, z, prev) if kind == "binary" else _onehot0_codes(nodes, z))
                if len(cands) == 1:
                    best = (0, z)
                    break
                cost = sum(logic.minimize(c).literals for c in ns_covers(m, kind, b - 1))
                cost += sum(logic.minimize(c).literals for c in out_covers(m, kind, b))
                if b + 1 < nb_:                       # one-step lookahead: outgoing cycle with the default zero state
                    nxt = m.boundary[b + 1]
                    enc.codes[b + 1] = (_binary_codes(nxt, default_zero[b + 1], enc.codes[b]) if kind == "binary"
                                        else _onehot0_codes(nxt, default_zero[b + 1]))
                    cost += sum(logic.minimize(c).literals for c in ns_covers(m, kind, b))
                if best is None or cost < best[0]:
                    best = (cost, z)
            z = best[1]
            enc.codes[b] = (_binary_codes(nodes, z, prev) if kind == "binary" else _onehot0_codes(nodes, z))
            if kind == "binary":
                prev = enc.codes[b]


def state_var(kind: str, i: int) -> str:
    return f"q{i}" if kind == "binary" else f"h{i}"


def state_lits(enc: Encoding, b: int, node: Node) -> List[Tuple[str, bool]]:
    code = enc.codes[b][node.id]
    if enc.kind == "binary":
        return [(state_var("binary", i), bool((code >> i) & 1)) for i in range(enc.bits[b])]
    if enc.bits[b] == 0:
        return []
    if enc.kind == "onehot":
        return [(state_var("onehot", code), True)]
    if code >= 0:
        return [(state_var("onehot0", code), True)]
    return [(state_var("onehot0", i), False) for i in range(enc.bits[b])]


def lane_alternatives(lc: LaneClasses, cond: FrozenSet[int]) -> List[List[Tuple[str, bool]]]:
    """Literal alternatives (OR of cubes) for 'class of this lane in cond'; [[ ]] = don't care."""
    knr = set(range(1, lc.n))
    if 0 in cond:
        missing = sorted(knr - cond)
        return [[(zvar(lc.lane, c), False) for c in missing]]
    return [[(zvar(lc.lane, c), True)] for c in sorted(cond)]


def ns_covers(m: Machine, kind: str, t: int) -> List[logic.Cover]:
    """Next-state cover per state bit of boundary t+1 (cycle t)."""
    enc = m.enc[kind]
    nbits = enc.bits[t + 1]
    covers: List[logic.Cover] = [[] for _ in range(nbits)]
    lanes = m.tdd.lanes
    for s in m.boundary[t]:
        slits = state_lits(enc, t, s)
        for pc, dst in m.transitions[t][s.id]:
            code = enc.codes[t + 1][dst.id]
            if kind == "binary":
                targets = [j for j in range(nbits) if (code >> j) & 1]
            else:
                targets = [code] if nbits and code >= 0 else []
            if not targets:
                continue
            alts = [lane_alternatives(lanes[l], cls) for l, cls in pc]
            for combo in itertools.product(*alts):
                lits = list(slits)
                for a in combo:
                    lits.extend(a)
                c = frozenset(lits)
                if logic.contradictory(c):
                    continue
                for j in targets:
                    covers[j].append(c)
    return covers


def out_covers(m: Machine, kind: str, b: int) -> List[logic.Cover]:
    """Output-id bit covers as functions of the state bits at boundary index b."""
    enc = m.enc[kind]
    idw = m.tdd.id_width
    covers: List[logic.Cover] = [[] for _ in range(idw)]
    for s in m.boundary[b]:
        pid = m.end_out[b][s.id]
        lits = frozenset(state_lits(enc, b, s))
        for j in range(idw):
            if (pid >> j) & 1:
                covers[j].append(lits)
    return covers


@dataclass
class MachineLogic:
    kind: str
    ns: List[List[logic.Minimized]]      # [t][state bit] next-state cycle t
    out: List[List[logic.Minimized]]     # [t][id bit] output logic after flit t
    support: List[Set[str]]              # per t: z vars read by ns logic
    exact: bool


def minimize_machine(m: Machine, kind: str) -> MachineLogic:
    ns, out, sup = [], [], []
    exact = True
    for t in range(m.T):
        row = [logic.minimize(c) for c in ns_covers(m, kind, t)]
        ns.append(row)
        sup.append({v for r in row for v in r.support() if v.startswith("z")})
        exact &= all(r.exact for r in row)
        orow = [logic.minimize(c) for c in out_covers(m, kind, t + 1)]
        out.append(orow)
        exact &= all(r.exact for r in orow)
    return MachineLogic(kind, ns, out, sup, exact)


# ----------------------------------------------------------------- simulation
def packet_classes(m: Machine, packet: bytes) -> List[Dict[int, int]]:
    """Per flit: lane -> class id of every valid byte (absent bytes are class 0)."""
    cfg = m.tdd.ap.config
    B = cfg.bytes_per_flit
    win = bytes(packet[: cfg.max_bytes])
    n_fl = -(-len(win) // B)
    return [{l: m.tdd.lanes[l].class_of[win[t * B + l]] for l in range(B) if t * B + l < len(win)}
            for t in range(n_fl)]


def run_table(m: Machine, packet: bytes) -> int:
    """Explicit machine: follow the decision diagram flit by flit (no minimized logic involved)."""
    tdd = m.tdd
    s = m.boundary[0][0]
    fl = packet_classes(m, packet)
    for t, cl in enumerate(fl):
        cls = {tdd.var_of[(t, l)]: c for l, c in cl.items() if (t, l) in tdd.var_of}
        # advance through the flit's variables only (later flits' vars are untouched)
        u = s
        while not u.is_leaf and tdd.var_pos[u.var][0] == t:
            u = u.ch[cls.get(u.var, 0)]
        s = u
    return m.end_out[len(fl)][s.id]


class LogicSim:
    """Evaluates the Espresso-minimized next-state / output covers (validates cubes + encoding + minimization)."""

    def __init__(self, m: Machine, ml: MachineLogic):
        self.m, self.ml = m, ml
        self.kind = ml.kind
        enc = m.enc[ml.kind]
        self.enc = enc
        B = m.tdd.B
        self.zbit: Dict[Tuple[int, int], int] = {}
        for l in range(B):
            lc = m.tdd.lanes[l]
            for c in range(1, lc.n):
                self.zbit[(l, c)] = len(self.zbit)
        nz = len(self.zbit)
        self.nz = nz
        self.ns_c: List[List[List[Tuple[int, int]]]] = []
        self.out_c: List[List[List[Tuple[int, int]]]] = []
        sidx = {state_var(ml.kind, i): nz + i for i in range(max(enc.bits) + 1)}
        zidx = {zvar(l, c): b for (l, c), b in self.zbit.items()}
        idx = {**zidx, **sidx}
        for t in range(m.T):
            self.ns_c.append([logic.compile_cover(r.cubes, idx) for r in ml.ns[t]])
            self.out_c.append([logic.compile_cover(r.cubes, idx) for r in ml.out[t]])
        # decode tables
        self.dec: List[Dict[int, Node]] = []
        for b, nodes in enumerate(m.boundary):
            d = {}
            for s in nodes:
                code = enc.codes[b][s.id]
                if ml.kind == "binary":
                    key = code
                elif code < 0:
                    key = 0
                else:
                    key = 1 << code if enc.bits[b] else 0
                d[key] = s
            self.dec.append(d)

    @staticmethod
    def _eval(cubes: List[Tuple[int, int]], x: int) -> int:
        for mk, v in cubes:
            if x & mk == v:
                return 1
        return 0

    def run(self, packet: bytes) -> int:
        m = self.m
        fl = packet_classes(m, packet)
        T = len(fl)
        state = 0   # state-bit vector (below shifted by nz)
        for t, cl in enumerate(fl):
            zs = 0
            for l, c in cl.items():
                if c >= 1:
                    zs |= 1 << self.zbit[(l, c)]
            x = zs | (state << self.nz)
            ns = 0
            for j, cubes in enumerate(self.ns_c[t]):
                if self._eval(cubes, x):
                    ns |= 1 << j
            state = ns
            if state not in self.dec[t + 1]:
                return -1                     # illegal state code
        x = state << self.nz
        pid = 0
        if T == 0:
            return m.end_out[0][m.boundary[0][0].id]
        for j, cubes in enumerate(self.out_c[T - 1]):
            if self._eval(cubes, x):
                pid |= 1 << j
        return pid


# ----------------------------------------------------------------- folding detection
def _transition_sig(m: Machine, t: int, s: Node):
    """Renaming-invariant description of the transition function of state ``s`` in cycle ``t``: the (sorted) list of
    symbol sets (as class-set cubes) per destination, destinations ordered by that list; returns (sig, dst order)."""
    groups: Dict[int, List[tuple]] = {}
    for pc, dst in m.transitions[t][s.id]:
        groups.setdefault(dst.id, []).append(tuple((l, tuple(sorted(cls))) for l, cls in pc))
    items = sorted((tuple(sorted(cubes)), d) for d, cubes in groups.items())
    return tuple(c for c, _ in items), [d for _, d in items]


def fold_relation(m: Machine, t1: int, t2: int) -> str:
    """'iso' if g_t1 and g_t2 are identical up to renaming of source and destination states, 'embeds' if g_t1 is a
    restriction of g_t2 (injective renaming), 'embedded' for the reverse, else 'no'."""
    def sigs(t):
        return {s.id: _transition_sig(m, t, s) for s in m.boundary[t]}
    A, B_ = sigs(t1), sigs(t2)

    def search(X, Y):
        xs = list(X)
        phi: Dict[int, int] = {}
        psi: Dict[int, int] = {}
        inv: Dict[int, int] = {}
        used: Set[int] = set()

        def rec(i):
            if i == len(xs):
                return True
            x = xs[i]
            for y in Y:
                if y in used or X[x][0] != Y[y][0]:
                    continue
                add = []
                ok = True
                for a, b in zip(X[x][1], Y[y][1]):
                    if psi.get(a, b) != b or inv.get(b, a) != a:
                        ok = False
                        break
                    if a not in psi:
                        psi[a], inv[b] = b, a
                        add.append((a, b))
                if ok:
                    used.add(y)
                    if rec(i + 1):
                        return True
                    used.discard(y)
                for a, b in add:
                    del psi[a], inv[b]
            return False
        return rec(0)

    if len(A) == len(B_) and search(A, B_):
        dst_a = {d.id for s in m.boundary[t1] for _, d in m.transitions[t1][s.id]}
        dst_b = {d.id for s in m.boundary[t2] for _, d in m.transitions[t2][s.id]}
        if len(dst_a) == len(dst_b):
            return "iso"
    if len(A) <= len(B_) and search(A, B_):
        return "embeds"
    if len(B_) <= len(A) and search(B_, A):
        return "embedded"
    return "no"


def fold_report(m: Machine) -> Dict[Tuple[int, int], str]:
    out = {}
    for t1 in range(m.T):
        for t2 in range(t1 + 1, m.T):
            out[(t1, t2)] = fold_relation(m, t1, t2)
    return out
