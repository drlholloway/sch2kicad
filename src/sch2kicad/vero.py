"""Stripboard (vero) layouts: a reading of holes, cuts and links, turned into nets.

A vero layout is not a schematic. It says which hole every lead goes in, where
the copper strips are cut and where wire links jump between strips, and nothing
about how the circuit would be drawn. So the reading comes in two parts:

``board.json`` is what the layout shows, hole by hole (``D6``: row D, column 6).
The nets are not read at all: they fall out of the strips, the cuts and the
links here, so a strip is never traced by eye. ``circuit.json`` is the schematic
Claude draws from those nets, and it names its board (``"board": "board.json"``)
so verify can prove the two connect the same pins.

The layout image is read again for the two things a misreading of a board turns
on, cuts (a red square on a hole) and links (a black bar between holes), and the
counts the layout prints under the board ("17x16, 19 cuts, 8 links") are checked
against the reading.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .parts import TWO_TERMINAL, canonical_pin

SCHEMA = "sch2kicad/vero@1"

# parts with pins that may be left open: a spare pin alone on its strip is not a misread
_CHIPS = {"ic", "lm386", "opamp_single", "opamp_dual", "opamp_quad"}

_HOLE = re.compile(r"^([A-Z]{1,2})(\d{1,3})$")

# Off-board wires whose names say what they are. Anything else (a pot lug, "Gain 3")
# keeps its own name.
_STANDARD = {"input": "IN", "in": "IN", "output": "OUT", "out": "OUT",
             "ground": "GND", "gnd": "GND", "0v": "GND",
             "9v": "+9V", "+9v": "+9V", "9vdc": "+9V", "vcc": "+9V", "dc": "+9V", "power": "+9V"}

# Op-amp units on the DIP package, as circuit.json names them: unit → pin → number.
_DIP = {
    "opamp_single": {"": {"out": "6", "-": "2", "+": "3"}, "V+": "7", "V-": "4"},
    "opamp_dual": {"A": {"out": "1", "-": "2", "+": "3"}, "B": {"out": "7", "-": "6", "+": "5"},
                   "V+": "8", "V-": "4"},
    "opamp_quad": {"A": {"out": "1", "-": "2", "+": "3"}, "B": {"out": "7", "-": "6", "+": "5"},
                   "C": {"out": "8", "-": "9", "+": "10"}, "D": {"out": "14", "-": "13", "+": "12"},
                   "V+": "4", "V-": "11"},
}


def row_name(i: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def row_index(name: str) -> int:
    n = 0
    for ch in name:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def standard_name(wire: str) -> str:
    """The net an off-board wire's name stands for: 'Input' → IN, 'Gain 3' → GAIN_3."""
    w = wire.strip()
    s = _STANDARD.get(w.lower().replace(" ", ""))
    if s:
        return s
    return re.sub(r"[^A-Z0-9+]+", "_", w.upper()).strip("_") or "WIRE"


@dataclass(frozen=True)
class Hole:
    row: int
    col: int            # 1-based, as printed

    @property
    def name(self) -> str:
        return f"{row_name(self.row)}{self.col}"


@dataclass
class BoardPart:
    ref: str
    type: str
    data: dict                              # the part as written, less its pins
    holes: dict[str, Hole] = field(default_factory=dict)       # pin → hole
    wires: dict[str, str] = field(default_factory=dict)        # pin → off-board wire name
    nets: dict[str, str] = field(default_factory=dict)         # pin → net named outright
    nc: list[str] = field(default_factory=list)


@dataclass
class Board:
    name: str
    rows: int
    cols: int
    strips: str                             # "rows": the holes of a row are one strip
    cuts: list[tuple[Hole, Hole | None]]    # (hole, None) = cut at the hole; (a, b) = between
    links: list[tuple[Hole, Hole]]
    wires: list[tuple[str, Hole]]
    parts: list[BoardPart]
    stated: dict
    grid: dict
    source: dict
    notes: list[str]
    path: Path | None = None
    problems: list[str] = field(default_factory=list)

    def image(self) -> Path | None:
        img = self.source.get("image")
        if not img:
            return None
        p = Path(img)
        return p if p.is_absolute() or self.path is None else self.path.parent / p


# ------------------------------------------------------------------ loading

