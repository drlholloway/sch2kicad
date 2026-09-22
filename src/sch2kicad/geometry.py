"""Symbol transforms, orientation fitting and placement on the KiCad grid.

KiCad stores a placed symbol as ``(at x y angle)`` plus an optional
``(mirror x|y)``. Internally that is a 2×2 matrix applied to library
coordinates (Y up) to get sheet coordinates (Y down):

    M = Mirror × Rotation^n × [[1, 0], [0, -1]]

where each later step is applied on the left (sheet space), exactly as
``SCH_SYMBOL::SetOrientation`` composes them.
"""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass, field

from .parts import Resolved
from .symlib import LibSymbol, Pin

GRID = 1.27

Mat = tuple[int, int, int, int]  # (x1, y1, x2, y2): x' = x1*x + y1*y ; y' = x2*x + y2*y

_M0: Mat = (1, 0, 0, -1)
_R_CCW: Mat = (0, 1, -1, 0)
_MIR_X: Mat = (1, 0, 0, -1)   # flips sheet Y (mirror about the X axis)
_MIR_Y: Mat = (-1, 0, 0, 1)   # flips sheet X


def _mul(t: Mat, m: Mat) -> Mat:
    """t × m (apply m first, then t)."""
    a, b, c, d = t
    e, f, g, h = m
    return (a * e + b * g, a * f + b * h, c * e + d * g, c * f + d * h)


