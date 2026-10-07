"""SOP covers and Espresso minimization (pyeda), shared by all three machines so the metric is identical.

A *cube* is a frozenset of ``(var_name, polarity)`` literals; a *cover* is a list of cubes (OR of ANDs).
``CONST0`` = ``[]``; ``CONST1`` = a cover containing the empty cube.
``minimize`` runs pyeda's Espresso on one single-output cover (guarded by a cube cap: above it the cover is only
de-duplicated / subsumption-reduced and ``exact`` is False -- reported, never silent).
"""
from __future__ import annotations

import signal
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

Cube = frozenset
Cover = List[Cube]

ESPRESSO_MAX_CUBES = 3000     # above this the input cover is not sent to Espresso
ESPRESSO_MAX_LITS = 120000


class StepTimeout(RuntimeError):
    pass


@contextmanager
def time_limit(seconds: int):
    """Wall-clock guard for pure-Python steps (SIGALRM; no-op where unavailable).  Cannot interrupt the C Espresso
    call itself -- that is bounded by the cube/literal caps above."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def h(signum, frame):
        raise StepTimeout(f"step exceeded {seconds}s")
    old = signal.signal(signal.SIGALRM, h)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def have_pyeda() -> bool:
    try:
        import pyeda.boolalg.minimization  # noqa: F401
        return True
    except Exception:
        return False


def cube(*lits: Tuple[str, bool]) -> Cube:
    return frozenset(lits)


def contradictory(c: Cube) -> bool:
    names = {}
    for v, p in c:
        if names.setdefault(v, p) != p:
            return True
    return False


def reduce_cover(cov: Iterable[Cube]) -> Cover:
    """Drop contradictory / duplicate cubes and cubes subsumed by a smaller cube (single-cube containment)."""
    uniq = sorted({c for c in cov if not contradictory(c)}, key=lambda c: (len(c), sorted(c)))
    out: Cover = []
    for c in uniq:
        if any(o <= c for o in out):
            continue
        out.append(c)
    return out


def cover_or(a: Cover, b: Cover) -> Cover:
    return reduce_cover(list(a) + list(b))


def cover_and(a: Cover, b: Cover) -> Cover:
    if len(a) * len(b) > 300000:
        raise OverflowError("cover_and blowup")
    return reduce_cover(x | y for x in a for y in b)


def cover_not(a: Cover, cap: int = 20000) -> Cover:
    """Complement of a DNF by distribution (bounded)."""
    cur: Cover = [frozenset()]
    for c in a:
        if not c:
            return []
        cur = reduce_cover(x | {(v, not p)} for x in cur for v, p in c)
        if len(cur) > cap:
            raise OverflowError("cover_not blowup")
    return cur


@dataclass
class Minimized:
    cubes: Cover
    exact: bool = True           # False if Espresso was skipped (size cap)

    @property
    def literals(self) -> int:
        return sum(len(c) for c in self.cubes)

    @property
    def terms(self) -> int:
        return len(self.cubes)

    def support(self) -> Set[str]:
        return {v for c in self.cubes for v, _ in c}


def minimize(cov: Cover) -> Minimized:
    cov = reduce_cover(cov)
    if not cov:
        return Minimized([])
    if any(len(c) == 0 for c in cov):
        return Minimized([frozenset()])
    if not have_pyeda():
        return Minimized(cov, False)
    if len(cov) > ESPRESSO_MAX_CUBES or sum(len(c) for c in cov) > ESPRESSO_MAX_LITS:
        return Minimized(cov, False)
    from pyeda.boolalg.expr import And, Or, exprvar
    from pyeda.boolalg.minimization import espresso_exprs
    vs: Dict[str, object] = {}

    def lit(v: str, p: bool):
        x = vs.get(v)
        if x is None:
            x = vs[v] = exprvar(v)
        return x if p else ~x
    e = Or(*[And(*[lit(v, p) for v, p in sorted(c)]) for c in cov])
    if e.is_zero():
        return Minimized([])
    if e.is_one():
        return Minimized([frozenset()])
    (r,) = espresso_exprs(e)
    return Minimized(_expr_to_cover(r))


def _expr_to_cover(e) -> Cover:
    from pyeda.boolalg.expr import AndOp, OrOp
    if e.is_zero():
        return []
    if e.is_one():
        return [frozenset()]

    def to_cube(t) -> Cube:
        if isinstance(t, AndOp):
            return frozenset(_lit(x) for x in t.xs)
        return frozenset([_lit(t)])
    if isinstance(e, OrOp):
        return [to_cube(t) for t in e.xs]
    return [to_cube(e)]


def _lit(x) -> Tuple[str, bool]:
    s = str(x)
    return (s[1:], False) if s.startswith("~") else (s, True)


def eval_cover(cov: Cover, assign: Dict[str, bool]) -> bool:
    return any(all(assign.get(v, False) == p for v, p in c) for c in cov)


def compile_cover(cov: Cover, index: Dict[str, int]) -> List[Tuple[int, int]]:
    """Cubes -> (mask, value) over a bitvector of variables."""
    out = []
    for c in cov:
        m = v = 0
        for name, p in c:
            b = 1 << index[name]
            m |= b
            if p:
                v |= b
        out.append((m, v))
    return out


def chain_cover(terms: Sequence[Tuple[int, Cover]], idw: int, cap: int = 1500) -> Optional[List[Cover]]:
    """Priority chain -> one cover per id bit.  ``terms`` = [(path_id, cover of 'term hits')] in priority order;
    result id = id of the first hitting term, 0 if none.  Adjacent terms with the same id are OR-merged first.
    Returns None if the distribution blows up."""
    groups: List[Tuple[int, Cover]] = []
    for pid, cov in terms:
        if not cov:
            continue
        if groups and groups[-1][0] == pid:
            groups[-1] = (pid, cover_or(groups[-1][1], cov))
        else:
            groups.append((pid, list(cov)))
    bits: List[Cover] = [[] for _ in range(idw)]
    not_prev: Cover = [frozenset()]
    try:
        for pid, cov in groups:
            hit = cover_and(cov, not_prev)
            for b in range(idw):
                if (pid >> b) & 1:
                    bits[b] = cover_or(bits[b], hit)
            if any(len(c) == 0 for c in cov):
                break
            not_prev = cover_and(not_prev, cover_not(cov, cap))
            if len(not_prev) > cap:
                return None
    except (OverflowError, StepTimeout):
        return None
    return bits
