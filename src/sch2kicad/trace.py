"""Read the drawing a second time, and check the reading against it.

``verify`` proves the KiCad file matches ``circuit.json`` pin for pin. Nothing in
it can tell whether ``circuit.json`` matches the *drawing* — that is the one step
only careful reading does, and a misread junction there is a dead board.

So this module reads the source again by a different route: the PDF's own vectors
where the page has any, the pixels otherwise. It keeps only conductors — wires and
the leads that reach a pin — joins them the way a schematic joins them, and hands
back a netlist built without ever looking at the net names. Comparing the two says
where a reading and a drawing disagree.

What counts as a conductor:

* **Vectors** — every stroked path, as its points. They join only where two ends
  meet, never where one crosses another: Aion FX draws a crossing as a hop-over
  whose apex *touches* the wire it steps over, so a contact that is not an end is
  no connection.
* **Pixels** — long axis-aligned runs of ink, which is what a wire looks like and
  what text and a symbol's outline do not. Here an end touching another run's
  middle is a real T-junction, because a raster gives no better evidence — unless
  the drawing has left evidence of its own, and a **break either side of the wire
  it crosses** is exactly that (``Conductors._hops``): where two wires really
  meet the ink is continuous, so a gap is there because somebody put it there.
  The other way round, a run that **stops just past** the wire it crosses has met
  it (``Conductors._stops_past``): at a real crossing nothing ends, and ink that
  carries a stroke or two beyond has been drawn to the far rim of a junction dot.
  Crossing the line is the whole of that rule — two runs facing each other on one
  line never cross it, which is what keeps it out of a hop's gap and out of the
  one a part's body left behind.
  Which of the page's colours *is* the ink is read from the drawing rather than
  assumed (``wire_ink``): only a wire runs a twentieth of the page in a straight
  line, so that is what picks it. A page drawn in one colour picks that colour.
* **Neither** counts ink inside a part: the space a part's own pins span is its
  body, and a body is not a wire. Dropping it is what keeps an op-amp's inputs
  apart, and it can only split a net that should be whole — never join two that
  should be apart. The pins of a symbol sit on or near its outline, so the space
  they span is nearly the whole of it — what sticks out past them is the stroke
  itself, the bulge of the circle round a transistor, the corner of a resistor's
  box. That overhang is a matter of strokes, not of how big the part is drawn,
  so it is measured in **page units** (``PAGE_UNIT``, ``BODY_REACH``) — and a
  fixed count of pixels is exactly what gets it right on a 3000 px build doc and
  wrong on a 460 px hand-drawn GIF, where a resistor's outline stands 3 px off
  its own lead and a tracer that walks it joins the two nets the part is there
  to keep apart. A part drawn *round* its terminals is the other way about, and
  no measure of reach finds it: the shape itself has to be recognised. Four runs
  of ink closing a rectangle with the pins inside are an outline (``drop_boxes``)
  — which is what a vector page gets for nothing, a path that comes back to where
  it started being known for a shape before anything else looks at it.

A junction dot joins whatever it sits on, crossing or not.

A rail's name is the other way a drawing joins two pins — a flag on a short stem
with its name written beside it — so which wire a name belongs to is part of the
reading as well, and getting it wrong welds two rails together. What settles it is
how much blank paper lies between the name and the wire rather than how far apart
they are: the flag itself stands in between, and on a sheet that draws its symbols
in another colour that flag is not ink the tracer keeps (``name_nets``).

Where a pin sits is half of it. A pin belongs where the wire stops and the symbol
starts — not where the drawing writes its *number*, and not in the middle of the
body, both of which read plausibly on a zoomed crop and put the pin in blank
paper. So a pin with no wire under it is reported with the distance to the nearest
ink, which is what says what to do: eleven pixels away is a coordinate to correct,
and the drawing holds the answer. A part with no conductor anywhere in the space
its pins stand in is the other kind — one the sheet never drew (``_name_the_undrawn``).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

Point = tuple[float, float]
BEHIND_PENALTY = 5.0   # px: how much further a wire may be, to be one that leaves the part
BODY_REACH = 2.0       # how far past its own pins a symbol's ink reaches, in page units
PAGE_UNIT = 320        # the smallest thing a page can say: one part in this many of its width
MIN_NET = 3.0          # a conductor worth matching a pin to, in those units
BODY_GAP = 80          # pins further apart than this, in those units, are drawn apart
COLOUR_STEPS = 6       # how finely two colours are told apart, per channel
WIRE_RUN = 20          # only a wire runs this share of the page in a straight line
HOP_GAP = 3.0          # how wide the break marking a crossing can be, in page units
DIAG_GAP = 3          # px of paper a slanting wire may cross, where another passes through it
PAST_END = 0.9         # how far past a wire a run may stop and still have met it


@dataclass(frozen=True)
class Seg:
    a: Point
    b: Point

    def length(self) -> float:
        return math.dist(self.a, self.b)


@dataclass
class Dot:
    x: float
    y: float
    r: float


def _nearest_on(p: Point, s: Seg) -> Point:
    (ax, ay), (bx, by), (px, py) = s.a, s.b, p
    l2 = (bx - ax) ** 2 + (by - ay) ** 2
    if l2 == 0:
        return s.a
    t = max(0.0, min(1.0, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / l2))
    return (ax + t * (bx - ax), ay + t * (by - ay))


def _dist_to_seg(p: Point, s: Seg) -> float:
    return math.dist(p, _nearest_on(p, s))


def _line(s: Seg, axis: int) -> float:
    """Where an axis-aligned segment sits across ``axis``."""
    return (s.a[axis] + s.b[axis]) / 2


def _side(p: Point, s: Seg) -> float:
    """Which side of the line through ``s`` the point ``p`` falls on, in pixels."""
    (ax, ay), (bx, by) = s.a, s.b
    dx, dy = bx - ax, by - ay
    n = math.hypot(dx, dy)
    return ((p[0] - ax) * dy - (p[1] - ay) * dx) / n if n else 0.0


def _faces(a: Seg, c: Seg, axis: int, tol: float, gap: float) -> Point | None:
    """The middle of the gap two runs on one line leave facing each other, if it is small."""
    if abs(_line(a, 1 - axis) - _line(c, 1 - axis)) > tol:
        return None                             # not the same line
    a0, a1 = sorted((a.a[axis], a.b[axis]))
    c0, c1 = sorted((c.a[axis], c.b[axis]))
    if a1 <= c0:
        lo, hi = a1, c0
    elif c1 <= a0:
        lo, hi = c1, a0
    else:
        return None                             # they overlap: not two ends facing
    if not 0 < hi - lo <= gap:
        return None
    across = (_line(a, 1 - axis) + _line(c, 1 - axis)) / 2
    return ((lo + hi) / 2, across) if axis == 0 else (across, (lo + hi) / 2)


def _crosses(s: Seg, m: Point, axis: int, tol: float, wire: float) -> bool:
    """Does ``s`` run across the gap at ``m`` — through it, rather than stopping in it?

    ``wire`` is how long a run has to be to be a wire at all. A capacitor's plate
    stands across its own two leads exactly as a jumped wire stands in the gap
    between two stubs: through the middle of it, and past both sides. It is the
    part's own ink, kept on purpose, since a run through one pin is a lead.
    """
    if s.length() < wire:                       # a plate is as long as a capacitor
        return False
    if abs(s.a[axis] - s.b[axis]) > tol:        # it has to lie across the broken wire
        return False
    if _dist_to_seg(m, s) > tol:
        return False
    return min(math.dist(m, s.a), math.dist(m, s.b)) > tol   # one that stops here is a T


def _at_cell(index: dict[tuple[int, int], set[int]], p: Point, cell: float) -> set[int]:
    """Everything indexed in the cell holding ``p``, and in the ones round it."""
    gx, gy = int(p[0] // cell), int(p[1] // cell)
    out: set[int] = set()
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            out |= index.get((gx + dx, gy + dy), set())
    return out


class Conductors:
    """Segments and dots, joined into nets."""

    def __init__(self, segs: list[Seg], dots: list[Dot], t_contacts: bool, tol: float = 2.0,
                 page: float = 0.0):
        self.segs = segs
        self.dots = dots
        self.tol = tol
        self.page = page       # the page's own smallest unit; 0 leaves crossings alone
        self.past = PAST_END * page     # how far past a wire a run may stop and still have met it
        self._parent: dict[int, int] = {}
        self._len: dict[int, float] | None = None
        self._join(t_contacts)

    # ......................................................... union-find
    def find(self, i: int) -> int:
        p = self._parent
        p.setdefault(i, i)
        while p[i] != i:
            p[i] = p[p[i]]
            i = p[i]
        return i

    def union(self, i: int, j: int) -> None:
        self._parent[self.find(i)] = self.find(j)

    def _join(self, t_contacts: bool) -> None:
        # the index has to reach as far as the furthest thing that joins, or a pair is
        # kept apart by where a cell boundary happens to fall rather than by a rule
        reach = max(self.tol, self.past)
        cell = max(reach * 2, 4.0)
        near: dict[tuple[int, int], set[int]] = defaultdict(set)
        for i, s in enumerate(self.segs):
            self.find(i)
            x0, x1 = sorted((s.a[0], s.b[0]))
            y0, y1 = sorted((s.a[1], s.b[1]))
            for gx in range(int((x0 - reach) // cell), int((x1 + reach) // cell) + 1):
                for gy in range(int((y0 - reach) // cell), int((y1 + reach) // cell) + 1):
                    near[(gx, gy)].add(i)
        apart = self._hops(near, cell) if self.page else set()
        for members in near.values():
            for i in members:
                for j in members:
                    if i < j and (i, j) not in apart and self._touch(
                            self.segs[i], self.segs[j], t_contacts):
                        self.union(i, j)
        # a dot joins everything it sits on, crossing or not — including a crossing
        # the ink says is a hop, since a drawing that means both says the dot
        for d in self.dots:
            on = [i for i, s in enumerate(self.segs)
                  if _dist_to_seg((d.x, d.y), s) <= d.r + self.tol]
            for i in on[1:]:
                self.union(on[0], i)

    def _hops(self, near: dict[tuple[int, int], set[int]], cell: float) -> set[tuple[int, int]]:
        """The crossings a drawing marks by breaking the wire that is jumped.

        Escobedo's sheets show a crossing as a gap: the ink of the wire being stepped
        over stops a pixel short of the other, picks up a pixel past it, and nothing
        joins them. The gap is narrower than the tolerance a raster needs for its
        T-junctions, so each stub ends within reach of the wire between them and the
        tracer welds all three — on the Gargler that put the whole signal bus on the
        filter's own node, eight pins across two nets.

        **The gap is itself the evidence.** Where two wires really meet, the ink is
        continuous and a raster reads one run, not two: a break exists only because
        the drawing put it there, and the one reason to break a wire is to say it does
        not connect. So two runs on one line, facing each other across a gap no wider
        than a wire, with a third running across the middle of it, are one wire
        hopping over another.

        Three things keep it honest, and the middle one is the whole difficulty.

        The crossing has to be **centred** in the gap, which an unrelated wire passing
        between two unrelated runs will not be.

        Both wires have to **be wires**. A break is not the only thing that leaves two
        runs facing each other on one line: dropping a part's body leaves the same
        shape, and a capacitor's plate then stands in the gap between its own two leads
        exactly as a jumped wire stands between two stubs — through the middle, and past
        both sides. Reading that as a crossing welds the capacitor end to end, which is
        the worst thing this module can do. Length is what tells them apart, measured
        the way ``wire_ink`` measures it: only a wire runs a twentieth of the page in a
        straight line, and a plate is as long as a capacitor. The same bar on the broken
        wire keeps two letter strokes either side of a title-block rule from counting.

        And the crossing has to **pass through** rather than stop in the gap: a wire
        ending between the two is a T-junction. A four-way junction is drawn with a dot,
        which joins everything it sits on whatever this decides.

        Returns the pairs that must not be joined, and joins the two stubs to each
        other, since the wire they belong to does carry on across the crossing.
        """
        tol, gap = self.tol, HOP_GAP * self.page
        wire = PAGE_UNIT / WIRE_RUN * self.page      # only a wire runs this far straight
        lines: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, s in enumerate(self.segs):
            for axis in (0, 1):
                if abs(s.a[1 - axis] - s.b[1 - axis]) <= tol:       # runs along `axis`
                    lines[(axis, int(_line(s, 1 - axis) // cell))].append(i)
        apart: set[tuple[int, int]] = set()
        for (axis, row), here in lines.items():
            for i in here:
                for j in here + lines.get((axis, row + 1), []):
                    if j <= i:
                        continue
                    a, c = self.segs[i], self.segs[j]
                    if max(a.length(), c.length()) < wire:
                        continue            # a wire is what gets broken; this is not one
                    m = _faces(a, c, axis, tol, gap)
                    if m is None:
                        continue
                    jumped = [k for k in _at_cell(near, m, cell) if k not in (i, j)
                              and _crosses(self.segs[k], m, axis, tol, wire)]
                    if not jumped:
                        continue
                    self.union(i, j)                    # the wire carries on across
                    for k in jumped:
                        apart.add((min(i, k), max(i, k)))
                        apart.add((min(j, k), max(j, k)))
        return apart

    def _stops_past(self, s: Seg, t: Seg) -> bool:
        """Does a run of ``s`` stop just past ``t``, having crossed the line it stands on?

        Where a wire crosses another and carries on, nothing ends. Where it stops a
        stroke or two beyond, the drawing has put a junction dot there: the dot is
        fatter than the wire, so the raster grows the run to the dot's far rim and
        the run ends a dot's radius past the wire it meets. The Theremin draws both
        of its four-way junctions that way, and the two ends land 3.3 and 3.5 px
        past a wire the tracer will only join within 2. The Christine's heavy dots
        put three T's 3 px past on a page whose unit is 3.75 px, 0.8 of one.

        Crossing the line is the whole guard. A run that stops *short* of another has
        not touched it, however near it comes, and that gap is the one the hop rule
        reads: a wire broken either side of the one it jumps stays on its own side of
        that wire's line, so nothing here sees it.
        """
        if _side(s.a, t) * _side(s.b, t) >= 0:      # it stays on one side: it never reached
            return False
        return any(_dist_to_seg(p, t) <= self.past for p in (s.a, s.b))

    def _touch(self, s: Seg, t: Seg, t_contacts: bool) -> bool:
        tol = self.tol
        for p in (s.a, s.b):
            for q in (t.a, t.b):
                if math.dist(p, q) <= tol:
                    return True
        if not t_contacts:
            return False
        if (any(_dist_to_seg(p, t) <= tol for p in (s.a, s.b))
                or any(_dist_to_seg(q, s) <= tol for q in (t.a, t.b))):
            return True
        return bool(self.past) and (self._stops_past(s, t) or self._stops_past(t, s))

    # ............................................................. lookup
    def nearest(self, p: Point) -> float:
        """How far the nearest conductor is from ``p``, at any distance.

        What tells a pin read a few pixels off its wire from a part the drawing
        never drew: the first has ink beside it, the second has none anywhere.
        """
        return min((_dist_to_seg(p, s) for s in self.segs), default=math.inf)

    def net_length(self) -> dict[int, float]:
        if self._len is None:
            self._len = defaultdict(float)
            for i, s in enumerate(self.segs):
                self._len[self.find(i)] += s.length()
        return self._len

    def net_at(self, p: Point, tol: float, min_len: float = 0.0,
               out: Point | None = None) -> int | None:
        """The net of the conductor nearest ``p``, if one is within ``tol``.

        A pixel reading gives the odd stray mark — a letter's crossbar, a stub of
        a symbol — so a conductor with nothing to it is passed over for the wire a
        little further off. Passed over, not refused: where the short conductor is
        the only one a pin has, it is the answer. A sheet that draws its symbols in
        their own colour leaves nothing of them in the wire ink, so what is left
        between two of them is the hop alone — on 22359 a pot's wiper reaches its
        resistor in sixteen pixels and its ground flag in eight, and refusing those
        loses the pin and, worse, sends the flag's name to whatever longer wire
        happens to pass nearby.

        ``out`` is the way the pin's lead leaves its part, and the wire is looked
        for that way: the other plate of a capacitor is as close to the pin as its
        own, and belongs to the other net.
        """
        lengths = self.net_length()
        best: tuple[float, float, int] | None = None
        for i, s in enumerate(self.segs):
            q = _nearest_on(p, s)
            d = math.dist(p, q)
            if d > tol:
                continue
            # a pin's own place is read to within a few pixels, so a wire just behind
            # it is still its wire — but one that leaves the part is the better answer.
            # Strictly behind: a conductor the pin stands on is not behind it, and the
            # dot product is exactly zero there, since the nearest point is the pin.
            behind = out is not None and (q[0] - p[0]) * out[0] + (q[1] - p[1]) * out[1] < 0
            score = d + (BEHIND_PENALTY if behind else 0.0)
            if lengths[self.find(i)] < min_len:
                score += tol                        # last, behind anything else in reach
            if best is None or score < best[0]:
                best = (score, d, i)
        return self.find(best[2]) if best else None


# ------------------------------------------------------------------ bodies

def _in_box(p: Point, box) -> bool:
    x0, y0, x1, y1 = box
    return x0 <= p[0] <= x1 and y0 <= p[1] <= y1


def _grow(box, by: float):
    x0, y0, x1, y1 = box
    return (x0 - by, y0 - by, x1 + by, y1 + by)


def _at(s: Seg, t: float) -> Point:
    return (s.a[0] + t * (s.b[0] - s.a[0]), s.a[1] + t * (s.b[1] - s.a[1]))


def _param(s: Seg, p: Point) -> float:
    """Where along ``s`` the foot of ``p`` falls, as t in [0, 1]."""
    (ax, ay), (bx, by) = s.a, s.b
    l2 = (bx - ax) ** 2 + (by - ay) ** 2
    if l2 == 0:
        return 0.0
    return max(0.0, min(1.0, ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / l2))


def _inside_span(s: Seg, box) -> tuple[float, float] | None:
    """The stretch of ``s`` that lies inside ``box``, as t in [0, 1]; None if none."""
    lo, hi = 0.0, 1.0
    for p0, p1, b0, b1 in ((s.a[0], s.b[0], box[0], box[2]),
                           (s.a[1], s.b[1], box[1], box[3])):
        d = p1 - p0
        if abs(d) < 1e-9:
            if not b0 <= p0 <= b1:
                return None
            continue
        t0, t1 = (b0 - p0) / d, (b1 - p0) / d
        lo, hi = max(lo, min(t0, t1)), min(hi, max(t0, t1))
        if lo > hi:
            return None
    return lo, hi


def _except(s: Seg, span: tuple[float, float] | None) -> list[Seg]:
    """``s`` with the stretch ``span`` taken out of it."""
    if span is None:
        return [s]
    lo, hi = span
    out = []
    if lo > 0:
        out.append(Seg(s.a, _at(s, lo)))
    if hi < 1:
        out.append(Seg(_at(s, hi), s.b))
    return out


def _runs(s: Seg, lo: float, hi: float, axis: int, tol: float) -> bool:
    """Does ``s`` run from ``lo`` to ``hi`` along ``axis`` — all the way, and no further?"""
    a, b = sorted((s.a[axis], s.b[axis]))
    return abs(a - lo) <= tol and abs(b - hi) <= tol


def _spans(s: Seg, lo: float, hi: float, axis: int, tol: float) -> bool:
    return min(s.a[axis], s.b[axis]) <= lo + tol and max(s.a[axis], s.b[axis]) >= hi - tol


def _box_round(segs: list[Seg], pins, touch: float) -> list[Seg]:
    """The four runs of ink that draw a closed rectangle round ``pins``, if they are there.

    Each side has to run the whole way between its two corners **and stop there**.
    That is what makes the shape a closed one rather than four lines that happen to
    cross: a rail passing a part runs on past the corner, and two parts standing
    between the same two rails make a rectangle whose sides do not stop.
    """
    x0, y0, x1, y1 = pins
    across = sorted((s for s in segs if abs(s.a[1] - s.b[1]) <= touch
                     and _spans(s, x0, x1, 0, touch)), key=lambda s: _line(s, 1))
    down = sorted((s for s in segs if abs(s.a[0] - s.b[0]) <= touch
                   and _spans(s, y0, y1, 1, touch)), key=lambda s: _line(s, 0))
    for top, bot in combinations(across, 2):
        for left, right in combinations(down, 2):
            lx, rx = _line(left, 0), _line(right, 0)
            ty, by = _line(top, 1), _line(bot, 1)
            if (_runs(top, lx, rx, 0, touch) and _runs(bot, lx, rx, 0, touch)
                    and _runs(left, ty, by, 1, touch) and _runs(right, ty, by, 1, touch)):
                return [top, bot, left, right]
    return []


def drop_boxes(segs: list[Seg], circuit, touch: float = 2.0) -> list[Seg]:
    """A closed rectangle of ink round a part's own pins is its outline, not four wires.

    This is the rule vector mode gets for nothing. There a path is an object, so one
    that comes back to where it started is known for a shape and dropped before
    anything else looks at it. A raster has no paths — only runs of ink — so the shape
    has to be found: four runs, two across and two down, each reaching from one corner
    to the other and stopping, with the part's pins inside.

    Without it a part drawn as a box with its terminals *inside* the outline welds
    them all together, because the sides chain one to the next round the corners. The
    Disorder's DPDT is drawn that way, and the four sides of its two boxes made one
    node of both poles and three separate off-page nets — which is the signature of a
    short, and the reason a false join is the direction worth spending a pass on.

    The pins have to be inside because that is what says the rectangle belongs to
    *this* part. A chip drawn as a box with its pins sticking out needs nothing from
    here: its pins already span its outline, so the body rule below covers it.
    """
    for part in circuit.parts:
        pts = [pin.at for pin in part.pins.values() if pin.at]
        pts += [at for at in part.supply_at.values() if at]
        if len(pts) < 2:
            continue
        for group in _drawn_apart(pts, BODY_GAP * touch):
            if len(group) < 2:
                continue                            # a lone stub draws no box
            xs, ys = [p[0] for p in group], [p[1] for p in group]
            sides = _box_round(segs, (min(xs), min(ys), max(xs), max(ys)), touch)
            if sides:
                segs = [s for s in segs if s not in sides]
    return segs


def drop_bodies(segs: list[Seg], circuit, touch: float = 2.0, min_len: float = 1.0) -> list[Seg]:
    """Drop what a part draws of itself, keep what reaches it.

    Taken part by part, each judged against its own pins. Against one part a
    segment is one of four things:

    * **through two of its pins** — the part's own body lying between them: a
      filled diode, the channel bar of a JFET, a plate pair read as one run. Cut
      the segment there; each pin keeps the wire on its own side.
    * **through one of its pins** — a lead, or a wire running up to one. Keep it
      whole: it is the one thing a body edge never does. Unless it is **half of
      one run broken in two** — two leads of the part on one line, through two of
      its pins, facing each other across a hairline between them — which is the
      first case again, and cut the same way (``_halves``).
    * **nowhere near a pin, and wholly inside the part's reach** — an outline, a
      zigzag, an arc of the circle round a transistor. Drop it.
    * **anything else** — a wire that only passes by. Keep the parts of it outside
      the space its pins span, which is where a wire cannot be.

    ``touch`` is how near counts as running through a pin: the page's own
    smallest unit, never a share of the part. A symbol's corner falls a stroke or
    two from the pin beside it whatever the part's size, and a tolerance that
    grew with the part would cut real wire out of a big chip's neighbours.

    Dropping and cutting can only split a net that should be whole; neither can
    join two that should be apart. That is what makes this safe to do at all.

    A part is judged one place at a time, because a sheet may draw it in more
    than one: 22359 puts the 4040's supply pins over in its power block, a third
    of a page from the chip. Taken together those pins span half the drawing, and
    everything inside that rectangle would be read as the chip's own ink. Pins
    that sit together are one drawing of the part and have a body between them;
    pins off on their own are another, and the empty page between the two is
    nobody's body.
    """
    for part in circuit.parts:
        pts = [pin.at for pin in part.pins.values() if pin.at]
        pts += [at for at in part.supply_at.values() if at]
        if len(pts) < 2:
            continue
        for group in _drawn_apart(pts, BODY_GAP * touch):
            if len(group) < 2:
                continue                            # a lone stub draws no body
            xs, ys = [p[0] for p in group], [p[1] for p in group]
            span = (min(xs), min(ys), max(xs), max(ys))
            near = _grow(span, BODY_REACH * touch)
            out, leads = [], []
            for s in segs:
                if (max(s.a[0], s.b[0]) < near[0] or min(s.a[0], s.b[0]) > near[2]
                        or max(s.a[1], s.b[1]) < near[1] or min(s.a[1], s.b[1]) > near[3]):
                    out.append(s)                   # not this part's business
                    continue
                on = [p for p in group if _dist_to_seg(p, s) <= touch]
                if len(on) >= 2:
                    ts = sorted(_param(s, p) for p in on)
                    out += _except(s, (ts[0], ts[-1]))      # its body, between its pins
                elif on:
                    leads.append((len(out), s, on[0]))      # a lead runs through its pin
                    out.append(s)
                elif _in_box(s.a, near) and _in_box(s.b, near):
                    continue                                # an outline, drawn round it
                else:
                    out += _except(s, _inside_span(s, _grow(span, touch)))
            halves = _halves([(s, p) for _, s, p in leads], touch)
            pieces = {leads[i][0]: cut for i, cut in halves.items()}   # one run broken in two
            segs = [s for i, whole in enumerate(out) for s in pieces.get(i, [whole])
                    if s.length() >= min_len]
    return segs


def _halves(leads: list[tuple[Seg, Point]], touch: float) -> dict[int, list[Seg]]:
    """Which of a part's leads are one run broken in two, each cut back to its pin
    (by position in ``leads``).

    Two leads of one part on the same line, each through a different one of its
    pins, whose inner ends face each other across less than ``touch`` *between*
    those pins, are the part's own body read in two pieces — a plate pair, a bar —
    and are cut at their pins as a single run through both would be. Nothing wider
    than a hairline qualifies, so a pin read out along its wire never has that wire
    taken for body: the gap to whatever faces it is the whole length of the part.
    """
    cut: dict[int, tuple[float, float]] = {}
    for i, (s, p) in enumerate(leads):
        for t, q in leads[i + 1:]:
            if p == q:
                continue
            for axis in (0, 1):
                other = 1 - axis
                flat = (abs(s.a[other] - s.b[other]) < 1.0 and abs(t.a[other] - t.b[other]) < 1.0
                        and abs(s.a[other] - t.a[other]) <= 1.5)
                if not flat or abs(p[axis] - q[axis]) <= touch:
                    continue
                lo_pin, hi_pin = (p, q) if p[axis] < q[axis] else (q, p)
                lo_seg, hi_seg = (s, t) if lo_pin is p else (t, s)
                lo_end = max(lo_seg.a[axis], lo_seg.b[axis])     # the ends that face each other
                hi_end = min(hi_seg.a[axis], hi_seg.b[axis])
                if not (lo_pin[axis] < lo_end <= hi_end < hi_pin[axis] and hi_end - lo_end <= touch):
                    continue
                cut[id(lo_seg)] = (_param(lo_seg, lo_pin), 1.0) if lo_seg.b[axis] >= lo_seg.a[axis] \
                    else (0.0, _param(lo_seg, lo_pin))
                cut[id(hi_seg)] = (0.0, _param(hi_seg, hi_pin)) if hi_seg.b[axis] >= hi_seg.a[axis] \
                    else (_param(hi_seg, hi_pin), 1.0)
    return {i: _except(s, cut[id(s)]) for i, (s, _) in enumerate(leads) if id(s) in cut}


def _drawn_apart(pts: list[Point], gap: float) -> list[list[Point]]:
    """Split a part's pins into the places the sheet draws it, by single linkage."""
    left, out = list(pts), []
    while left:
        group, more = [left.pop()], True
        while more:
            more = False
            for i in range(len(left) - 1, -1, -1):
                if any(math.dist(left[i], p) <= gap for p in group):
                    group.append(left.pop(i))
                    more = True
        out.append(group)
    return out