def _hole(text, where: str, b: Board) -> Hole | None:
    m = _HOLE.match(str(text).strip().upper())
    if not m:
        b.problems.append(f"{where}: {text!r} is not a hole (row letter + column, like D6)")
        return None
    h = Hole(row_index(m.group(1)), int(m.group(2)))
    if not (0 <= h.row < b.rows and 1 <= h.col <= b.cols):
        b.problems.append(f"{where}: {h.name} is off the {b.cols}x{b.rows} board")
        return None
    return h


def load_board(path: str | Path) -> Board:
    path = Path(path)
    data = json.loads(path.read_text())
    b = from_dict(data)
    b.path = path.resolve()
    return b


def from_dict(data: dict) -> Board:
    if data.get("schema") not in (SCHEMA, None):
        raise ValueError(f"unknown schema {data.get('schema')!r}; expected {SCHEMA}")
    bd = data.get("board") or {}
    if "rows" not in bd or "cols" not in bd:
        raise ValueError('board.json needs "board": {"rows": N, "cols": N}')
    strips = str(bd.get("strips", "rows"))
    if strips not in ("rows", "cols"):
        raise ValueError('"strips" is "rows" (strips run along a lettered row) or "cols"')
    b = Board(name=str(data.get("name", "board")), rows=int(bd["rows"]), cols=int(bd["cols"]),
              strips=strips, cuts=[], links=[], wires=[], parts=[],
              stated=dict(data.get("stated") or {}), grid=dict(data.get("grid") or {}),
              source=dict(data.get("source") or {}), notes=[str(n) for n in data.get("notes") or []])
    for c in data.get("cuts") or []:
        if "/" in str(c):
            x, y = str(c).split("/", 1)
            hx, hy = _hole(x, f"cut {c}", b), _hole(y, f"cut {c}", b)
            if hx and hy:
                if not _adjacent_on_strip(b, hx, hy):
                    b.problems.append(f"cut {c}: a cut between holes needs two neighbours on one strip")
                b.cuts.append((hx, hy))
        else:
            h = _hole(c, f"cut {c}", b)
            if h:
                b.cuts.append((h, None))
    for ln in data.get("links") or []:
        if not (isinstance(ln, (list, tuple)) and len(ln) == 2):
            b.problems.append(f"link {ln!r}: a link is [hole, hole]")
            continue
        x, y = _hole(ln[0], f"link {ln}", b), _hole(ln[1], f"link {ln}", b)
        if x and y:
            b.links.append((x, y))
    for w in data.get("wires") or []:
        h = _hole(w.get("at"), f"wire {w.get('name')}", b)
        if h:
            b.wires.append((str(w["name"]), h))
    wire_names = {n for n, _ in b.wires}
    for i, p in enumerate(data.get("parts") or []):
        where = f"parts[{i}] ({p.get('ref', '?')})"
        if "ref" not in p or "type" not in p:
            raise ValueError(f"{where}: every part needs ref and type")
        bp = BoardPart(ref=str(p["ref"]), type=str(p["type"]),
                       data={k: v for k, v in p.items() if k != "pins"})
        for pin, v in (p.get("pins") or {}).items():
            pin = str(pin)
            if isinstance(v, dict):
                bp.nets[pin] = str(v["net"])
            elif v is None or str(v).upper() in ("NC", "N/C"):
                bp.nc.append(pin)
            elif _HOLE.match(str(v).strip().upper()):
                h = _hole(v, f"{where}.{pin}", b)
                if h:
                    bp.holes[pin] = h
            elif str(v) in wire_names:
                bp.wires[pin] = str(v)
            else:
                b.problems.append(f"{where}.{pin}: {v!r} is neither a hole nor a wire on the board")
        b.parts.append(bp)
    return b


def _adjacent_on_strip(b: Board, x: Hole, y: Hole) -> bool:
    if b.strips == "rows":
        return x.row == y.row and abs(x.col - y.col) == 1
    return x.col == y.col and abs(x.row - y.row) == 1


# ------------------------------------------------------------------ nets

