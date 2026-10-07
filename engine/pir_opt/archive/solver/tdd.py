"""Time-ordered decision diagrams for F(x) = path_id of the first matching row (formulation steps 2-3).

Input variables ``x`` (one multi-valued variable per byte position ``k = t*B + lane`` of the window, only if the
lane has >= 2 classes), ordered by ``t`` then ``lane``.  The value of a variable is the lane class of that byte
(``classes.py``); a byte past the end of the packet has class 0 (all predicates false), which reproduces golden's
byte-granular short-packet rule exactly -- no separate length variable (see classes.py).

Two implementations of the same function:

* ``MDD`` -- pure Python, hash-consed multi-valued decision diagram with multi-terminal leaves (leaf value = path id).
  It is the explicit fallback AND the structure the machine is derived from.
* ``build_bdd`` -- ``dd.autoref`` BDD over binary-encoded class bits; ``F(x, y) = (y == path_id(x))`` with the id
  bits ``y`` at the bottom of the order.  Unused class codes are mapped to class 0.

States at the cut after flit ``t`` = number of distinct cofactors of F crossing the cut between flit ``t`` and
``t+1`` (Myhill-Nerode states of the one-flit-per-cycle Moore machine).  Both implementations count them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ..atoms import AtomizedPir
from .classes import LaneClasses, atom_classes, build_lane_classes


class DDBlowup(RuntimeError):
    pass


# ------------------------------------------------------------------ MDD
class Node:
    __slots__ = ("id", "var", "ch", "val")

    def __init__(self, id_: int, var: int, ch: Optional[tuple], val=None):
        self.id, self.var, self.ch, self.val = id_, var, ch, val

    @property
    def is_leaf(self) -> bool:
        return self.ch is None

    def __repr__(self) -> str:
        return f"<leaf {self.val}>" if self.is_leaf else f"<n{self.id} v{self.var}>"


class MDD:
    INF = 1 << 30

    def __init__(self, max_nodes: int = 3_000_000):
        self._u: Dict[tuple, Node] = {}
        self._leaves: Dict[object, Node] = {}
        self._memo: Dict[tuple, Node] = {}
        self.max_nodes = max_nodes

    def leaf(self, v) -> Node:
        n = self._leaves.get(v)
        if n is None:
            n = self._leaves[v] = Node(len(self._u) + len(self._leaves), self.INF, None, v)
        return n

    def mk(self, var: int, ch: tuple) -> Node:
        if all(c is ch[0] for c in ch):
            return ch[0]
        key = (var, tuple(c.id for c in ch))
        n = self._u.get(key)
        if n is None:
            if len(self._u) > self.max_nodes:
                raise DDBlowup(f"MDD exceeded {self.max_nodes} nodes")
            n = self._u[key] = Node(len(self._u) + len(self._leaves), var, ch)
        return n

    def apply(self, op, a: Node, b: Node, tag) -> Node:
        if a.is_leaf and b.is_leaf:
            return self.leaf(op(a.val, b.val))
        key = (tag, a.id, b.id)
        r = self._memo.get(key)
        if r is not None:
            return r
        v = min(a.var, b.var)
        ca = a.ch if a.var == v else None
        cb = b.ch if b.var == v else None
        n = len(ca if ca is not None else cb)
        r = self.mk(v, tuple(self.apply(op, ca[i] if ca else a, cb[i] if cb else b, tag) for i in range(n)))
        self._memo[key] = r
        return r

    def AND(self, a: Node, b: Node) -> Node:
        return self.apply(lambda x, y: x and y, a, b, "and")

    def ite(self, m: Node, a: Node, b: Node) -> Node:
        if m.is_leaf:
            return a if m.val else b
        if a is b:
            return a
        key = ("ite", m.id, a.id, b.id)
        r = self._memo.get(key)
        if r is not None:
            return r
        v = min(m.var, a.var, b.var)
        n = len(m.ch if m.var == v else (a.ch if a.var == v else b.ch))
        r = self.mk(v, tuple(self.ite(m.ch[i] if m.var == v else m,
                                      a.ch[i] if a.var == v else a,
                                      b.ch[i] if b.var == v else b) for i in range(n)))
        self._memo[key] = r
        return r

    def size(self, root: Node) -> int:
        seen = set()
        stack = [root]
        while stack:
            u = stack.pop()
            if u.id in seen:
                continue
            seen.add(u.id)
            if not u.is_leaf:
                stack.extend(u.ch)
        return len(seen)


@dataclass
class TDD:
    ap: AtomizedPir
    lanes: List[LaneClasses]
    mdd: MDD
    root: Node
    var_pos: List[Tuple[int, int]]          # var index -> (t, lane)
    var_of: Dict[Tuple[int, int], int]      # (t, lane) -> var index
    id_width: int

    @property
    def T(self) -> int:
        return self.ap.config.num_flits

    @property
    def B(self) -> int:
        return self.ap.config.bytes_per_flit

    def flit_of(self, u: Node) -> int:
        return self.T + 1 if u.is_leaf else self.var_pos[u.var][0]

    def lane_of(self, u: Node) -> int:
        return -1 if u.is_leaf else self.var_pos[u.var][1]

    def cut(self, t: int) -> List[Node]:
        """Distinct cofactors crossing the cut after flit ``t`` (``t = -1`` -> just the root). Deterministic order."""
        out: Dict[int, Node] = {}
        seen = set()
        stack = [self.root]
        order: List[Node] = []
        while stack:
            u = stack.pop()
            if u.id in seen:
                continue
            seen.add(u.id)
            if self.flit_of(u) > t:
                out[u.id] = u
            else:
                stack.extend(reversed(u.ch))
        # deterministic: order of first appearance in a DFS (children in class order)
        res: List[Node] = []
        seen2 = set()

        def dfs(u: Node) -> None:
            if u.id in seen2:
                return
            seen2.add(u.id)
            if u.id in out:
                res.append(u)
                return
            for c in u.ch:
                dfs(c)
        dfs(self.root)
        return res

    def eval_node(self, u: Node, classes: Dict[int, int]) -> int:
        """Evaluate a (sub)function; ``classes`` maps var index -> class id (missing -> 0)."""
        while not u.is_leaf:
            u = u.ch[classes.get(u.var, 0)]
        return u.val

    def end_output(self, u: Node) -> int:
        """Output if the packet ends now: every remaining byte absent (class 0)."""
        while not u.is_leaf:
            u = u.ch[0]
        return u.val


def build_tdd(ap: AtomizedPir, id_width: Optional[int] = None, max_nodes: int = 3_000_000) -> TDD:
    cfg = ap.config
    B, M = cfg.bytes_per_flit, cfg.max_bytes
    lanes = build_lane_classes(ap)
    var_pos: List[Tuple[int, int]] = []
    var_of: Dict[Tuple[int, int], int] = {}
    for k in range(M):
        t, l = divmod(k, B)
        if lanes[l].n >= 2:
            var_of[(t, l)] = len(var_pos)
            var_pos.append((t, l))
    mdd = MDD(max_nodes)
    T_, F_ = mdd.leaf(True), mdd.leaf(False)
    default = mdd.leaf(0)
    f = default
    width = id_width or max([1] + [r.path_id.bit_length() for r in ap.rows])
    for row in reversed(ap.rows):               # priority fold: first row wins
        m = T_
        for aid in row.atom_ids:
            a = ap.atoms[aid]
            v = var_of[(a.t, a.lane)]
            lc = lanes[a.lane]
            S = set(atom_classes(lc, a.mask, a.value))
            m = mdd.AND(m, mdd.mk(v, tuple(T_ if c in S else F_ for c in range(lc.n))))
        f = mdd.ite(m, mdd.leaf(row.path_id), f)
    return TDD(ap, lanes, mdd, f, var_pos, var_of, width)


def count_states_explicit(tdd: TDD) -> List[int]:
    """states after flit t, t = 0..T-1 (the last entry = number of distinct final outputs)."""
    return [len(tdd.cut(t)) for t in range(tdd.T)]


# ------------------------------------------------------------------ BDD (dd library)
def have_dd() -> bool:
    try:
        import dd.autoref  # noqa: F401
        return True
    except Exception:
        return False


@dataclass
class BddResult:
    states: List[int]       # per t = 0..T-1
    f_size: int             # nodes of F(x, y) (DAG, complement edges)
    nvars: int
    manager_nodes: int


def build_bdd_states(ap: AtomizedPir, id_width: Optional[int] = None) -> BddResult:
    from dd import autoref as _bdd
    cfg = ap.config
    B, M, T = cfg.bytes_per_flit, cfg.max_bytes, cfg.num_flits
    lanes = build_lane_classes(ap)
    bdd = _bdd.BDD()
    names: List[str] = []
    flit_last_level: Dict[int, int] = {}
    code_bits: Dict[Tuple[int, int], List[str]] = {}
    for k in range(M):
        t, l = divmod(k, B)
        nb = lanes[l].nbits
        if nb:
            bits = [f"x{t}_{l}_{b}" for b in range(nb)]
            code_bits[(t, l)] = bits
            names += bits
            flit_last_level[t] = len(names) - 1
    width = id_width or max([1] + [r.path_id.bit_length() for r in ap.rows])
    ynames = [f"y{b}" for b in range(width)]
    bdd.declare(*(names + ynames))

    def ind(t: int, l: int, c: int):
        lc = lanes[l]
        bits = code_bits[(t, l)]
        def eq(cc):
            r = bdd.true
            for i, nm in enumerate(bits):
                bit = (cc >> (len(bits) - 1 - i)) & 1
                r = r & (bdd.var(nm) if bit else ~bdd.var(nm))
            return r
        if c != 0:
            return eq(c)
        r = bdd.true
        for cc in range(1, lc.n):                # class 0 = everything that is not class 1..n-1 (incl. unused codes)
            r = r & ~eq(cc)
        return r

    def idexpr(pid: int):
        r = bdd.true
        for b in range(width):
            yb = bdd.var(ynames[b])
            r = r & (yb if (pid >> b) & 1 else ~yb)
        return r

    g = idexpr(0)
    for row in reversed(ap.rows):
        m = bdd.true
        for aid in row.atom_ids:
            a = ap.atoms[aid]
            S = atom_classes(lanes[a.lane], a.mask, a.value)
            u = bdd.false
            for c in S:
                u = u | ind(a.t, a.lane, c)
            m = m & u
        g = bdd.ite(m, idexpr(row.path_id), g)

    def cof(u):
        lo, hi = u.low, u.high
        if u.negated:
            lo, hi = ~lo, ~hi
        return lo, hi

    states = []
    for t in range(T):
        cutlevel = max([lv for tt, lv in flit_last_level.items() if tt <= t], default=-1)
        found = set()
        seen = set()
        stack = [g]
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            if u == bdd.true or u == bdd.false or u.level > cutlevel:
                found.add(u)
            else:
                lo, hi = cof(u)
                stack += [lo, hi]
        states.append(len(found))
    dag = set()
    stack = [g]
    while stack:
        u = stack.pop()
        key = (u.node if hasattr(u, "node") else int(u))
        if u in dag:
            continue
        dag.add(u)
        if not (u == bdd.true or u == bdd.false):
            lo, hi = cof(u)
            stack += [lo, hi]
    return BddResult(states, len(dag), len(names) + width, len(bdd))