def pin_points(circuit) -> list[Point]:
    pts = [pin.at for p in circuit.parts for pin in p.pins.values() if pin.at]
    pts += [at for p in circuit.parts for at in p.supply_at.values() if at]
    pts += [port.at for port in circuit.ports if port.at]
    return pts


# ------------------------------------------------------------------ vector

def _closed(items) -> bool:
    """A path that comes back to where it started: a triangle, a box, a circle."""
    if any(it[0] == "re" for it in items):
        return True
    pts = [q for it in items if it[0] in ("l", "c", "qu") for q in (it[1], it[-1])]
    return len(pts) > 3 and abs(pts[0].x - pts[-1].x) < 0.5 and abs(pts[0].y - pts[-1].y) < 0.5


def turn_point(p: Point, rot: int, w: float, h: float) -> Point:
    """Where ``p`` lands once a ``w`` x ``h`` picture is turned ``rot`` degrees clockwise,
    as ``Image.rotate(-rot, expand=True)`` turns it."""
    x, y = p
    if rot == 90:
        return (h - y, x)
    if rot == 180:
        return (w - x, h - y)
    if rot == 270:
        return (y, w - x)
    return (x, y)


def vector_segments(pdf: Path, page_no: int, scale: float, rot: int = 0) -> tuple[list[Seg], list[Dot]]:
    """Stroked paths as segments, filled circles as junction dots, in image pixels.

    ``rot`` is how far ingest turned the page (the Acapulco Tone draws its circuit
    sideways on an upright page), so the segments land on ``source.png`` as it is.
    """
    import pymupdf

    doc = pymupdf.open(pdf)
    page = doc[page_no - 1]
    w, h = page.rect.width * scale, page.rect.height * scale

    def at(x: float, y: float) -> Point:
        return turn_point((x * scale, y * scale), rot, w, h)
    segs: list[Seg] = []
    dots: list[Dot] = []
    for d in page.get_drawings():
        items = d["items"]
        if "f" in (d["type"] or "") and items and all(it[0] in ("c", "qu") for it in items):
            r = d["rect"]                       # a filled blob: a junction dot
            dw, dh = (r.x1 - r.x0) * scale, (r.y1 - r.y0) * scale
            if dw <= 12 and dh <= 12 and abs(dw - dh) <= 3:
                dots.append(Dot(*at((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2), max(dw, dh) / 2))
            continue
        if "s" not in (d["type"] or "") or _closed(items):
            continue                            # a closed path is an outline, not a wire
        if min(d.get("color") or (0,)) > 0.9:
            continue    # paper-white: nothing anyone sees (the Acapulco Tone's page-wide X)
        for it in items:
            pts = []
            if it[0] == "l":
                pts = [it[1], it[2]]
            elif it[0] in ("c", "qu"):          # a curve joins its ends like a chord
                pts = [it[1], it[-1]]
            for u, v in zip(pts, pts[1:]):
                a = at(u.x, u.y)
                b = at(v.x, v.y)
                if a != b:
                    segs.append(Seg(a, b))
    doc.close()
    return _drop_crosses(segs, 12 * scale), dots


def _drop_crosses(segs: list[Seg], most: float) -> list[Seg]:
    """Leave out the X a sheet marks a pad or a no-connect with.

    Two short strokes crossing at both their middles are a mark, not wires: the
    Acapulco Tone ends each of its pads (IN, 9V, LED) in one, the lead starting at
    the very point they cross. Kept, the pin read there lands on a stroke that
    meets nothing, since on a vector page strokes join only end to end.
    """
    short = [i for i, s in enumerate(segs)
             if s.length() <= most and abs(s.a[0] - s.b[0]) > 0.3 * s.length()
             and abs(s.a[1] - s.b[1]) > 0.3 * s.length()]
    drop: set[int] = set()
    for i, j in combinations(short, 2):
        s, t = segs[i], segs[j]
        ms, mt = _at(s, 0.5), _at(t, 0.5)
        if math.dist(ms, mt) <= 0.1 * s.length() and abs(s.length() - t.length()) <= 0.2 * s.length() \
                and abs(_side(t.a, s)) > 0.3 * s.length():
            drop |= {i, j}
    return [s for k, s in enumerate(segs) if k not in drop]


# ------------------------------------------------------------------ raster

def _long_ink(mask, least: int) -> int:
    """How much of ``mask`` sits in axis-aligned runs of ``least`` pixels or more."""
    import numpy as np

    total = 0
    for m in (mask, mask.T):
        pad = np.zeros((m.shape[0], m.shape[1] + 2), dtype=bool)
        pad[:, 1:-1] = m
        d = np.diff(pad.astype(np.int8), axis=1).ravel()
        runs = np.flatnonzero(d == -1) - np.flatnonzero(d == 1)
        total += int(runs[runs >= least].sum())
    return total


def wire_ink(rgb, min_len: int, ink_level: int = 170):
    """Which of the colours on this page is the one the wires are drawn in.

    Ink is what stands far enough off the page, measured in whichever channel
    stands furthest. That matters: a saturated green wire is as far from white as
    a black one, but its *darkest* channel is bright, so the older test — dark and
    colourless — kept a navy symbol and lost the wire it was drawn beside.

    Which colour is the wires is then read from the drawing instead of assumed.
    Anything a page is drawn in comes in one colour per kind of thing, and only a
    wire runs a twentieth of the page in a straight line: a resistor's outline is
    as long as a resistor. So each colour is scored by how much of its ink lies in
    runs that long, and the winner is the ink. Blending toward white scales all
    three channels together and leaves the direction alone, so a colour's paler
    edge is counted with its core rather than as a colour of its own.

    A page drawn in one colour picks that colour, which is what every sheet before
    22359 did anyway. A page that draws its wires in two colours would keep only
    the busier one — say so in the report rather than trusting it.

    How far off the page a line stands is read across the pair it may be split
    over. A line thinner than a pixel that falls between two of them puts half its
    ink in each, so neither is as dark as the bar though together they carry a
    whole line's worth; on 22359 that is the difference between a wire at 165 and
    the same wire, drawn a third of a pixel over, at 64 and 76.
    """
    import numpy as np

    off = 255 - rgb                                 # how far from the page, per channel
    depth = off.max(axis=2)
    bar = 255 - ink_level
    beside = np.zeros_like(depth)                   # the deepest of the four neighbours
    beside[:, :-1] = np.maximum(beside[:, :-1], depth[:, 1:])
    beside[:, 1:] = np.maximum(beside[:, 1:], depth[:, :-1])
    beside[:-1, :] = np.maximum(beside[:-1, :], depth[1:, :])
    beside[1:, :] = np.maximum(beside[1:, :], depth[:-1, :])
    # its own share has to be real, so blank paper beside a wire is still paper
    on = (depth >= bar / 2) & (depth + beside >= bar)
    if not on.any():
        return on
    shade = np.round(off[on] / depth[on][:, None] * COLOUR_STEPS).astype(np.int32)
    code = (shade[:, 0] * (COLOUR_STEPS + 1) + shade[:, 1]) * (COLOUR_STEPS + 1) + shade[:, 2]
    counts = np.bincount(code)
    whole = np.zeros(rgb.shape[:2], np.int32)
    whole[on] = code
    least = max(min_len, rgb.shape[1] // WIRE_RUN)
    best, ink = -1, None
    for key in np.argsort(counts)[::-1][:6]:
        if counts[key] < on.sum() * 0.02:
            break
        only = on & (whole == key)
        score = _long_ink(only, least)
        if score > best:
            best, ink = score, only
    return ink if ink is not None else on


def page_ink(path: Path, ink_level: int = 170):
    """Everything drawn on the page, in whatever colour: wires, symbols and text alike.

    ``wire_ink`` keeps the one colour the wires are drawn in, which is what a tracer
    wants and all it wants. This keeps the rest of the drawing as well, which is what
    says whether the space between two things on the page is a symbol or bare paper.
    """
    import numpy as np
    from PIL import Image

    with Image.open(path) as im:
        rgb = np.asarray(im.convert("RGB")).astype(np.int16)
    return (255 - rgb).max(axis=2) >= 255 - ink_level


def raster_segments(path: Path, min_len: int | None = None, max_width: int = 5,
                    ink_level: int = 170) -> tuple[list[Seg], list[Dot]]:
    """Long thin runs of dark ink: what a wire looks like, and text does not.

    A run is grown across neighbouring rows (or columns) so a two-pixel line counts
    once; a group thicker than ``max_width`` is a filled shape, not a wire. How long
    a run has to be goes with how big the drawing is: a sheet scanned small draws
    the same wire in fewer pixels, and asking for nine of them loses it.
    """
    import numpy as np
    from PIL import Image

    with Image.open(path) as im:
        rgb = np.asarray(im.convert("RGB")).astype(np.int16)
    if min_len is None:
        min_len = max(5, round(rgb.shape[1] / PAGE_UNIT))
    dark = wire_ink(rgb, min_len, ink_level)
    segs: list[Seg] = []
    for axis in (0, 1):                          # 0: rows → horizontal runs, 1: columns
        m = dark if axis == 0 else dark.T
        runs: dict[int, list[tuple[int, int]]] = {}
        pad = np.zeros((m.shape[0], m.shape[1] + 2), dtype=bool)
        pad[:, 1:-1] = m
        d = np.diff(pad.astype(np.int8), axis=1)
        for line in range(m.shape[0]):
            starts = np.flatnonzero(d[line] == 1)
            stops = np.flatnonzero(d[line] == -1)
            runs[line] = [(int(s), int(e) - 1) for s, e in zip(starts, stops) if e - s >= min_len]
        segs += _group_runs(runs, axis, max_width)
    diagonals = _diagonals(dark, max(min_len, dark.shape[1] // WIRE_RUN))
    if diagonals:
        segs = [s for s in segs if not _along(s, diagonals)] + diagonals
    return segs, []


def _diagonals(dark, wire: int, band: float = 1.5) -> list[Seg]:
    """Wires drawn on the slant, each as one segment.

    A raster keeps a wire as a run of ink along a row or a column, and a slanting
    wire has none: a shallow one comes out as a staircase of short steps, each a run
    in its own right, chained end to end within the tracer's reach. That carries it
    well enough — until two of them cross. The Angry Droid's multivibrator crosses
    its capacitors and its diodes over to the opposite transistor in two Xs, with no
    dot, and where the staircases pass through each other they share pixels, so the
    steps chained one side of the circuit to the other: both collectors and both
    bases, one net.

    Read as lines, they are what a vector page would have made of them: two strokes
    that meet only where one of them ends. A slanting line is only a wire if it runs
    straight for a tenth of the page, twice what a straight wire has to, and the ink
    is unbroken along it but for the gap where it passes through another: no letter,
    zigzag, arc of a logo or side of an op-amp's triangle does that, and those stay
    the staircases they always were.
    """
    import cv2
    import numpy as np

    img = dark.astype(np.uint8) * 255
    found = cv2.HoughLinesP(img, 1, np.pi / 360, threshold=max(20, wire // 2),
                            minLineLength=wire, maxLineGap=2)
    if found is None:
        return []
    h, w = dark.shape

    def inked(p) -> bool:
        """Ink on the line itself: the two pixels either side of it, and no further,
        so a walk that carries on into the wire it meets stops a few pixels in."""
        x, y = p
        near = {(math.floor(x), math.floor(y)), (math.ceil(x), math.ceil(y)),
                (math.floor(x), math.ceil(y)), (math.ceil(x), math.floor(y))}
        return any(0 <= j < h and 0 <= i < w and dark[j, i] for i, j in near)

    def walk(p, u) -> Point:
        """Out along ``u`` from ``p`` for as long as the ink goes, over a crossing's gap."""
        miss, step = 0, p
        while miss <= DIAG_GAP:
            nxt = (step[0] + u[0], step[1] + u[1])
            if not (0 <= nxt[0] < w and 0 <= nxt[1] < h):
                break
            step = nxt
            if inked(step):
                p, miss = step, 0
            else:
                miss += 1
        return p

    ys, xs = np.nonzero(dark)

    def refit(s: Seg) -> Seg:
        """The straight line through the ink along ``s``, over the same span."""
        x0, x1 = sorted((s.a[0], s.b[0]))
        y0, y1 = sorted((s.a[1], s.b[1]))
        box = (xs >= x0 - 2) & (xs <= x1 + 2) & (ys >= y0 - 2) & (ys <= y1 + 2)
        px, py = xs[box].astype(float), ys[box].astype(float)
        dx, dy = s.b[0] - s.a[0], s.b[1] - s.a[1]
        n = math.hypot(dx, dy)
        on = np.abs((px - s.a[0]) * dy - (py - s.a[1]) * dx) / n <= band
        if on.sum() < 2:
            return s
        px, py = px[on], py[on]
        if abs(dx) >= abs(dy):                  # shallow: y as a function of x
            k, c = np.polyfit(px, py, 1)
            return Seg((s.a[0], k * s.a[0] + c), (s.b[0], k * s.b[0] + c))
        k, c = np.polyfit(py, px, 1)
        return Seg((k * s.a[1] + c, s.a[1]), (k * s.b[1] + c, s.b[1]))

    def trim(s: Seg) -> Seg:
        """Back in from each end to the last of the line's own ink."""
        n = int(s.length())
        on = [k for k in range(n + 1) if inked(_at(s, k / n))] if n else []
        return Seg(_at(s, on[0] / n), _at(s, on[-1] / n)) if on else s

    lines: list[Seg] = []
    for x0, y0, x1, y1 in found.reshape(-1, 4):
        dx, dy = abs(int(x1) - int(x0)), abs(int(y1) - int(y0))
        if math.degrees(math.atan2(min(dx, dy), max(dx, dy))) < 5:
            continue                            # a row or a column: the runs have it
        s = Seg((float(x0), float(y0)), (float(x1), float(y1)))
        if any(all(_dist_to_line(p, m) <= band for p in (s.a, s.b)) and
               -0.05 <= _reach(m, s.a) <= 1.05 and -0.05 <= _reach(m, s.b) <= 1.05 for m in lines):
            continue                            # a piece of a stroke already walked
        for _ in range(3):                      # a short find is a little off the true slope,
            u = ((s.b[0] - s.a[0]) / s.length(), (s.b[1] - s.a[1]) / s.length())
            s = Seg(walk(s.a, (-u[0], -u[1])), walk(s.b, u))
            s = refit(s)                        # so walk it, fit it to its ink, walk it again
        s = trim(s)
        n = int(s.length())
        if n < 2 * wire or sum(inked(_at(s, k / n)) for k in range(n + 1)) < 0.95 * (n + 1):
            continue
        lines = [m for m in lines if not (all(_dist_to_line(p, s) <= band for p in (m.a, m.b)))]
        lines.append(s)
    return lines


def _reach(s: Seg, p: Point) -> float:
    """Where along the line through ``s`` the foot of ``p`` falls, past its ends too."""
    (ax, ay), (bx, by) = s.a, s.b
    return ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / ((bx - ax) ** 2 + (by - ay) ** 2)


def _dist_to_line(p: Point, s: Seg) -> float:
    """How far ``p`` is from the infinite line through ``s``."""
    return abs(_side(p, s))


def _along(s: Seg, diagonals: list[Seg], band: float = 2.5) -> bool:
    """Is this run a step of one of the slanting wires, every pixel of it on one?"""
    n = max(1, int(s.length()))
    return all(any(_dist_to_seg(_at(s, k / n), d) <= band for d in diagonals) for k in range(n + 1))


def _group_runs(runs: dict[int, list[tuple[int, int]]], axis: int, max_width: int) -> list[Seg]:
    """Stack runs that overlap on neighbouring lines: one line of ink, one segment.

    Two runs are the same line when they largely coincide — not when one merely
    sits inside the other. A junction dot is wider than the wire through it, so
    its own short rows lie squarely under the wire's long ones; measuring the
    overlap against the shorter run alone would let the dot go on carrying the
    wire's group downwards until the stack looked too thick to be a line, and the
    wire were thrown away with it.
    """
    groups: list[list[tuple[int, int, int]]] = []       # (line, start, end)
    open_groups: list[list[tuple[int, int, int]]] = []
    for line in sorted(runs):
        current: list[list[tuple[int, int, int]]] = []
        for start, end in runs[line]:
            hit = None
            for g in open_groups:
                gl, gs, ge = g[-1]
                share = 0.6 * max(end - start, ge - gs)
                if gl == line - 1 and min(end, ge) - max(start, gs) >= share:
                    hit = g
                    break
            if hit is None:
                hit = []
                groups.append(hit)
            hit.append((line, start, end))
            current.append(hit)
        open_groups = current
    out = []
    for g in groups:
        if len(g) > max_width:                  # a filled shape, not a line
            continue
        mid = sum(l for l, _, _ in g) / len(g)
        start = min(s for _, s, _ in g)
        end = max(e for _, _, e in g)
        out.append(Seg((start, mid), (end, mid)) if axis == 0 else Seg((mid, start), (mid, end)))
    return out


def page_unit(path: Path) -> float:
    """The smallest distance this page can mean: a share of how wide it is drawn."""
    from PIL import Image

    with Image.open(path) as im:
        return max(2.0, im.width / PAGE_UNIT)


def raster_dots(work: Path) -> list[Dot]:
    """Junction dots ingest already found, if the work directory has them."""
    p = work / "dots.json"
    if not p.exists():
        return []
    return [Dot(d["x"], d["y"], d["d"] / 2) for d in json.loads(p.read_text())]


# -------------------------------------------------------------------- names

def read_text(work: Path) -> list[dict]:
    """What ingest read off the page: exact boxes for a vector page, OCR otherwise."""
    for name in ("text.json", "ocr.json"):
        p = work / name
        if p.exists():
            rows = json.loads(p.read_text())
            return [r for r in rows if r.get("text") and r.get("box")]
    return []


def _box_dist(p: Point, box) -> float:
    x0, y0, x1, y1 = box
    dx = max(x0 - p[0], 0.0, p[0] - x1)
    dy = max(y0 - p[1], 0.0, p[1] - y1)
    return math.hypot(dx, dy)


def _on_box(p: Point, box) -> Point:
    """The point of ``box`` nearest ``p`` — where a distance to it is measured from."""
    x0, y0, x1, y1 = box
    return (min(max(p[0], x0), x1), min(max(p[1], y0), y1))


def _paper(a: Point, b: Point, ink) -> float:
    """How many pixels between ``a`` and ``b`` the page leaves blank.

    Distance says how far apart two things are. This says how much of the way
    between them is empty, which is a different question, and the one to ask about
    a name and the wire it may belong to: what fills the gap is the symbol the name
    is written against.
    """
    steps = int(round(math.dist(a, b)))
    if steps == 0:
        return 0.0
    high, wide = ink.shape
    blank = 0
    for k in range(steps + 1):
        x = int(round(a[0] + (b[0] - a[0]) * k / steps))
        y = int(round(a[1] + (b[1] - a[1]) * k / steps))
        blank += not (0 <= y < high and 0 <= x < wide and ink[y, x])
    return float(blank)


def name_nets(cond: Conductors, texts: list[dict], names, on_pins: set[int],
              ink=None) -> dict[int, set[str]]:
    """Attach a rail's name to the conductor it is written against.

    A rail is drawn once per pin — a flag on a short stem, with its name next to
    it — so the drawing joins those pins by name rather than by wire. Only strings
    that are already a net name in the reading are looked for, and each goes to the
    nearest conductor that reaches a pin — squarely beside the name and close to
    it, since a flag is written against its own stem and a wire that merely passes
    within reach is neither.

    What near means is **how much blank paper lies between the name and the wire**,
    not how far apart the two are, wherever the page is there to say (``ink``, and
    a vector page is not). The flag stands between its name and its stem, and on a
    sheet that draws its symbols in another colour that flag is not ink the tracer
    keeps — so measuring by distance measures across a symbol taken out of the
    picture. 22359 writes every ``GND`` 26 px from the end of its own wire, with the
    ground bar and the red lead it hangs from filling 20 of them, and by distance an
    octave pot's corner 18 px off on bare paper won: the sheet's ground came back
    welded to the pot's top, and nine more of its flags reached no wire at all.
    Counting the paper puts the two 6 px and 18 px away, which is the way round the
    drawing means it.

    Where the wire is measured from is either end of it or the point of it nearest
    the name, whichever reads closer. A stem ends beside its own name, so for a flag
    the two are the same; but a name written along a wire sits *beside* it while that
    wire's end is off to one side, and scoring by the end alone lets a different
    net's corner that happens to sit under the text win by a pixel. Naming the wrong
    net is not a near miss here — the names are what join a rail's pins, so it welds
    two rails into one.
    """
    want = {n.upper(): n for n in names}
    ends = [(p, i) for i, s in enumerate(cond.segs) for p in (s.a, s.b) if cond.find(i) in on_pins]
    hits: list[tuple[str, int]] = []
    for t in texts:
        name = want.get(str(t["text"]).strip().upper())
        if not name:
            continue
        box = t["box"]
        x0, y0, x1, y1 = box
        c = ((x0 + x1) / 2, (y0 + y1) / 2)
        align = max(8.0, (y1 - y0) / 2)
        reach = max(25.0, 1.2 * (y1 - y0))
        best = None
        for p, i in ends:
            if min(abs(p[0] - c[0]), abs(p[1] - c[1])) > align:
                continue
            for q in (p, _nearest_on(c, cond.segs[i])):
                d = _box_dist(q, box)
                gap = d if ink is None else _paper(_on_box(q, box), q, ink)
                # two wires the same paper from the name: the nearer one is meant
                if gap <= reach and (best is None or (gap, d) < best[:2]):
                    best = (gap, d, i)
        if best:
            hits.append((name, best[2]))
    for name in {n for n, _ in hits}:            # one rail, however many flags draw it
        segs = [i for n, i in hits if n == name]
        for i in segs[1:]:
            cond.union(i, segs[0])
    found: dict[int, set[str]] = defaultdict(set)
    for name, i in hits:
        found[cond.find(i)].add(name)
    return found


# ------------------------------------------------------------------- check

@dataclass
class Crosscheck:
    pins: dict[str, tuple[str, int | None]] = field(default_factory=dict)
    splits: list[str] = field(default_factory=list)
    merges: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    misnamed: list[str] = field(default_factory=list)
    misplaced: list[str] = field(default_factory=list)
    undrawn: list[str] = field(default_factory=list)
    unclaimed: list[Point] = field(default_factory=list)
    source: str = ""
    traced: int = 0
    added: int = 0          # pins belonging to the parts in ``undrawn``

    @property
    def disagreements(self) -> int:
        return len(self.splits) + len(self.merges) + len(self.misnamed)

    @property
    def ok(self) -> bool:
        """No disagreement. A pin with no wire under it is a gap in the check, not a fault."""
        return self.disagreements == 0

    @property
    def checked(self) -> int:
        return len(self.pins) - len(self.unmatched) - self.added

    @property
    def drawn(self) -> int:
        """Pins the drawing draws: a part it does not draw has no wire to trace to,
        so it does not belong in the tally — the reading put it there on purpose."""
        return len(self.pins) - self.added

    def text(self) -> str:
        lines = [f"{self.source}: {self.traced} conductors, "
                 f"{self.checked} of {self.drawn} pins traced back to one"]
        for s in self.splits:
            lines.append(f"  split:   {s}")
        for m in self.merges:
            lines.append(f"  joined:  {m}")
        for m in self.misnamed:
            lines.append(f"  name:    {m}")
        for m in self.misplaced:
            lines.append(f"  place:   {m}")
        for u in self.undrawn:
            lines.append(f"  not drawn: {u}")
        for u in self.unmatched:
            lines.append(f"  no wire: {u}")
        if self.unclaimed:
            lines.append("  wire nothing in the reading reaches, at: "
                         + ", ".join(f"({int(x)},{int(y)})" for x, y in self.unclaimed[:12])
                         + (" ..." if len(self.unclaimed) > 12 else ""))
        lines.append("the reading agrees with the drawing" if not self.disagreements
                     else f"{self.disagreements} disagreement(s) with the drawing")
        return "\n".join(lines)


def _name_the_undrawn(rep: Crosscheck, cond: Conductors,
                      placed: dict[str, list[tuple[str, Point, int | None]]],
                      pin_tol: float, reach: float) -> None:
    """Say which parts the drawing does not draw, and take them off the tally.

    A reading holds more than the sheet does: the status LED a bypass board wants,
    the spare gate of a quad nobody used. Those pins have no wire to find and never
    will, and counting them as untraced buries the ones that are the reading's own
    fault — a pin put where the drawing writes the pin *number* rather than where
    its wire ends.

    What tells the two apart is the space the part stands in. Wherever a drawing
    draws a part it draws ink there: a lead, a plate, a stub to the box. So when no
    pin of a part is on a conductor **and** no conductor comes into the part's own
    space at all, the part is not on the sheet. The space is the box its pins make,
    grown by as far as a symbol's ink reaches past them (``BODY_REACH``) and by what
    a pin's own place is worth (``pin_tol``) — the two allowances the tracer already
    measures. A pin read a few pixels off still has its wire inside that box, so it
    stays on the list to be fixed, which is the whole point of telling them apart.
    """
    for key, pins in sorted(placed.items()):
        if any(root is not None for _, _, root in pins):
            continue
        xs = [at[0] for _, at, _ in pins]
        ys = [at[1] for _, at, _ in pins]
        space = _grow((min(xs), min(ys), max(xs), max(ys)), reach + pin_tol)
        if any(_inside_span(s, space) for s in cond.segs):
            continue
        ink = min(cond.nearest(at) for _, at, _ in pins)
        rep.undrawn.append(f"{key}: {len(pins)} pins, no wire in the space it would "
                           f"stand in, and none within {ink:.0f} px of it")
        rep.added += len(pins)
        gone = {f"{key}.{name}" for name, _, _ in pins}
        rep.unmatched = [u for u in rep.unmatched if u.split(" at ")[0] not in gone]


def _reaches(cond: Conductors, circuit, pin: str, root: int, tol: float, min_net: float,
             further: float = 2.5) -> bool:
    """Would this pin be on that conductor if its position were a few pixels out?"""
    at = _pin_at(circuit, pin)
    if at is None:
        return False
    for i, s in enumerate(cond.segs):
        if cond.find(i) == root and _dist_to_seg(at, s) <= tol * further:
            return True
    return False


def _centre(pts: list[Point]) -> Point | None:
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) if pts else None


def _away(centre: Point | None, at: Point) -> Point | None:
    """Which way the lead leaves the part, as a unit vector."""
    if centre is None:
        return None
    dx, dy = at[0] - centre[0], at[1] - centre[1]
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1 else None


def _pin_at(circuit, pin: str) -> Point | None:
    if pin.startswith("port:"):
        return next((p.at for p in circuit.ports if p.name == pin[5:]), None)
    key, _, name = pin.rpartition(".")
    for p in circuit.parts:
        if p.key == key:
            if name in p.pins:
                return p.pins[name].at
            return p.supply_at.get(name)
    return None


def crosscheck(circuit, cond: Conductors, named: dict[int, set[str]] | None = None,
               pin_tol: float = 5.0, min_net: float = 0.0, unclaimed_len: float = 120.0,
               reach: float = BODY_REACH * 2.0) -> Crosscheck:
    """Compare the drawing's own connectivity with the nets in ``circuit.json``.

    A signal net has to be one connected thing in the drawing and nothing else's. A
    rail is drawn as a flag at each pin instead, so its pins are islands on purpose:
    what holds a rail together is its name, and mixing two rails up shows as a pin
    sitting where the drawing writes the other name.
    """
    named = named or {}
    rep = Crosscheck(source=str(circuit.source.get("file") or circuit.name), traced=len(cond.segs))
    by_net: dict[str, set[int | None]] = defaultdict(set)
    by_traced: dict[int, set[str]] = defaultdict(set)
    where: dict[tuple[str, int | None], list[str]] = defaultdict(list)
    placed: dict[str, list[tuple[str, Point, int | None]]] = defaultdict(list)
    for part in circuit.parts:
        key = part.key
        drawn = [(name, pin.net, pin.at) for name, pin in part.pins.items()]
        drawn += [(name, net, part.supply_at.get(name)) for name, net in part.supply.items()]
        centre = _centre([at for _, _, at in drawn if at])
        for name, net, at in drawn:
            if not at or not net or net == "NC":
                continue
            root = cond.net_at(at, pin_tol, min_net, out=_away(centre, at))
            rep.pins[f"{key}.{name}"] = (net, root)
            by_net[net].add(root)
            where[(net, root)].append(f"{key}.{name}")
            placed[key].append((name, at, root))
            if root is None:
                # where the nearest ink is says what to do about it: a pin with a wire
                # a few pixels off is read in the wrong place, and the drawing says where
                rep.unmatched.append(f"{key}.{name} at {tuple(int(v) for v in at)}"
                                     f", nearest ink {cond.nearest(at):.0f} px away")
            else:
                by_traced[root].add(net)
    _name_the_undrawn(rep, cond, placed, pin_tol, reach)
    for port in circuit.ports:
        if not port.at:
            continue
        root = cond.net_at(port.at, pin_tol, min_net)
        rep.pins[f"port:{port.name}"] = (port.net, root)
        by_net[port.net].add(root)
        where[(port.net, root)].append(f"port:{port.name}")
        if root is not None:
            by_traced[root].add(port.net)

    for net, roots in sorted(by_net.items()):
        if circuit.net_kind(net) in ("power", "ground"):
            continue      # a rail is a flag per pin: islands on purpose, and the
        real = [r for r in roots if r is not None]      # name check is what holds it
        if len(real) <= 1:
            continue
        main = max(real, key=lambda r: len(where[(net, r)]))
        stray = [r for r in real if r is not None and r != main]
        off = [p for r in stray for p in where[(net, r)]
               if _reaches(cond, circuit, p, main, pin_tol, min_net)]
        if len(off) == sum(len(where[(net, r)]) for r in stray):
            rep.misplaced.append(f"{net} holds together if {', '.join(sorted(off))} sit a few pixels over")
            continue
        parts = " | ".join(", ".join(sorted(where[(net, r)])) for r in real)
        rep.splits.append(f"{net} is {len(real)} separate things in the drawing: {parts}")
    # a run of wire the reading knows nothing about: a part or a link left out
    claimed = set(by_traced) | set(named)
    for root, length in sorted(cond.net_length().items()):
        if root not in claimed and length >= unclaimed_len:
            here = [s for i, s in enumerate(cond.segs) if cond.find(i) == root]
            if here:
                seg = max(here, key=lambda s: s.length())
                rep.unclaimed.append((round(seg.a[0]), round(seg.a[1])))
    for root, nets in sorted(by_traced.items()):
        if len(nets) > 1:
            pins = sorted(p for n in nets for p in where[(n, root)])
            rep.merges.append(f"{', '.join(sorted(nets))} are one thing in the drawing: {', '.join(pins)}")
        wrote = named.get(root, set())
        for net in sorted(nets):
            if wrote and net not in wrote:
                rep.misnamed.append(f"{net} is where the drawing writes {', '.join(sorted(wrote))}: "
                                    + ", ".join(sorted(where[(net, root)])))
    return rep


def crosscheck_circuit(circuit, doc: Path | None = None) -> Crosscheck:
    """Trace the source ``circuit.json`` was read from, then check the reading against it.

    ``ingest.json`` beside the circuit says which source it was: a page of vectors
    is read as vectors, anything else from the pixels of ``source.png``.
    """
    work = circuit.path.parent if circuit.path else Path(".")
    meta = {}
    if (work / "ingest.json").exists():
        meta = json.loads((work / "ingest.json").read_text())
    pin_tol, min_net, touch, page = 5.0, 0.0, 2.0, 0.0
    ink = None                              # the page itself, where there are pixels of it
    if meta.get("kind") == "vector":
        pdf = doc or Path(meta.get("pdf") or meta.get("input"))
        if not pdf.exists():
            raise FileNotFoundError(f"the vector page needs its PDF; {pdf} is not there")
        segs, dots = vector_segments(pdf, int(meta["page"]), float(meta.get("dpi", 300)) / 72,
                                     int(meta.get("rotated", 0)))
        t_contacts = False
    else:
        # the positions in a reading are pixels of the upright source.png ingest wrote,
        # so that is the image to trace, whatever the reading names as its source
        image = work / "source.png"
        if not image.exists():
            image = circuit.source_image()
        if not image or not image.exists():
            raise FileNotFoundError(f"source image {image} not found")
        segs, dots = raster_segments(image)
        dots = dots or raster_dots(work)
        t_contacts = True
        # what a rail's name is written against is a symbol, not a wire, so telling
        # which wire it belongs to takes the whole page, not the colour the wires are in
        ink = page_ink(image)
        # a pixel reading of a pin's place is worth a few px; and what makes a
        # conductor worth matching a pin to goes with the page, since the lead
        # between two stacked parts on a small sheet is shorter than a stray mark
        # on a big one
        touch = page_unit(image)
        pin_tol, min_net = 11.0, max(6.0, MIN_NET * touch)
        # only a raster joins a T at all, so only a raster needs the hop rule: a
        # vector page never takes a crossing for a contact in the first place
        page = touch
    segs = drop_boxes(segs, circuit, touch=touch)      # a closed shape is an outline
    segs = drop_bodies(segs, circuit, touch=touch)
    cond = Conductors(segs, dots, t_contacts=t_contacts, page=page)
    on_pins = {r for r in (cond.net_at(at, pin_tol, min_net) for at in pin_points(circuit)) if r is not None}
    # only rails: a signal's name may be written anywhere, or read wrong off pixels,
    # and a rail is the only net whose pins the drawing joins by name alone
    rails = [n for n in circuit.all_net_names() if circuit.net_kind(n) in ("power", "ground")]
    named = name_nets(cond, read_text(work), rails, on_pins, ink)
    return crosscheck(circuit, cond, named, pin_tol=pin_tol, min_net=min_net,
                      reach=BODY_REACH * touch)