def matrix(angle: int, mirror: str | None = None) -> Mat:
    m = _M0
    for _ in range((angle // 90) % 4):
        m = _mul(_R_CCW, m)
    if mirror == "x":
        m = _mul(_MIR_X, m)
    elif mirror == "y":
        m = _mul(_MIR_Y, m)
    return m


def apply(m: Mat, x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[1] * y, m[2] * x + m[3] * y)


@dataclass(frozen=True)
class Orient:
    angle: int
    mirror: str | None

    @property
    def m(self) -> Mat:
        return matrix(self.angle, self.mirror)


def _unique_orients() -> list[Orient]:
    seen: dict[Mat, Orient] = {}
    for mirror in (None, "x", "y"):
        for angle in (0, 90, 180, 270):
            o = Orient(angle, mirror)
            seen.setdefault(o.m, o)
    return list(seen.values())


ORIENTS = _unique_orients()   # 8 distinct placements


def snap(v: float, g: float = GRID) -> float:
    return round(round(v / g) * g, 4)


def pin_world(origin: tuple[float, float], o: Orient, pin: Pin) -> tuple[float, float]:
    dx, dy = apply(o.m, pin.x, pin.y)
    return (round(origin[0] + dx, 4), round(origin[1] + dy, 4))


def pin_outward(o: Orient, pin: Pin) -> tuple[int, int]:
    ox, oy = pin.outward()
    x, y = apply(o.m, ox, oy)
    return (int(round(x)), int(round(y)))


def body_box(sym: LibSymbol, unit: int, origin: tuple[float, float], o: Orient) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = sym.body_bbox(unit)
    pts = [apply(o.m, x, y) for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))]
    xs = [origin[0] + p[0] for p in pts]
    ys = [origin[1] + p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


# ------------------------------------------------------------- orientation fit

@dataclass
class FitResult:
    orient: Orient
    pinmap: dict[str, str]
    score: float


def fit_orientation(res: Resolved) -> FitResult:
    """Choose rotation/mirror (and swappable-pin assignment) matching the source drawing."""
    sym = res.symbol
    lib_pins = {p.number: p for p in sym.pins_for(res.unit)}
    sem = _semantic_pins(res)
    observed = {s: pin.at for s, pin in sem.items() if pin.at is not None and s in res.pinmap}
    # unconnected pins say little about orientation (other drawings put them anywhere)
    wired = {s: at for s, at in observed.items() if sem[s].net is not None}
    if len(wired) >= 3 or (len(wired) >= 2 and len(wired) < len(observed)):
        observed = wired
    variants = [dict(res.pinmap)]
    for group in res.swappable:
        present = [g for g in group if g in res.pinmap]
        new = []
        for v in variants:
            for perm in itertools.permutations([v[g] for g in present]):
                w = dict(v)
                w.update(dict(zip(present, perm)))
                new.append(w)
        variants = new or variants
    if len(observed) < 2:
        return FitResult(Orient(0, None), variants[0], 0.0)
    o_pts = list(observed.values())
    ocx = sum(p[0] for p in o_pts) / len(o_pts)
    ocy = sum(p[1] for p in o_pts) / len(o_pts)
    best: FitResult | None = None
    for vi, pmap in enumerate(variants):
        if any(pmap.get(s) not in lib_pins for s in observed):
            continue
        for oi, orient in enumerate(ORIENTS):
            m_pts = {s: apply(orient.m, lib_pins[pmap[s]].x, lib_pins[pmap[s]].y) for s in observed}
            mcx = sum(p[0] for p in m_pts.values()) / len(m_pts)
            mcy = sum(p[1] for p in m_pts.values()) / len(m_pts)
            score = 0.0
            for s, (ox, oy) in observed.items():
                ax, ay = ox - ocx, oy - ocy
                bx, by = m_pts[s][0] - mcx, m_pts[s][1] - mcy
                na, nb = math.hypot(ax, ay), math.hypot(bx, by)
                if na < 1e-9 or nb < 1e-9:
                    continue
                score += (ax * bx + ay * by) / (na * nb)
            # prefer plain rotations, then the unswapped pin assignment
            score -= 1e-3 * (orient.mirror is not None) + 1e-4 * (orient.angle // 90) + 1e-5 * vi + 1e-6 * oi
            if best is None or score > best.score:
                best = FitResult(orient, pmap, score)
    return best or FitResult(Orient(0, None), variants[0], 0.0)


def _semantic_pins(res: Resolved):
    from .parts import canonical_pin
    return {canonical_pin(res.part.type, k): v for k, v in res.part.pins.items()}


# ------------------------------------------------------------------ placement

@dataclass
class Placed:
    res: Resolved
    orient: Orient
    pinmap: dict[str, str]
    origin: tuple[float, float] = (0.0, 0.0)
    src_center: tuple[float, float] | None = None
    uuid: str = ""
    moved: bool = False

    @property
    def sym(self) -> LibSymbol:
        return self.res.symbol

    @property
    def key(self) -> str:
        return self.res.ref + ("#" + str(self.res.unit))

    def pins(self) -> list[Pin]:
        return self.sym.pins_for(self.res.unit)

    def pin_pos(self, number: str) -> tuple[float, float]:
        return pin_world(self.origin, self.orient, self.sym.pin(number, self.res.unit))

    def pin_dir(self, number: str) -> tuple[int, int]:
        return pin_outward(self.orient, self.sym.pin(number, self.res.unit))

    def box(self) -> tuple[float, float, float, float]:
        return body_box(self.sym, self.res.unit, self.origin, self.orient)

    def extent(self, margin: float = 0.0) -> tuple[float, float, float, float]:
        """Body plus pins."""
        x0, y0, x1, y1 = self.box()
        for p in self.pins():
            px, py = pin_world(self.origin, self.orient, p)
            x0, y0, x1, y1 = min(x0, px), min(y0, py), max(x1, px), max(y1, py)
        return (x0 - margin, y0 - margin, x1 + margin, y1 + margin)

    def net_of_pin(self, number: str, nets: dict) -> str | None:
        return nets.get((self.res.ref, number))


def estimate_scale(placed: list[Placed]) -> float:
    """mm per source pixel: KiCad symbol pin spans over drawn pin spans, median."""
    ratios = []
    for pl in placed:
        obs = {s: p.at for s, p in _semantic_pins(pl.res).items() if p.at is not None and s in pl.pinmap}
        if len(obs) < 2:
            continue
        best = None
        for a, b in itertools.combinations(obs, 2):
            d_src = math.dist(obs[a], obs[b])
            if d_src < 3:
                continue
            pa = pl.sym.pin(pl.pinmap[a], pl.res.unit)
            pb = pl.sym.pin(pl.pinmap[b], pl.res.unit)
            d_lib = math.dist((pa.x, pa.y), (pb.x, pb.y))
            if best is None or d_src > best[0]:
                best = (d_src, d_lib)
        if best and best[1] > 0:
            ratios.append(best[1] / best[0])
    if not ratios:
        return 0.2
    return statistics.median(ratios)


def _boxes_overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _point_in(box, p) -> bool:
    return box[0] < p[0] < box[2] and box[1] < p[1] < box[3]


@dataclass
class Sheet:
    width: float
    height: float
    paper: str
    usable: tuple[float, float, float, float]   # x0, y0, x1, y1 for the drawing


PAPERS = [("A4", 297.0, 210.0), ("A3", 420.0, 297.0), ("A2", 594.0, 420.0), ("A1", 841.0, 594.0)]


def choose_sheet(w: float, h: float) -> Sheet:
    for name, pw, ph in PAPERS:
        # leave the border (~10mm) and a band at the bottom for the title block
        ux0, uy0, ux1, uy1 = 20.32, 20.32, pw - 20.32, ph - 45.72
        if w <= ux1 - ux0 and h <= uy1 - uy0:
            return Sheet(pw, ph, name, (ux0, uy0, ux1, uy1))
    name, pw, ph = PAPERS[-1]
    return Sheet(pw, ph, name, (20.32, 20.32, pw - 20.32, ph - 45.72))


class Layout:
    """Places resolved parts to mirror the source drawing, then untangles overlaps."""

    def __init__(self, resolved: list[Resolved], spacing: float = 1.15):
        self.spacing = spacing
        self.placed: list[Placed] = []
        self.unplaced: list[Placed] = []
        for res in resolved:
            fit = fit_orientation(res)
            pl = Placed(res=res, orient=fit.orient, pinmap=fit.pinmap)
            obs = [p.at for p in res.part.pins.values() if p.at is not None]
            if obs:
                pl.src_center = (sum(p[0] for p in obs) / len(obs), sum(p[1] for p in obs) / len(obs))
                self.placed.append(pl)
            else:
                self.unplaced.append(pl)
        self.scale = estimate_scale(self.placed) * spacing
        self.src_min = (0.0, 0.0)
        self.offset = (0.0, 0.0)
        self.sheet = choose_sheet(100, 100)

    # source px → sheet mm
    def to_sheet(self, p: tuple[float, float]) -> tuple[float, float]:
        return (self.offset[0] + (p[0] - self.src_min[0]) * self.scale,
                self.offset[1] + (p[1] - self.src_min[1]) * self.scale)

    def place(self, extra_points: list[tuple[float, float]] = ()) -> None:
        pts = [pl.src_center for pl in self.placed] + list(extra_points)
        for pl in self.placed:
            pts += [p.at for p in pl.res.part.pins.values() if p.at is not None]
        if not pts:
            pts = [(0.0, 0.0)]
        minx, miny = min(p[0] for p in pts), min(p[1] for p in pts)
        maxx, maxy = max(p[0] for p in pts), max(p[1] for p in pts)
        w, h = (maxx - minx) * self.scale + 30, (maxy - miny) * self.scale + 30
        self.sheet = choose_sheet(w, h)
        self.src_min = (minx, miny)
        ux0, uy0, _, _ = self.sheet.usable
        self.offset = (snap(ux0 + 10.16), snap(uy0 + 10.16))
        for pl in self.placed:
            self._place_one(pl)
        self._legalise()

    def _place_one(self, pl: Placed) -> None:
        obs = {s: p.at for s, p in _semantic_pins(pl.res).items() if p.at is not None and s in pl.pinmap}
        if not obs:
            cx, cy = self.to_sheet(pl.src_center)
            pl.origin = (snap(cx), snap(cy))
            return
        lib = [pl.sym.pin(pl.pinmap[s], pl.res.unit) for s in obs]
        m = [apply(pl.orient.m, p.x, p.y) for p in lib]
        mcx = sum(p[0] for p in m) / len(m)
        mcy = sum(p[1] for p in m) / len(m)
        sx = sum(self.to_sheet(p)[0] for p in obs.values()) / len(obs)
        sy = sum(self.to_sheet(p)[1] for p in obs.values()) / len(obs)
        pl.origin = (snap(sx - mcx), snap(sy - mcy))

    # .................................................................. overlap
    def conflicts(self, pl: Placed, others: list[Placed], margin: float = 1.27) -> bool:
        ext = pl.extent(margin)
        body = pl.box()
        tips = [pl.pin_pos(p.number) for p in pl.pins()]
        for o in others:
            if o is pl:
                continue
            oext = o.extent(margin)
            if not _boxes_overlap(ext, oext):
                continue
            if _boxes_overlap(body, o.box()):
                return True
            obox = o.box()
            otips = [o.pin_pos(p.number) for p in o.pins()]
            if any(_point_in(obox, t) for t in tips) or any(_point_in(body, t) for t in otips):
                return True
            # coincident pin tips short two nets unless the drawing joined them
            for t in tips:
                for u in otips:
                    if abs(t[0] - u[0]) < 1e-3 and abs(t[1] - u[1]) < 1e-3:
                        return True
            # bodies closer than one grid step are unreadable
            if _boxes_overlap(pl.extent(0.3), o.extent(0.3)) and _boxes_overlap(body, o.extent(0.0)):
                return True
        return False

    def _legalise(self) -> None:
        order = sorted(self.placed, key=lambda p: (p.origin[0], p.origin[1]))
        done: list[Placed] = []
        for pl in order:
            if self.conflicts(pl, done):
                spot = self.find_spot(pl, done)
                if spot:
                    pl.origin = spot
                    pl.moved = True
            done.append(pl)

    def find_spot(self, pl: Placed, others: list[Placed], radius: int = 14) -> tuple[float, float] | None:
        ox, oy = pl.origin
        cands = sorted(((dx, dy) for dx in range(-radius, radius + 1) for dy in range(-radius, radius + 1)
                        if (dx, dy) != (0, 0)), key=lambda d: (abs(d[0]) + abs(d[1]), abs(d[1]), d))
        for dx, dy in cands:
            pl.origin = (snap(ox + dx * GRID), snap(oy + dy * GRID))
            if not self.conflicts(pl, others):
                return pl.origin
        pl.origin = (ox, oy)
        return None

    def bounds(self, margin: float = 0.0) -> tuple[float, float, float, float]:
        exts = [p.extent() for p in self.placed]
        if not exts:
            return (0, 0, 0, 0)
        return (min(e[0] for e in exts) - margin, min(e[1] for e in exts) - margin,
                max(e[2] for e in exts) + margin, max(e[3] for e in exts) + margin)
