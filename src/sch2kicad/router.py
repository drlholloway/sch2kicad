"""Orthogonal wire routing on the 50 mil grid.

KiCad joins anything that touches at a wire end or a pin tip, so the rules
here are strict: another net's wire may be crossed only straight through
(no bend, no end, no junction there), pin tips and symbol bodies are walls,
and a wire never runs along another wire. A net that cannot be routed under
those rules falls back to net labels, which are always correct.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field

from .geometry import GRID

Cell = tuple[int, int]
H, V = 1, 2
DIRS = {(1, 0): H, (-1, 0): H, (0, 1): V, (0, -1): V}


def cell_of(p: tuple[float, float]) -> Cell:
    return (int(round(p[0] / GRID)), int(round(p[1] / GRID)))


def mm(c: Cell) -> tuple[float, float]:
    return (round(c[0] * GRID, 4), round(c[1] * GRID, 4))


def on_grid(p: tuple[float, float]) -> bool:
    return all(abs(v / GRID - round(v / GRID)) < 1e-3 for v in p)


def grid_cell(p: tuple[float, float], out: tuple[int, int]) -> Cell:
    """The grid point a pin reaches: its own tip, or for an off-grid pin (some library
    symbols sit 0.254 mm off the 50 mil grid) the next grid point outward, so a stub
    from the tip never runs back over the pin."""
    cell = []
    for v, d in zip(p, out):
        g = v / GRID
        if abs(g - round(g)) < 1e-3:
            cell.append(int(round(g)))
        elif d > 0:
            cell.append(math.ceil(g))
        elif d < 0:
            cell.append(math.floor(g))
        else:
            cell.append(int(round(g)))
    return (cell[0], cell[1])


@dataclass
class Terminal:
    """A place a net must reach: a pin tip (or a flag / port pin)."""
    pos: tuple[float, float]           # exact tip position, mm
    out: tuple[int, int]               # outward direction (sheet coords)
    owner: str                         # e.g. "R1.2"

    @property
    def cell(self) -> Cell:
        return grid_cell(self.pos, self.out)

    @property
    def stub(self):
        """Wire from an off-grid tip to its grid point, or None."""
        g = mm(self.cell)
        return None if on_grid(self.pos) else (self.pos, g)


@dataclass
class NetRoute:
    net: str
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = field(default_factory=list)
    junctions: list[tuple[float, float]] = field(default_factory=list)
    labelled: bool = False
    failed_owners: list[str] = field(default_factory=list)


class Router:
    def __init__(self, bounds: tuple[float, float, float, float]):
        x0, y0, x1, y1 = bounds
        self.bounds = (int(math.floor(x0 / GRID)), int(math.floor(y0 / GRID)),
                       int(math.ceil(x1 / GRID)), int(math.ceil(y1 / GRID)))
        self.hard: set[Cell] = set()                 # bodies, pin lines
        self.box_owner: dict[Cell, set[str]] = {}    # which symbol bodies cover a cell
        self.pin_lines: set[Cell] = set()
        self.pin_net: dict[Cell, str] = {}           # pin tips
        self.occ: dict[Cell, dict[str, set[str]]] = {}   # cell -> net -> {"h","v","n"}
        self.soft: dict[Cell, float] = {}            # cosmetic cost (near other things)
        self.net_cells: dict[str, set[Cell]] = {}

    # ............................................................ obstacles
    def block_box(self, box: tuple[float, float, float, float], inflate: float = 0.6,
                  owner: str = "") -> None:
        x0, y0, x1, y1 = box
        i0, i1 = math.ceil((x0 - inflate) / GRID - 1e-6), math.floor((x1 + inflate) / GRID + 1e-6)
        j0, j1 = math.ceil((y0 - inflate) / GRID - 1e-6), math.floor((y1 + inflate) / GRID + 1e-6)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                self.hard.add((i, j))
                self.box_owner.setdefault((i, j), set()).add(owner)

    def block_pin(self, tip: tuple[float, float], out: tuple[int, int], length: float, net: str | None) -> None:
        """Pin tip belongs to ``net``; the pin's line back to the body is a wall."""
        c = grid_cell(tip, out)
        self.pin_net[c] = net or f"__nc__{c}"
        steps = int(round(length / GRID))
        for k in range(1, max(steps, 1) + 1):
            cell = (c[0] - out[0] * k, c[1] - out[1] * k)
            self.hard.add(cell)
            self.pin_lines.add(cell)
        self.hard.discard(c)

    def open_escape(self, tip: tuple[float, float], out: tuple[int, int], owner: str) -> None:
        """Let a pin leave its own symbol. Graphics that reach past a pin tip (an LED's
        light arrows) put the cells beyond it inside the symbol's box, walling the pin
        in; clear them along the pin's axis until the box ends. Another symbol's body,
        a pin tip or a pin line stops the walk."""
        c = grid_cell(tip, out)
        while True:
            c = (c[0] + out[0], c[1] + out[1])
            owners = self.box_owner.get(c, set())
            if owner not in owners or owners - {owner} or c in self.pin_net or c in self.pin_lines:
                return
            self.hard.discard(c)

    def _mark(self, net: str, c: Cell, kind: str) -> None:
        self.occ.setdefault(c, {}).setdefault(net, set()).add(kind)
        self.net_cells.setdefault(net, set()).add(c)

    def add_segment(self, net: str, a: Cell, b: Cell) -> None:
        if a == b:
            self._mark(net, a, "n")
            return
        dx = (b[0] > a[0]) - (b[0] < a[0])
        dy = (b[1] > a[1]) - (b[1] < a[1])
        kind = "h" if dy == 0 else "v"
        n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        for k in range(n + 1):
            c = (a[0] + dx * k, a[1] + dy * k)
            self._mark(net, c, "n" if k in (0, n) else kind)
        for k in range(n + 1):
            c = (a[0] + dx * k, a[1] + dy * k)
            for nb in ((c[0] + 1, c[1]), (c[0] - 1, c[1]), (c[0], c[1] + 1), (c[0], c[1] - 1)):
                self.soft[nb] = self.soft.get(nb, 0) + 0.15

    def rip_up(self, net: str) -> None:
        for c in self.net_cells.pop(net, set()):
            d = self.occ.get(c)
            if d and net in d:
                del d[net]
                if not d:
                    del self.occ[c]

    # .................................................................. search
    def _enter_ok(self, net: str, c: Cell, move: int, final: bool) -> bool:
        if c in self.hard:
            return False
        owner = self.pin_net.get(c)
        if owner is not None and owner != net:
            return False
        others = {n: k for n, k in self.occ.get(c, {}).items() if n != net}
        if others:
            if final:
                return False
            for kinds in others.values():
                if "n" in kinds:
                    return False
                if move == H and "h" in kinds:
                    return False
                if move == V and "v" in kinds:
                    return False
        return True

    def _crossing(self, net: str, c: Cell) -> bool:
        return any(n != net for n in self.occ.get(c, {}))

    def search(self, net: str, start: Cell, first: tuple[int, int], targets: set[Cell],
               box_pad: int) -> list[Cell] | None:
        tx0 = min(t[0] for t in targets)
        tx1 = max(t[0] for t in targets)
        ty0 = min(t[1] for t in targets)
        ty1 = max(t[1] for t in targets)
        bx0 = max(self.bounds[0], min(start[0], tx0) - box_pad)
        bx1 = min(self.bounds[2], max(start[0], tx1) + box_pad)
        by0 = max(self.bounds[1], min(start[1], ty0) - box_pad)
        by1 = min(self.bounds[3], max(start[1], ty1) + box_pad)

        def h(c: Cell) -> float:
            dx = 0 if tx0 <= c[0] <= tx1 else min(abs(c[0] - tx0), abs(c[0] - tx1))
            dy = 0 if ty0 <= c[1] <= ty1 else min(abs(c[1] - ty0), abs(c[1] - ty1))
            return dx + dy

        BEND, CROSS = 2.0, 3.0
        # state: (cell, incoming move vector)
        counter = itertools.count()
        start_state = (start, (0, 0))
        g = {start_state: 0.0}
        prev: dict = {}
        heap = [(h(start), next(counter), start_state)]
        while heap:
            _, _, state = heapq.heappop(heap)
            c, inc = state
            gc = g[state]
            if c in targets and c != start:
                path = [c]
                s = state
                while s in prev:
                    s = prev[s]
                    path.append(s[0])
                return path[::-1]
            crossing_here = c != start and self._crossing(net, c)
            for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if inc == (0, 0) and d != first:
                    continue
                if inc != (0, 0) and d == (-inc[0], -inc[1]):
                    continue
                if crossing_here and d != inc:
                    continue   # straight through a crossing only
                nc = (c[0] + d[0], c[1] + d[1])
                if not (bx0 <= nc[0] <= bx1 and by0 <= nc[1] <= by1):
                    continue
                move = DIRS[d]
                is_target = nc in targets
                if nc in self.pin_net and not is_target:
                    continue   # never run a wire across a pin tip, even one of this net
                if not self._enter_ok(net, nc, move, final=is_target):
                    continue
                cost = gc + 1.0 + self.soft.get(nc, 0.0)
                if inc != (0, 0) and d != inc:
                    cost += BEND
                if self._crossing(net, nc):
                    cost += CROSS
                ns = (nc, d)
                if cost < g.get(ns, float("inf")):
                    g[ns] = cost
                    prev[ns] = state
                    heapq.heappush(heap, (cost + h(nc), next(counter), ns))
        return None

    # .............................................................. per net
    def route_net(self, net: str, terms: list[Terminal]) -> NetRoute:
        result = NetRoute(net)
        if len(terms) < 2:
            return result
        # wires already drawn for this net (stubs to flags) seed the tree
        existing = set(self.net_cells.get(net, ()))
        cx = sum(t.cell[0] for t in terms) / len(terms)
        cy = sum(t.cell[1] for t in terms) / len(terms)
        order = sorted(terms, key=lambda t: (t.cell not in existing, abs(t.cell[0] - cx) + abs(t.cell[1] - cy)))
        first = order[0]
        tree: set[Cell] = {first.cell} | existing
        self._mark(net, first.cell, "n")
        pending = order[1:]
        segs: list[tuple[Cell, Cell]] = []
        while pending:
            pending.sort(key=lambda t: min(abs(t.cell[0] - c[0]) + abs(t.cell[1] - c[1]) for c in tree))
            t = pending.pop(0)
            if t.cell in tree:
                continue
            path = None
            for pad in (8, 24, 80):
                path = self.search(net, t.cell, t.out, tree, pad)
                if path:
                    break
            if not path:
                result.failed_owners.append(t.owner)
                continue
            corners = [path[0]]
            for k in range(1, len(path) - 1):
                a, b, c = path[k - 1], path[k], path[k + 1]
                if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
                    corners.append(b)
            corners.append(path[-1])
            for a, b in zip(corners, corners[1:]):
                self.add_segment(net, a, b)
                segs.append((a, b))
            tree.update(path)
        result.segments = [(mm(a), mm(b)) for a, b in segs]
        return result