@dataclass
class Nets:
    """What the board connects. ``groups`` are the conductors: each is the holes,
    the pins and the wires on one piece of copper (with its links)."""
    names: dict[str, list[str]]             # net → ["R3.1", "Q2.B", ...]
    holes: dict[str, list[str]]             # net → holes on it
    wires: dict[str, list[str]]             # net → off-board wire names on it
    pin_net: dict[tuple[str, str], str]     # (ref, pin) → net
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    lone: set[str] = field(default_factory=set)     # nets that are one lead and nothing else


def derive(b: Board) -> Nets:
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    cut_holes = {h for h, other in b.cuts if other is None}
    cut_between = {frozenset((h, o)) for h, o in b.cuts if o is not None}
    for r in range(b.rows):
        for c in range(1, b.cols + 1):
            h = Hole(r, c)
            find(h)
            nxt = Hole(r, c + 1) if b.strips == "rows" else Hole(r + 1, c)
            if (b.strips == "rows" and c < b.cols) or (b.strips == "cols" and r + 1 < b.rows):
                if h not in cut_holes and nxt not in cut_holes and frozenset((h, nxt)) not in cut_between:
                    union(h, nxt)
    probs: list[str] = list(b.problems)
    warns: list[str] = []
    for x, y in b.links:
        for h in (x, y):
            if h in cut_holes:
                probs.append(f"link {x.name}-{y.name} lands on {h.name}, which is cut: it joins nothing there")
        union(x, y)
    for name, h in b.wires:
        if h in cut_holes:
            probs.append(f"wire {name} is soldered at {h.name}, which is cut")
        union(("wire", name), h)
    used: dict[Hole, list[str]] = {}
    for p in b.parts:
        for pin, h in p.holes.items():
            if h in cut_holes:
                probs.append(f"{p.ref}.{pin} is in {h.name}, which is cut: that lead joins nothing")
            used.setdefault(h, []).append(f"{p.ref}.{pin}")
            union(("pin", p.ref, pin), h)
        for pin, w in p.wires.items():
            union(("pin", p.ref, pin), ("wire", w))
        for pin, n in p.nets.items():
            union(("pin", p.ref, pin), ("net", n))
    for h, pins in used.items():
        if len(pins) > 1:
            warns.append(f"{h.name} holds {len(pins)} leads ({', '.join(pins)}): "
                         f"possible, but check it is not a misread hole")

    groups: dict = {}
    for k in list(parent):
        groups.setdefault(find(k), []).append(k)
    names: dict[str, list[str]] = {}
    holes: dict[str, list[str]] = {}
    wires: dict[str, list[str]] = {}
    pin_net: dict[tuple[str, str], str] = {}
    taken: set[str] = set()
    types = {p.ref: p.type for p in b.parts}
    unused: dict[str, list[str]] = {}
    notes: list[str] = []
    lone: set[str] = set()
    for members in groups.values():
        pins = sorted(f"{k[1]}.{k[2]}" for k in members if isinstance(k, tuple) and k[0] == "pin")
        ws = sorted(k[1] for k in members if isinstance(k, tuple) and k[0] == "wire")
        explicit = sorted(k[1] for k in members if isinstance(k, tuple) and k[0] == "net")
        hs = sorted((k for k in members if isinstance(k, Hole)), key=lambda h: (h.row, h.col))
        if not pins and not ws and not explicit:
            continue
        std = sorted({standard_name(w) for w in ws} | set(explicit))
        rails = [s for s in std if s in ("IN", "OUT", "GND", "+9V")]
        if len(rails) > 1:
            probs.append(f"{' and '.join(rails)} are joined on the board "
                         f"({', '.join(h.name for h in hs[:6])}): a short")
        if rails:
            name = rails[0]
        elif explicit:
            name = explicit[0]
        elif ws:
            name = standard_name(ws[0])
        else:
            first = next((h for h in hs if h in used), hs[0] if hs else None)
            name = f"N_{first.name}" if first else f"N_{pins[0].replace('.', '_')}"
        base, i = name, 2
        while name in taken:
            name, i = f"{base}_{i}", i + 1
        taken.add(name)
        names[name] = pins
        holes[name] = [h.name for h in hs]
        wires[name] = ws
        for k in members:
            if isinstance(k, tuple) and k[0] == "pin":
                pin_net[(k[1], k[2])] = name
        if len(pins) == 1 and not ws and not explicit:
            lone.add(name)
            ref, pin = pins[0].split(".", 1)
            if types.get(ref) in _CHIPS:
                unused.setdefault(ref, []).append(pin)
            else:
                warns.append(f"{pins[0]} is alone on its strip ({', '.join(h.name for h in hs)}): "
                             f"a lead that connects to nothing — misread hole, or a missing link?")
    # a chip's spare pins are normal; say them once, so a missing link still stands out
    for ref, pins in unused.items():
        nums = sorted(pins, key=lambda x: (len(x), x))
        notes.append(f"{ref} pins {', '.join(nums)} are alone on their strips: unused "
                     f"(check the datasheet lets them float, and the schematic marks them NC)")
    nets = Nets(names=names, holes=holes, wires=wires, pin_net=pin_net, problems=probs, warnings=warns,
                notes=notes, lone=lone)
    st = b.stated
    if "cuts" in st and int(st["cuts"]) != len(b.cuts):
        probs.append(f"the layout says {st['cuts']} cuts; the reading has {len(b.cuts)}")
    if "links" in st and int(st["links"]) != len(b.links):
        probs.append(f"the layout says {st['links']} links; the reading has {len(b.links)}")
    return nets


