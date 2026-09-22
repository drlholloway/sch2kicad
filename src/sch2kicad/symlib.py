"""Load KiCad library symbols: pins, geometry, and the copy embedded in a schematic.

Library coordinates are Y-up; schematic coordinates are Y-down. Everything in
this module stays in library coordinates — :mod:`geometry` does the transform.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr
from .kicad_env import LibTable, lib_table
from .sexpr import Q


@dataclass(frozen=True)
class Pin:
    number: str
    name: str
    etype: str          # passive, input, power_in, ...
    x: float            # connection point, library coords (Y up)
    y: float
    angle: int          # direction from the connection point toward the body
    length: float
    unit: int           # 0 = shared by every unit
    body_style: int     # 0 = shared, 1 = normal, 2 = De Morgan
    hidden: bool = False

    def outward(self) -> tuple[float, float]:
        """Unit vector pointing away from the body, library coords."""
        a = math.radians(self.angle)
        return (-round(math.cos(a)), -round(math.sin(a)))


@dataclass
class LibSymbol:
    lib_id: str
    node: list                       # flattened, named with the full lib_id
    pins: list[Pin]
    props: dict[str, str]
    unit_count: int
    power: bool
    _bbox_cache: dict = field(default_factory=dict, repr=False)

    @property
    def name(self) -> str:
        return self.lib_id.split(":", 1)[1]

    def pins_for(self, unit: int, body_style: int = 1) -> list[Pin]:
        return [p for p in self.pins
                if p.unit in (0, unit) and p.body_style in (0, body_style)]

    def pin(self, number: str, unit: int | None = None) -> Pin:
        for p in self.pins:
            if p.number == number and (unit is None or p.unit in (0, unit)):
                return p
        raise KeyError(f"{self.lib_id} has no pin {number!r}" + (f" in unit {unit}" if unit else ""))

    def unit_of_pin(self, number: str) -> int:
        for p in self.pins:
            if p.number == number:
                return p.unit
        raise KeyError(f"{self.lib_id} has no pin {number!r}")

    def body_bbox(self, unit: int, body_style: int = 1) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) of the drawn body, library coords, pins excluded."""
        key = (unit, body_style)
        if key not in self._bbox_cache:
            self._bbox_cache[key] = _graphics_bbox(self.node, self.name_in_node(), unit, body_style)
        return self._bbox_cache[key]

    def name_in_node(self) -> str:
        return str(self.node[1]).split(":", 1)[-1]


_UNIT_RE = re.compile(r"^(.*)_(\d+)_(\d+)$")


def _unit_suffix(subname: str) -> tuple[int, int]:
    m = _UNIT_RE.match(subname)
    if not m:
        return (0, 0)
    return int(m.group(2)), int(m.group(3))


def _graphics_bbox(node: list, base: str, unit: int, body_style: int) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []

    def add(x, y):
        xs.append(float(x))
        ys.append(float(y))

    for sub in sexpr.children(node, "symbol"):
        u, s = _unit_suffix(str(sub[1]))
        if u not in (0, unit) or s not in (0, body_style):
            continue
        for g in sub[1:]:
            h = sexpr.head(g)
            if h == "rectangle":
                for k in ("start", "end"):
                    c = sexpr.child(g, k)
                    add(c[1], c[2])
            elif h in ("polyline", "bezier"):
                pts = sexpr.child(g, "pts")
                for xy in sexpr.children(pts, "xy"):
                    add(xy[1], xy[2])
            elif h == "circle":
                c = sexpr.child(g, "center")
                r = float(sexpr.child(g, "radius")[1])
                add(float(c[1]) - r, float(c[2]) - r)
                add(float(c[1]) + r, float(c[2]) + r)
            elif h == "arc":
                for k in ("start", "mid", "end"):
                    c = sexpr.child(g, k)
                    if c:
                        add(c[1], c[2])
            elif h == "pin":
                # the pin's inner end is part of the body outline
                at = sexpr.child(g, "at")
                length = float(sexpr.value(sexpr.child(g, "length"), 1, 0))
                a = math.radians(float(at[3]) if len(at) > 3 else 0)
                add(float(at[1]) + length * math.cos(a), float(at[2]) + length * math.sin(a))
    if not xs:
        return (-1.27, -1.27, 1.27, 1.27)
    return (min(xs), min(ys), max(xs), max(ys))


