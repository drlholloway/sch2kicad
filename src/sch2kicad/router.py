"""Orthogonal wire routing on the 50 mil grid.

KiCad joins anything that touches at a wire end or a pin tip, so the rules
here are strict: another net's wire may be crossed only straight through
(no bend, no end, no junction there), another net's pin tips and every symbol
body are walls, and a wire never runs along another wire. A wire may pass
through a pin tip of its own net — that is a junction, and normalise() splits
it there. A net that cannot be routed under those rules falls back to net
labels, which are always correct.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field

from .geometry import GRID

Cell = tuple[int, int]
# what a step off the drawing's own wire for this net costs: enough that a trunk
# runs down the column the drawing gave it rather than the one the shortest tree
# finds, little enough that a guide a grid out of true never buys a detour
OFF_GUIDE = 0.25
# a bend costs this many grid of wire, a crossing of another net's wire this many.
# Three, not two: Lane's wiring takes the longer way round for a turn fewer (the
# FILTER return round the bottom of Memory Lapse); at three the drawn samples lose
# ten of their 24 jogs and 40 corners, still 60% on the drawing's own lines
BEND, CROSS = 3.0, 3.0
# a bend this many cells or fewer after the last one is a jog: a step that reads as
# a slip of the pen rather than a route, so it pays on top of the two bends
JOG_RUN, JOG = 3, 3.0
# how many of a net's pins (nearest its middle first) a tree is tried from
MAX_STARTS = 6
# what a wire turning right at a pin's tip pays, rather than setting off along the
# lead: a bend's worth (None forbids it). Lane runs a column straight down through
# a row of pin tips; stepping out a grid first cost two corners a pin
TIP_TURN: float | None = 2.0
# and what coming into a tip from the side pays: free, one branch could take a tip
# from the side and leave the next to kink round it (Musket's C2)
TIP_ARRIVE = 1.0
TEXT_MARGIN = 0.5      # a wire this near a piece of text pays as if it crossed it
TEXT_COST = 2.5        # what a wire pays for each grid step across text
WIDEN_OVER = 6.0       # a route dearer than its distance by this looks further afield
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
        self.pin_out: dict[Cell, tuple[int, int]] = {}   # which way each tip's lead points
        self.occ: dict[Cell, dict[str, set[str]]] = {}   # cell -> net -> {"h","v","n"}
        self.soft: dict[Cell, float] = {}            # cosmetic cost (near other things)
        self.net_cells: dict[str, set[Cell]] = {}
        self.guide: dict[str, set[Cell]] = {}        # net -> cells the drawing runs its wires on

    # ............................................................ obstacles
    def block_box(self, box: tuple[float, float, float, float], inflate: float = 0.6,
                  owner: str = "") -> None:
        x0, y0, x1, y1 = box
        i0, i1 = math.ceil((x0 - inflate) / GRID - 1e-6), math.floor((x1 + inflate) / GRID + 1e-6)
        j0, j1 = math.ceil((y0 - inflate) / GRID - 1e-6), math.floor((y1 + inflate) / GRID + 1e-6)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                if (i, j) in self.pin_net:
                    continue        # another symbol's pin tip: a wire has to be able to reach it
                self.hard.add((i, j))
                self.box_owner.setdefault((i, j), set()).add(owner)

    def avoid(self, box: tuple[float, float, float, float], cost: float | None = None) -> None:
        """Make a wire pay to cross something readable (a value, a reference).

        Soft, never a wall: a net that can only be joined through a piece of text is
        still joined — a detour is cosmetic, a missing wire is a dead board.
        """
        cost = TEXT_COST if cost is None else cost
        x0, y0, x1, y1 = box
        # the grid lines the text lies on, and those within a little of it: rounding
        # outward to whole cells charged a wire a grid clear of the text below it —
        # VOLUME's tip on R12's row went up and round its own name
        m = TEXT_MARGIN
        for i in range(int(math.ceil((x0 - m) / GRID)), int(math.floor((x1 + m) / GRID)) + 1):
            for j in range(int(math.ceil((y0 - m) / GRID)), int(math.floor((y1 + m) / GRID)) + 1):
                self.soft[(i, j)] = self.soft.get((i, j), 0.0) + cost

    def block_pin(self, tip: tuple[float, float], out: tuple[int, int], length: float, net: str | None) -> None:
        """Pin tip belongs to ``net``; the pin's line back to the body is a wall."""
        c = grid_cell(tip, out)
        self.pin_net[c] = net or f"__nc__{c}"
        self.pin_out[c] = out
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

    def search(self, net: str, start: Cell, first: tuple[int, int] | None, targets: set[Cell],
               box_pad: int) -> tuple[list[Cell], float] | None:
        """Cheapest path from ``start`` to any of ``targets``, and what it cost.

        ``first`` is the direction the wire must set off in — a pin's own lead, so the
        wire reads as continuing it. ``None`` lets it turn at the tip instead, for a pin
        whose lead points into a wall.
        """
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

        guide = self.guide.get(net)
        # state: (cell, incoming move vector, cells run since the last bend, capped);
        # a wire leaves its start as if it had come a long way, so turning just past
        # a pin is an ordinary bend and only a step between two bends is a jog
        counter = itertools.count()
        start_state = (start, (0, 0), JOG_RUN)
        g = {start_state: 0.0}
        prev: dict = {}
        heap = [(h(start), next(counter), start_state)]
        while heap:
            _, _, state = heapq.heappop(heap)
            c, inc, run = state
            gc = g[state]
            if c in targets and c != start:
                path = [c]
                s = state
                while s in prev:
                    s = prev[s]
                    path.append(s[0])
                return path[::-1], gc
            crossing_here = c != start and self._crossing(net, c)
            for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                # a wire sets off along the pin's own lead, or turns right at its tip
                # for a bend's cost — Lane runs a column straight down through R10's
                # and R7's tips, where stepping out a grid first cost two corners each
                if inc == (0, 0) and first is not None and d != first and \
                        (TIP_TURN is None or d == (-first[0], -first[1])):
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
                if nc in self.pin_net and not is_target and self.pin_net[nc] != net:
                    continue   # another net's pin tip is a wall; this net's own is a junction
                if not self._enter_ok(net, nc, move, final=is_target):
                    continue
                cost = gc + 1.0 + self.soft.get(nc, 0.0)
                turned = inc != (0, 0) and d != inc
                tip_turn = inc == (0, 0) and first is not None and d != first
                if turned:
                    cost += BEND + (JOG if run < JOG_RUN else 0.0)
                elif tip_turn:
                    cost += TIP_TURN                    # turning at the tip is a bend
                after = 1 if (turned or tip_turn) else min(run + 1, JOG_RUN)
                lead = self.pin_out.get(nc) if is_target else None
                if lead and d not in (lead, (-lead[0], -lead[1])) and TIP_TURN is not None:
                    # coming into a pin's tip from the side is the same bend as leaving it
                    # that way — free, it let one branch of Musket's Q2 collector take C2's
                    # tip from below and left the next to kink round it — and a grid or
                    # two after another bend it is a jog
                    cost += TIP_ARRIVE + (JOG if after < JOG_RUN and turned else 0.0)
                if self._crossing(net, nc):
                    cost += CROSS
                if guide and nc not in guide:
                    cost += OFF_GUIDE
                ns = (nc, d, after)
                if cost < g.get(ns, float("inf")):
                    g[ns] = cost
                    prev[ns] = state
                    heapq.heappush(heap, (cost + h(nc), next(counter), ns))
        return None

    # .............................................................. per net
    def route_net(self, net: str, terms: list[Terminal], turn_at_tip: bool = False) -> NetRoute:
        """Join a net's terminals as one tree, grown from whichever pin makes it cheapest.

        The tree is grown greedily, nearest pin first, so the pin it starts from
        settles where the trunk runs: started at a resistor's end, a collector's column
        jogs over to meet it; started at the collector, the resistor's lead reaches
        across instead. Each start is tried and the cheapest tree kept — the same costs
        the search pays (length, bends, jogs, crossings, distance off the drawing's own
        wire), so the choice is the one the search would make if it saw the whole net.
        """
        result = NetRoute(net)
        if len(terms) < 2:
            return result
        # wires already drawn for this net (stubs to flags) seed the tree
        existing = set(self.net_cells.get(net, ()))
        cx = sum(t.cell[0] for t in terms) / len(terms)
        cy = sum(t.cell[1] for t in terms) / len(terms)
        order = sorted(terms, key=lambda t: (t.cell not in existing, abs(t.cell[0] - cx) + abs(t.cell[1] - cy)))
        # a tree seeded by a flag's stub starts there; otherwise any pin may start it
        starts = order[:1] if order[0].cell in existing else order[:MAX_STARTS]
        best = None
        for first in starts:
            grown = self._grow(net, first, [t for t in order if t is not first], existing, turn_at_tip)
            key = (len(grown[1]), grown[2])     # fewest pins left out, then cheapest
            if best is None or key < best[0]:
                best = (key, grown, first)
        (segs, failed, _), first = best[1], best[2]
        self._mark(net, first.cell, "n")
        for a, b in segs:
            self.add_segment(net, a, b)
        result.failed_owners = failed
        result.segments = [(mm(a), mm(b)) for a, b in segs]
        return result

    def _grow(self, net: str, first: Terminal, pending: list[Terminal], existing: set[Cell],
              turn_at_tip: bool) -> tuple[list[tuple[Cell, Cell]], list[str], float]:
        """One greedy tree from ``first``: its segments, the pins it could not reach, its cost."""
        tree: set[Cell] = {first.cell} | existing
        pending = list(pending)
        segs: list[tuple[Cell, Cell]] = []
        failed: list[str] = []
        total = 0.0
        while pending:
            pending.sort(key=lambda t: min(abs(t.cell[0] - c[0]) + abs(t.cell[1] - c[1]) for c in tree))
            t = pending.pop(0)
            if t.cell in tree:
                continue
            found = None
            for pad in (8, 24, 80):
                found = self.search(net, t.cell, None if turn_at_tip else t.out, tree, pad)
                if found:
                    break
            if found and pad == 8:
                # the near box can hold only a dear way round: Memory Lapse's LM386 output
                # ran on through the grounds under PT2399, crossing their leads, where
                # Lane runs it a few grid lower, under them all
                near = min(abs(t.cell[0] - c[0]) + abs(t.cell[1] - c[1]) for c in tree)
                if found[1] > near + WIDEN_OVER:
                    wide = self.search(net, t.cell, None if turn_at_tip else t.out, tree, 24)
                    if wide and wide[1] < found[1]:
                        found = wide
            if not found:
                failed.append(t.owner)
                continue
            path, cost = found
            total += cost
            corners = [path[0]]
            for k in range(1, len(path) - 1):
                a, b, c = path[k - 1], path[k], path[k + 1]
                if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
                    corners.append(b)
            corners.append(path[-1])
            segs.extend(zip(corners, corners[1:]))
            tree.update(path)
        return segs, failed, total


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