# ------------------------------------------------------------------ the image

@dataclass
class Grid:
    x0: float           # centre of hole A1
    y0: float
    dx: float           # column pitch
    dy: float           # row pitch

    def at(self, h: Hole) -> tuple[float, float]:
        return (self.x0 + (h.col - 1) * self.dx, self.y0 + h.row * self.dy)


def find_grid(b: Board, img) -> Grid | None:
    """Hole A1 and the pitch, from the board's copper. The strips are the one large
    run of a single copper colour; their ends give the board's extent, and the
    rows and columns the board states divide it."""
    import numpy as np
    if b.grid.get("A1") and b.grid.get("pitch"):
        (x0, y0), (dx, dy) = b.grid["A1"], b.grid["pitch"]
        return Grid(float(x0), float(y0), float(dx), float(dy))
    a = np.asarray(img.convert("RGB")).astype(int)
    if b.strips == "cols":
        a = a.transpose(1, 0, 2)
    # the copper colour: the commonest saturated warm colour
    r, g, bl = a[..., 0], a[..., 1], a[..., 2]
    warm = (r > 150) & (r - bl > 80) & (g > 90) & (g < 200)
    if warm.sum() < 1000:
        return None
    cols = a[warm]
    keys, counts = np.unique(cols // 4, axis=0, return_counts=True)
    copper = keys[counts.argmax()] * 4 + 2
    mask = (np.abs(a - copper).sum(axis=2) < 24)
    prof = mask.sum(axis=1)
    line = prof > 0.1 * prof.max()
    bands, y = [], 0
    while y < len(line):
        if line[y]:
            s = y
            while y < len(line) and line[y]:
                y += 1
            bands.append((s, y - 1))
        y += 1
    n = b.rows if b.strips == "rows" else b.cols
    bands = [x for x in bands if x[1] - x[0] >= 4]
    if len(bands) != n:
        return None
    centres = [(s + e) / 2 for s, e in bands]
    pitch = (centres[-1] - centres[0]) / (n - 1)
    xs = np.nonzero(mask[int(bands[0][0]):int(bands[-1][1]) + 1].any(axis=0))[0]
    m = b.cols if b.strips == "rows" else b.rows
    left, right = xs.min(), xs.max()
    along = (right - left + 1) / m
    first = left + along / 2
    if b.strips == "rows":
        return Grid(first, centres[0], along, pitch)
    return Grid(centres[0], first, pitch, along)


@dataclass
class Seen:
    grid: Grid | None
    cuts: set[Hole] = field(default_factory=set)
    links: list[tuple[Hole, Hole]] = field(default_factory=list)
    gaps: set[Hole] = field(default_factory=set)        # a bar between this hole and the next
    warnings: list[str] = field(default_factory=list)


def _red(px) -> bool:
    """A cut's red, also seen through a translucent part drawn over it. A translucent
    red body over bare copper is orange-red, not this."""
    r, g, b = int(px[0]), int(px[1]), int(px[2])
    return r > 150 and g < 60 and b < 60 and r - g > 110


def _dark(px) -> bool:
    return int(px[0]) + int(px[1]) + int(px[2]) < 120


def look(b: Board) -> Seen:
    """Read the cuts and links off the layout image, to check the reading by."""
    from PIL import Image
    import numpy as np
    src = b.image()
    if not src or not src.exists():
        return Seen(None, warnings=["no layout image beside board.json: cuts and links not checked"])
    with Image.open(src) as im:
        img = im.convert("RGB")
    g = find_grid(b, img)
    if g is None:
        return Seen(None, warnings=["could not find the hole grid in the image; give "
                                    '"grid": {"A1": [x, y], "pitch": [dx, dy]} in board.json'])
    a = np.asarray(img).astype(int)
    H, W = a.shape[:2]
    seen = Seen(g)
    p = min(g.dx, g.dy)

    def px(x, y):
        x, y = int(round(x)), int(round(y))
        return a[y, x] if 0 <= x < W and 0 <= y < H else None

    def ring(cx, cy, rad, n=24):
        import math
        pts = [px(cx + rad * math.cos(2 * math.pi * i / n), cy + rad * math.sin(2 * math.pi * i / n))
               for i in range(n)]
        return [q for q in pts if q is not None]

    # A cut is a red square a little smaller than the pitch round a hole that still
    # shows. A capacitor's red body is wider than a hole, so red on both sides of
    # the hole along the strip is a body, not a cut; a body also covers the hole.
    along = (1, 0) if b.strips == "rows" else (0, 1)
    for r in range(b.rows):
        for c in range(1, b.cols + 1):
            h = Hole(r, c)
            cx, cy = g.at(h)
            # a link drawn across the cut darkens part of the ring: leave that out
            inner = [q for q in ring(cx, cy, 0.28 * p) if not _dark(q)]
            centre = px(cx, cy)
            if len(inner) < 8 or centre is None or _red(centre):
                continue
            sides = [px(cx + s * 0.5 * p * along[0], cy + s * 0.5 * p * along[1]) for s in (-1, 1)]
            body = all(q is not None and _red(q) for q in sides)
            if sum(_red(q) for q in inner) / len(inner) > 0.6 and not body:
                seen.cuts.add(h)

    # A link is a black bar from hole to hole across the strips. Cut squares are
    # drawn over it, so red counts as bar too; the holes themselves are left out.
    def bridged(h: Hole, n: Hole) -> bool:
        (x1, y1), (x2, y2) = g.at(h), g.at(n)
        steps = 24
        dark = red = total = 0
        for i in range(steps + 1):
            t = i / steps
            x, y = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            if min(t, 1 - t) * p < 0.22 * p:
                continue
            q = px(x, y)
            if q is None:
                return False
            total += 1
            dark += _dark(q)
            red += _red(q)
        return total > 0 and (dark + red) >= 0.85 * total and dark >= 0.25 * total

    step = (lambda h: Hole(h.row + 1, h.col)) if b.strips == "rows" else (lambda h: Hole(h.row, h.col + 1))
    lines = [[Hole(r, c) for r in range(b.rows)] for c in range(1, b.cols + 1)] if b.strips == "rows" \
        else [[Hole(r, c) for c in range(1, b.cols + 1)] for r in range(b.rows)]
    for line in lines:
        for h, n in zip(line, line[1:]):
            if bridged(h, n):
                seen.gaps.add(h)
    for h in sorted(seen.gaps, key=lambda h: (h.col, h.row) if b.strips == "rows" else (h.row, h.col)):
        prev = Hole(h.row - 1, h.col) if b.strips == "rows" else Hole(h.row, h.col - 1)
        if prev in seen.gaps:
            continue
        end = h
        while end in seen.gaps:
            end = step(end)
        seen.links.append((h, end))
    return seen


def compare_seen(b: Board, seen: Seen) -> list[str]:
    """Warnings where the image and the reading differ about cuts and links."""
    if seen.grid is None:
        return list(seen.warnings)
    out = list(seen.warnings)
    read_cuts = {h for h, o in b.cuts if o is None}
    for h in sorted(read_cuts - seen.cuts, key=lambda h: (h.row, h.col)):
        out.append(f"cut at {h.name} is in the reading, but no cut mark shows there "
                   f"(hidden under a part, or a misread hole)")
    for h in sorted(seen.cuts - read_cuts, key=lambda h: (h.row, h.col)):
        out.append(f"the image shows a cut at {h.name} that the reading does not have")
    # A link may pass under a part, so the bar the image shows can come in pieces.
    # Each read link must show at both ends and stop there; a bar outside every
    # read link is one the reading does not have.
    rows = b.strips == "rows"

    def span(x: Hole, y: Hole) -> list[Hole] | None:
        if rows and x.col == y.col:
            lo, hi = sorted((x.row, y.row))
            return [Hole(r, x.col) for r in range(lo, hi)]
        if not rows and x.row == y.row:
            lo, hi = sorted((x.col, y.col))
            return [Hole(x.row, c) for c in range(lo, hi)]
        return None

    def prev_gap(h: Hole) -> Hole:
        return Hole(h.row - 1, h.col) if rows else Hole(h.row, h.col - 1)

    def next_hole(h: Hole) -> Hole:
        return Hole(h.row + 1, h.col) if rows else Hole(h.row, h.col + 1)

    spans = {(x, y): span(x, y) for x, y in b.links}
    covered = {gp for gaps in spans.values() if gaps for gp in gaps}
    for (x, y), gaps in spans.items():
        if not gaps:
            continue        # along a strip or slanting: nothing to look for
        name = f"{x.name}-{y.name}"
        ends = [gp for gp in (gaps[0], gaps[-1]) if gp not in seen.gaps]
        if ends:
            out.append(f"link {name} is in the reading, but no black bar shows at "
                       f"{'either end' if len(set(ends)) == 2 or len(gaps) == 1 else 'one end'} "
                       f"(under a part, or a misread end)")
        first, last = gaps[0], next_hole(gaps[-1])
        for beyond, end in ((prev_gap(first), first), (last, last)):
            if beyond in seen.gaps and beyond not in covered:
                out.append(f"the black bar of link {name} carries on past {end.name} in the image: "
                           f"is that end misread?")
    for x, y in seen.links:
        gaps = span(x, y) or []
        if gaps and not any(gp in covered for gp in gaps):
            out.append(f"the image shows a link {x.name}-{y.name} that the reading does not have")
    return out


def overlay(b: Board, nets: Nets, seen: Seen, out: Path) -> Path | None:
    """The reading drawn over the layout: every part's leads joined by a coloured
    line and labelled, cuts read as blue crosses, links read as green lines. What
    the image shows and the reading does not is ringed in magenta."""
    from PIL import Image, ImageDraw
    src = b.image()
    if seen.grid is None or not src or not src.exists():
        return None
    g = seen.grid
    with Image.open(src) as im:
        img = im.convert("RGB")
    d = ImageDraw.Draw(img)
    p = min(g.dx, g.dy)
    palette = [(0, 90, 255), (0, 160, 0), (200, 0, 200), (0, 150, 160), (230, 120, 0), (120, 60, 200)]
    for i, part in enumerate(b.parts):
        pts = [g.at(h) for h in part.holes.values()]
        if not pts:
            continue
        col = palette[i % len(palette)]
        for x, y in pts[1:]:
            d.line([pts[0], (x, y)], fill=col, width=2)
        for (pin, h), (x, y) in zip(part.holes.items(), pts):
            d.ellipse([x - 4, y - 4, x + 4, y + 4], outline=col, width=2)
            d.text((x + 5, y - 12), f"{pin}", fill=col)
        cx = sum(x for x, _ in pts) / len(pts)
        cy = sum(y for _, y in pts) / len(pts)
        d.text((cx + 6, cy), part.ref, fill=(0, 0, 0), stroke_width=2, stroke_fill=(255, 255, 255))
    for h, o in b.cuts:
        if o is None:
            x, y = g.at(h)
            r = 0.3 * p
            d.line([x - r, y - r, x + r, y + r], fill=(0, 60, 255), width=3)
            d.line([x - r, y + r, x + r, y - r], fill=(0, 60, 255), width=3)
    for x, y in b.links:
        d.line([g.at(x), g.at(y)], fill=(0, 200, 0), width=3)
    read_cuts = {h for h, o in b.cuts if o is None}
    for h in seen.cuts - read_cuts:
        x, y = g.at(h)
        d.ellipse([x - 0.5 * p, y - 0.5 * p, x + 0.5 * p, y + 0.5 * p], outline=(255, 0, 255), width=3)
    for name, h in b.wires:
        x, y = g.at(h)
        d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(255, 0, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


# ------------------------------------------------------------------ the schematic

def _keys_board(b: Board, nets: Nets) -> dict[str, Counter]:
    """Each net as a multiset of pin keys. The two ends of a resistor or a plain
    capacitor are one key, so a schematic may draw either end either way round;
    a pot's lugs, a diode's ends and a transistor's leads stay apart."""
    types = {p.ref: p.type for p in b.parts}
    out: dict[str, Counter] = {}
    for name, pins in nets.names.items():
        cnt = Counter()
        for pr in pins:
            ref, pin = pr.split(".", 1)
            cnt[_key(types[ref], ref, pin)] += 1
        if name in _named_wires(b):
            cnt[f"name:{name}"] += 1
        out[name] = cnt
    return out


def _named_wires(b: Board) -> set[str]:
    """Off-board connections that are not a part's lug: the jacks and the supply,
    whether a wire on the board or a lug named straight onto one ("Volume 2 to
    Output"). The schematic must carry these by name (a port or a rail)."""
    lugs = {w for p in b.parts for w in p.wires.values()}
    named = {standard_name(n) for n, _ in b.wires if n not in lugs}
    return named | {n for p in b.parts for n in p.nets.values() if n in ("IN", "OUT", "GND", "+9V")}


def _key(ptype: str, ref: str, pin: str) -> str:
    if ptype in TWO_TERMINAL:
        return f"{ref}.~"
    return f"{ref}.{canonical_pin(ptype, pin)}"


def _keys_circuit(c, named: set[str]) -> dict[str, Counter]:
    out: dict[str, Counter] = {}

    def add(net, key):
        if net:
            out.setdefault(net, Counter())[key] += 1

    for p in c.parts:
        if p.type in _DIP:
            dip = _DIP[p.type]
            for pin, ref in p.pins.items():
                num = dip.get(p.unit or "", {}).get(canonical_pin(p.type, pin))
                add(ref.net, f"{p.ref}.{num or canonical_pin(p.type, pin)}")
            for k, n in p.supply.items():
                rail = canonical_pin(p.type, k)
                add(n, f"{p.ref}.{dip.get(rail, rail)}")
            continue
        for pin, ref in p.pins.items():
            add(ref.net, _key(p.type, p.ref, pin))
        for k, n in p.supply.items():
            add(n, f"{p.ref}.{k}")
    for name in list(out):
        if name in named:
            out[name][f"name:{name}"] += 1
    for port in c.ports:
        n = standard_name(port.name)
        if n in named and port.net in out and f"name:{n}" not in out[port.net]:
            out[port.net][f"name:{n}"] += 1
    return out


def check_circuit(c, board_path: Path) -> "BoardCheck":
    """Does the schematic connect exactly the pins the board connects?"""
    b = load_board(board_path)
    nets = derive(b)
    named = _named_wires(b)
    bk = {n: k for n, k in _keys_board(b, nets).items() if sum(k.values()) > 1}
    ck = {n: k for n, k in _keys_circuit(c, named).items() if sum(k.values()) > 1}
    chk = BoardCheck(board=b.name, pins=sum(len(v) for v in nets.names.values()),
                     problems=list(nets.problems))
    unmatched_c = {n: k for n, k in ck.items()}
    for bn, k in sorted(bk.items()):
        hit = next((cn for cn, ck_ in unmatched_c.items() if ck_ == k), None)
        if hit:
            del unmatched_c[hit]
            continue
        # the circuit net sharing the most pins says what went wrong
        best = max(unmatched_c.items(), key=lambda kv: sum((kv[1] & k).values()), default=None)
        if best and sum((best[1] & k).values()):
            missing = sorted((k - best[1]).elements())
            extra = sorted((best[1] - k).elements())
            chk.diffs.append(f"board net {bn} ({', '.join(nets.holes[bn][:4])}...) is schematic net "
                             f"{best[0]}"
                             + (f" without {', '.join(missing)}" if missing else "")
                             + (f" plus {', '.join(extra)}" if extra else ""))
        else:
            chk.diffs.append(f"board net {bn} ({', '.join(sorted(k))}) has nothing like it in the schematic")
    board_refs = {p.ref for p in b.parts}
    circ_refs = {p.ref for p in c.parts}
    for r in sorted(board_refs - circ_refs):
        chk.diffs.append(f"{r} is on the board but not in the schematic")
    for r in sorted(circ_refs - board_refs):
        chk.diffs.append(f"{r} is in the schematic but not on the board")
    for p in b.parts:
        cp = next((q for q in c.parts if q.ref == p.ref), None)
        if cp is None:
            continue
        for f in ("value", "part"):
            bv, cv = str(p.data.get(f, "")), getattr(cp, f)
            if bv and bv != cv:
                chk.diffs.append(f"{p.ref}: the board says {f} {bv!r}, the schematic {cv!r}")
    return chk


@dataclass
class BoardCheck:
    board: str
    pins: int
    problems: list[str] = field(default_factory=list)
    diffs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.diffs


# ------------------------------------------------------------------ skeleton

def skeleton(b: Board, nets: Nets) -> dict:
    """A circuit.json with every part, pin and net from the board and no positions:
    the schematic is Claude's to draw. Op-amps come back as units the way
    circuit.json writes them."""
    parts = []
    for p in b.parts:
        base = {k: v for k, v in p.data.items() if k not in ("ref", "type")}

        def net(pin):
            n = nets.pin_net.get((p.ref, pin))
            if pin in p.nc or n is None:
                return "NC"
            # alone on its strip: connected to nothing, which a schematic draws as NC
            return "NC" if n in nets.lone else n

        if p.type in _DIP:
            dip = _DIP[p.type]
            for unit, m in dip.items():
                if unit in ("V+", "V-"):
                    continue
                e = {"ref": p.ref, "type": p.type, **base}
                if unit:
                    e["unit"] = unit
                e["pins"] = {sem: net(num) for sem, num in m.items()}
                if unit in ("", "A"):
                    e["supply"] = {"V+": net(dip["V+"]), "V-": net(dip["V-"])}
                parts.append(e)
            continue
        names = list(p.holes) + list(p.wires) + list(p.nets) + p.nc
        parts.append({"ref": p.ref, "type": p.type, **base, "pins": {pin: net(pin) for pin in names}})
    used = {n for n in nets.names}
    kinds = {}
    if "GND" in used:
        kinds["GND"] = {"kind": "ground"}
    if "+9V" in used:
        kinds["+9V"] = {"kind": "power"}
    ports = [{"name": n, "net": n} for n in sorted(_named_wires(b)) if n != "GND"]
    src = dict(b.source)
    return {"schema": "sch2kicad/circuit@1", "name": b.name, "source": src,
            "board": b.path.name if b.path else "board.json",
            "title_block": {"title": b.name}, "nets": kinds, "parts": parts, "ports": ports,
            "texts": [], "notes": list(b.notes), "blocks": []}


def report(b: Board, nets: Nets, seen: Seen) -> tuple[str, bool]:
    lines = [f"{b.name}: {b.cols}x{b.rows} board, {len(b.parts)} parts, "
             f"{len(b.cuts)} cuts, {len(b.links)} links, {len(nets.names)} nets"]
    if seen.grid:
        g = seen.grid
        lines.append(f"  grid:  A1 at ({g.x0:.0f},{g.y0:.0f}), pitch {g.dx:.1f} x {g.dy:.1f} px")
    for name in sorted(nets.names, key=lambda n: (not n.startswith(("IN", "OUT", "GND", "+9V")), n)):
        w = nets.wires.get(name)
        lines.append(f"  {name:10} {' '.join(nets.names[name])}" + (f"   [wire: {', '.join(w)}]" if w else ""))
    image = compare_seen(b, seen)
    for p in nets.problems:
        lines.append(f"  problem: {p}")
    for w in nets.warnings + image:
        lines.append(f"  warning: {w}")
    for n in nets.notes:
        lines.append(f"  note: {n}")
    ok = not nets.problems
    lines.append("BOARD OK" if ok and not image else ("BOARD OK, check the warnings" if ok else "BOARD HAS PROBLEMS"))
    return "\n".join(lines), ok
