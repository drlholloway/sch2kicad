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
  middle is a real T-junction, because a raster gives no better evidence.
* **Neither** counts ink inside a part: the space a part's own pins span is its
  body, and a body is not a wire. Dropping it is what keeps an op-amp's inputs
  apart, and it can only split a net that should be whole — never join two that
  should be apart.

A junction dot joins whatever it sits on, crossing or not.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

Point = tuple[float, float]
BEHIND_PENALTY = 5.0   # px: how much further a wire may be, to be one that leaves the part


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


class Conductors:
    """Segments and dots, joined into nets."""

    def __init__(self, segs: list[Seg], dots: list[Dot], t_contacts: bool, tol: float = 2.0):
        self.segs = segs
        self.dots = dots
        self.tol = tol
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
        tol = self.tol
        cell = max(tol * 2, 4.0)
        near: dict[tuple[int, int], set[int]] = defaultdict(set)
        for i, s in enumerate(self.segs):
            self.find(i)
            x0, x1 = sorted((s.a[0], s.b[0]))
            y0, y1 = sorted((s.a[1], s.b[1]))
            for gx in range(int((x0 - tol) // cell), int((x1 + tol) // cell) + 1):
                for gy in range(int((y0 - tol) // cell), int((y1 + tol) // cell) + 1):
                    near[(gx, gy)].add(i)
        for members in near.values():
            for i in members:
                for j in members:
                    if i < j and self._touch(self.segs[i], self.segs[j], t_contacts):
                        self.union(i, j)
        # a dot joins everything it sits on, crossing or not
        for d in self.dots:
            on = [i for i, s in enumerate(self.segs) if _dist_to_seg((d.x, d.y), s) <= d.r + tol]
            for i in on[1:]:
                self.union(on[0], i)

    def _touch(self, s: Seg, t: Seg, t_contacts: bool) -> bool:
        tol = self.tol
        for p in (s.a, s.b):
            for q in (t.a, t.b):
                if math.dist(p, q) <= tol:
                    return True
        if not t_contacts:
            return False
        return (any(_dist_to_seg(p, t) <= tol for p in (s.a, s.b))
                or any(_dist_to_seg(q, s) <= tol for q in (t.a, t.b)))

    # ............................................................. lookup
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
        little further off. ``out`` is the way the pin's lead leaves its part, and
        the wire is looked for that way: the other plate of a capacitor is as close
        to the pin as its own, and belongs to the other net.
        """
        lengths = self.net_length()
        best: tuple[float, float, int] | None = None
        for i, s in enumerate(self.segs):
            if lengths[self.find(i)] < min_len:
                continue
            q = _nearest_on(p, s)
            d = math.dist(p, q)
            if d > tol:
                continue
            # a pin's own place is read to within a few pixels, so a wire just behind
            # it is still its wire — but one that leaves the part is the better answer
            behind = out is not None and (q[0] - p[0]) * out[0] + (q[1] - p[1]) * out[1] <= 0
            score = d + (BEHIND_PENALTY if behind else 0.0)
            if best is None or score < best[0]:
                best = (score, d, i)
        return self.find(best[2]) if best else None


# ------------------------------------------------------------------ bodies

def part_boxes(circuit, margin: float = 2.0) -> list[tuple[float, float, float, float]]:
    """The space each part's pins span: its body, plus the leads that reach it."""
    boxes = []
    for p in circuit.parts:
        pts = [pin.at for pin in p.pins.values() if pin.at]
        pts += [at for at in p.supply_at.values() if at]
        if len(pts) < 2:
            continue
        xs = [q[0] for q in pts]
        ys = [q[1] for q in pts]
        boxes.append((min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin))
    return boxes


def _in_boxes(p: Point, boxes) -> bool:
    return any(_in_box(p, b) for b in boxes)


def _in_box(p: Point, box) -> bool:
    x0, y0, x1, y1 = box
    return x0 <= p[0] <= x1 and y0 <= p[1] <= y1


def drop_bodies(segs: list[Seg], boxes, pins: list[Point], keep_tol: float = 2.5) -> list[Seg]:
    """Drop what a part draws of itself, keep what reaches it.

    A segment counts as the part's own when its middle lies inside the space the
    part's pins span — an outline, a zigzag, a plate. A wire that ends on a pin
    runs away from the part, so its middle is outside and it survives whole, right
    up to the point the pin sits on. A lead is kept too, by the one thing that
    tells it from an outline: it ends where a pin is.
    """
    keep = {(round(x / keep_tol), round(y / keep_tol)) for x, y in pins}

    def on_pin(p: Point) -> bool:
        gx, gy = round(p[0] / keep_tol), round(p[1] / keep_tol)
        return any((gx + dx, gy + dy) in keep for dx in (-1, 0, 1) for dy in (-1, 0, 1))

    out = []
    for s in segs:
        mid = ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2)
        # inside one part from end to end, or through the middle of one: its own
        inside = _in_boxes(mid, boxes) or any(_in_box(s.a, b) and _in_box(s.b, b) for b in boxes)
        if inside and not (on_pin(s.a) or on_pin(s.b)):
            continue
        out.append(s)
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


def vector_segments(pdf: Path, page_no: int, scale: float) -> tuple[list[Seg], list[Dot]]:
    """Stroked paths as segments, filled circles as junction dots, in image pixels."""
    import pymupdf

    doc = pymupdf.open(pdf)
    page = doc[page_no - 1]
    segs: list[Seg] = []
    dots: list[Dot] = []
    for d in page.get_drawings():
        items = d["items"]
        if "f" in (d["type"] or "") and items and all(it[0] in ("c", "qu") for it in items):
            r = d["rect"]                       # a filled blob: a junction dot
            w, h = (r.x1 - r.x0) * scale, (r.y1 - r.y0) * scale
            if w <= 12 and h <= 12 and abs(w - h) <= 3:
                dots.append(Dot((r.x0 + r.x1) / 2 * scale, (r.y0 + r.y1) / 2 * scale, max(w, h) / 2))
            continue
        if "s" not in (d["type"] or "") or _closed(items):
            continue                            # a closed path is an outline, not a wire
        for it in items:
            pts = []
            if it[0] == "l":
                pts = [it[1], it[2]]
            elif it[0] in ("c", "qu"):          # a curve joins its ends like a chord
                pts = [it[1], it[-1]]
            for u, v in zip(pts, pts[1:]):
                a = (u.x * scale, u.y * scale)
                b = (v.x * scale, v.y * scale)
                if a != b:
                    segs.append(Seg(a, b))
    doc.close()
    return segs, dots


# ------------------------------------------------------------------ raster

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
        min_len = max(5, round(rgb.shape[1] / 320))
    # ink is dark and colourless. A thin wire comes out grey once the page has been
    # scaled, so the level is generous; what keeps a symbol out is its colour, since
    # Parasit draws wires in black and everything else in blue
    hi, lo = rgb.max(axis=2), rgb.min(axis=2)
    dark = (hi <= ink_level) & (hi - lo <= 60)
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
    return segs, []


def _group_runs(runs: dict[int, list[tuple[int, int]]], axis: int, max_width: int) -> list[Seg]:
    """Stack runs that overlap on neighbouring lines: one line of ink, one segment."""
    groups: list[list[tuple[int, int, int]]] = []       # (line, start, end)
    open_groups: list[list[tuple[int, int, int]]] = []
    for line in sorted(runs):
        current: list[list[tuple[int, int, int]]] = []
        for start, end in runs[line]:
            hit = None
            for g in open_groups:
                gl, gs, ge = g[-1]
                if gl == line - 1 and min(end, ge) - max(start, gs) >= 0.6 * (end - start):
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


def name_nets(cond: Conductors, texts: list[dict], names, on_pins: set[int]) -> dict[int, set[str]]:
    """Attach a rail's name to the conductor it is written beside.

    A rail is drawn once per pin — a flag on a short stem, with its name next to
    it — so the drawing joins those pins by name rather than by wire. Only strings
    that are already a net name in the reading are looked for, and each goes to the
    nearest conductor that reaches a pin — squarely beside the name and close to
    it, since a flag is written against its own stem and a wire that merely passes
    within reach is neither.
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
            d = _box_dist(p, box)
            if d <= reach and (best is None or d < best[0]):
                best = (d, i)
        if best:
            hits.append((name, best[1]))
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
    unclaimed: list[Point] = field(default_factory=list)
    source: str = ""
    traced: int = 0

    @property
    def disagreements(self) -> int:
        return len(self.splits) + len(self.merges) + len(self.misnamed)

    @property
    def ok(self) -> bool:
        """No disagreement. A pin with no wire under it is a gap in the check, not a fault."""
        return self.disagreements == 0

    @property
    def checked(self) -> int:
        return len(self.pins) - len(self.unmatched)

    def text(self) -> str:
        lines = [f"{self.source}: {self.traced} conductors, "
                 f"{self.checked} of {len(self.pins)} pins traced back to one"]
        for s in self.splits:
            lines.append(f"  split:   {s}")
        for m in self.merges:
            lines.append(f"  joined:  {m}")
        for m in self.misnamed:
            lines.append(f"  name:    {m}")
        for m in self.misplaced:
            lines.append(f"  place:   {m}")
        for u in self.unmatched:
            lines.append(f"  no wire: {u}")
        if self.unclaimed:
            lines.append("  wire nothing in the reading reaches, at: "
                         + ", ".join(f"({int(x)},{int(y)})" for x, y in self.unclaimed[:12])
                         + (" ..." if len(self.unclaimed) > 12 else ""))
        lines.append("the reading agrees with the drawing" if not self.disagreements
                     else f"{self.disagreements} disagreement(s) with the drawing")
        return "\n".join(lines)


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
               pin_tol: float = 5.0, min_net: float = 0.0, unclaimed_len: float = 120.0) -> Crosscheck:
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
            if root is None:
                rep.unmatched.append(f"{key}.{name} at {tuple(int(v) for v in at)}")
            else:
                by_traced[root].add(net)
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
    boxes = part_boxes(circuit)
    pin_tol, min_net = 5.0, 0.0
    if meta.get("kind") == "vector":
        pdf = doc or Path(meta.get("pdf") or meta.get("input"))
        if not pdf.exists():
            raise FileNotFoundError(f"the vector page needs its PDF; {pdf} is not there")
        segs, dots = vector_segments(pdf, int(meta["page"]), float(meta.get("dpi", 300)) / 72)
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
        pin_tol, min_net = 11.0, 15.0   # a pixel reading of a pin's place is worth a few px
    segs = [s for s in drop_bodies(segs, boxes, pin_points(circuit)) if s.length() >= 1.0]
    cond = Conductors(segs, dots, t_contacts=t_contacts)
    on_pins = {r for r in (cond.net_at(at, pin_tol, min_net) for at in pin_points(circuit)) if r is not None}
    # only rails: a signal's name may be written anywhere, or read wrong off pixels,
    # and a rail is the only net whose pins the drawing joins by name alone
    rails = [n for n in circuit.all_net_names() if circuit.net_kind(n) in ("power", "ground")]
    named = name_nets(cond, read_text(work), rails, on_pins)
    return crosscheck(circuit, cond, named, pin_tol=pin_tol, min_net=min_net)