def _parse_pins(node: list) -> list[Pin]:
    pins: list[Pin] = []
    for sub in sexpr.children(node, "symbol"):
        u, s = _unit_suffix(str(sub[1]))
        for p in sexpr.children(sub, "pin"):
            at = sexpr.child(p, "at")
            num = sexpr.child(p, "number")
            name = sexpr.child(p, "name")
            hidden = "hide" in p or any(
                isinstance(c, list) and c[0] == "hide" and (len(c) == 1 or c[1] == "yes") for c in p)
            pins.append(Pin(
                number=str(num[1]) if num else "",
                name=str(name[1]) if name else "",
                etype=str(p[1]),
                x=float(at[1]), y=float(at[2]),
                angle=int(float(at[3])) % 360 if len(at) > 3 else 0,
                length=float(sexpr.value(sexpr.child(p, "length"), 1, 0)),
                unit=u, body_style=s, hidden=hidden,
            ))
    return pins


def _unit_count(node: list) -> int:
    units = {_unit_suffix(str(sub[1]))[0] for sub in sexpr.children(node, "symbol")}
    units.discard(0)
    return max(units) if units else 1


class SymbolResolver:
    """Loads symbols by ``lib:name`` through KiCad's library tables."""

    def __init__(self, table: LibTable | None = None, project_dir: Path | None = None):
        self.table = table or lib_table("sym", project_dir)
        self._files: dict[str, dict[str, list]] = {}
        self._symbols: dict[str, LibSymbol] = {}

    def _lib(self, nickname: str) -> dict[str, list]:
        if nickname not in self._files:
            path = self.table.resolve(nickname)
            if path is None or not path.exists():
                raise KeyError(f"symbol library {nickname!r} is not in your sym-lib-table")
            root = sexpr.parse(path.read_text())
            self._files[nickname] = {str(s[1]): s for s in sexpr.children(root, "symbol")}
        return self._files[nickname]

    def exists(self, lib_id: str) -> bool:
        try:
            nick, name = lib_id.split(":", 1)
            return name in self._lib(nick)
        except (KeyError, ValueError):
            return False

    def _flatten(self, nick: str, name: str, depth: int = 0) -> list:
        lib = self._lib(nick)
        if name not in lib:
            raise KeyError(f"{nick}:{name} not found")
        node = copy.deepcopy(lib[name])
        ext = sexpr.child(node, "extends")
        if ext is None:
            return node
        if depth > 8:
            raise ValueError(f"extends chain too deep at {nick}:{name}")
        parent = self._flatten(nick, str(ext[1]), depth + 1)
        pname = str(parent[1])
        out = [node[0], Q(name)]
        child_props = {str(p[1]): p for p in sexpr.children(node, "property")}
        child_flags = {sexpr.head(c): c for c in node[2:] if isinstance(c, list)
                       and sexpr.head(c) not in ("property", "extends", "symbol")}
        seen_props: set[str] = set()
        for item in parent[2:]:
            h = sexpr.head(item)
            if h == "property":
                pn = str(item[1])
                out.append(child_props.get(pn, item))
                seen_props.add(pn)
            elif h == "symbol":
                sub = copy.deepcopy(item)
                sub[1] = Q(re.sub("^" + re.escape(pname), name, str(sub[1])))
                out.append(sub)
            elif h in child_flags:
                out.append(child_flags.pop(h))
            else:
                out.append(item)
        # properties only the child defines go before the unit sub-symbols
        extra = [p for n, p in child_props.items() if n not in seen_props]
        first_sub = next((i for i, c in enumerate(out) if sexpr.head(c) == "symbol"), len(out))
        out[first_sub:first_sub] = extra + list(child_flags.values())
        return out

    def get(self, lib_id: str) -> LibSymbol:
        if lib_id in self._symbols:
            return self._symbols[lib_id]
        if ":" not in lib_id:
            raise KeyError(f"lib_id {lib_id!r} needs a library nickname")
        nick, name = lib_id.split(":", 1)
        node = self._flatten(nick, name)
        node[1] = Q(lib_id)
        # KiCad reads a lone "~" in an older library as an empty field; embed it the
        # way KiCad will see the library, or ERC reports the copy as out of date
        for prop in sexpr.children(node, "property"):
            if len(prop) > 2 and prop[2] == "~":
                prop[2] = Q("")
        props = {str(p[1]): str(p[2]) for p in sexpr.children(node, "property")}
        power = sexpr.child(node, "power") is not None
        sym = LibSymbol(lib_id=lib_id, node=node, pins=_parse_pins(node), props=props,
                        unit_count=_unit_count(node), power=power)
        self._symbols[lib_id] = sym
        return sym