def normalise(segments: list[tuple[tuple[float, float], tuple[float, float]]],
              pins: list[tuple[float, float]]):
    """Split wires wherever something ends on them and find the junction dots.

    Returns (segments, junctions). After this every connection is end-to-end,
    so KiCad's connectivity does not depend on T-joins being inferred.
    """
    def key(p):
        return (round(p[0], 3), round(p[1], 3))

    points = {key(p) for p in pins}
    for a, b in segments:
        points.add(key(a))
        points.add(key(b))
    out = []
    for a, b in segments:
        a, b = key(a), key(b)
        if a == b:
            continue
        inner = []
        for p in points:
            if p in (a, b):
                continue
            if a[0] == b[0] == p[0] and min(a[1], b[1]) < p[1] < max(a[1], b[1]):
                inner.append(p)
            elif a[1] == b[1] == p[1] and min(a[0], b[0]) < p[0] < max(a[0], b[0]):
                inner.append(p)
        chain = [a] + sorted(inner, key=lambda p: abs(p[0] - a[0]) + abs(p[1] - a[1])) + [b]
        out.extend(zip(chain, chain[1:]))
    # drop exact duplicates
    seen = set()
    uniq = []
    for a, b in out:
        k = (a, b) if a <= b else (b, a)
        if k not in seen:
            seen.add(k)
            uniq.append((a, b))
    degree: dict = {}
    for a, b in uniq:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    for p in pins:
        k = key(p)
        if k in degree:
            degree[k] += 1
    junctions = [p for p, d in degree.items() if d >= 3]
    return uniq, junctions
