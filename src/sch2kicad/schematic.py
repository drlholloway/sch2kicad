"""Write KiCad 10 ``.kicad_sch`` / ``.kicad_pro`` files.

The writer only emits what KiCad needs to load the file; ``kicad-cli sch
upgrade --force`` then re-saves it through KiCad's own writer, so the file on
disk is exactly what KiCad would have written.
"""

from __future__ import annotations

import json
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr
from .geometry import GRID, Orient, apply
from .kicad_env import GENERATOR, SCH_FILE_VERSION
from .sexpr import Q
from .symlib import LibSymbol


def new_uuid() -> str:
    return str(uuidlib.uuid4())


def _font(size: float = 1.27, bold: bool = False, italic: bool = False, color=None) -> list:
    f = ["font", ["size", size, size]]
    if bold:
        f.append(["bold", "yes"])
    if italic:
        f.append(["italic", "yes"])
    if color:
        f.append(["color", *color])
    return f


def _effects(size: float = 1.27, justify: list[str] | None = None, hide: bool = False,
             bold: bool = False, italic: bool = False, color=None) -> list:
    e = ["effects", _font(size, bold, italic, color)]
    if justify:
        e.append(["justify", *justify])
    if hide:
        e.append(["hide", "yes"])
    return e


@dataclass
class SymbolInstance:
    lib: LibSymbol
    ref: str
    unit: int
    at: tuple[float, float]
    orient: Orient
    value: str
    footprint: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    field_pos: dict[str, tuple[float, float]] = field(default_factory=dict)
    hidden_fields: set[str] = field(default_factory=set)
    dnp: bool = False
    uuid: str = field(default_factory=new_uuid)


class SchematicWriter:
    def __init__(self, project: str, paper: str = "A4"):
        self.project = project
        self.paper = paper
        self.root_uuid = new_uuid()
        self.lib_symbols: dict[str, LibSymbol] = {}
        self.items: list[list] = []
        self.title_block: dict = {}
        self.symbols: list[SymbolInstance] = []
        self.groups: list[tuple[str, list[str]]] = []
        self._auto: dict[str, int] = {}

    # ............................................................... symbols
    def add_symbol(self, inst: SymbolInstance) -> str:
        if inst.ref.endswith("?"):  # power symbols and flags: #PWR01, #FLG01, ...
            prefix = inst.ref[:-1]
            self._auto[prefix] = self._auto.get(prefix, 0) + 1
            inst.ref = f"{prefix}{self._auto[prefix]:02d}"
        self.lib_symbols.setdefault(inst.lib.lib_id, inst.lib)
        self.symbols.append(inst)
        return inst.uuid

    def _symbol_node(self, s: SymbolInstance) -> list:
        at = ["at", s.at[0], s.at[1], s.orient.angle]
        node: list = ["symbol", ["lib_id", Q(s.lib.lib_id)], at]
        if s.orient.mirror:
            node.append(["mirror", s.orient.mirror])
        node += [["unit", s.unit], ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
                 ["dnp", "yes" if s.dnp else "no"], ["uuid", Q(s.uuid)]]
        # the text reads horizontally on screen whatever the symbol's rotation
        text_angle = 90 if s.orient.angle in (90, 270) else 0
        lib_props = dict(s.lib.props)
        props: list[tuple[str, str]] = [("Reference", s.ref), ("Value", s.value),
                                        ("Footprint", s.footprint),
                                        ("Datasheet", lib_props.get("Datasheet", "")),
                                        ("Description", lib_props.get("Description", ""))]
        for k, v in s.fields.items():
            props.append((k, v))
        for name, val in props:
            pos = s.field_pos.get(name, s.at)
            hide = name in s.hidden_fields or name in ("Footprint", "Datasheet", "Description") \
                or (s.lib.power and name == "Reference")
            node.append(["property", Q(name), Q(val), ["at", pos[0], pos[1], text_angle],
                         _effects(hide=hide)])
        for p in s.lib.pins_for(s.unit):
            node.append(["pin", Q(p.number), ["uuid", Q(new_uuid())]])
        node.append(["instances", ["project", Q(self.project),
                                   ["path", Q("/" + self.root_uuid), ["reference", Q(s.ref)], ["unit", s.unit]]]])
        return node

    # ................................................................. items
    def wire(self, a, b) -> str:
        u = new_uuid()
        self.items.append(["wire", ["pts", ["xy", a[0], a[1]], ["xy", b[0], b[1]]],
                           ["stroke", ["width", 0], ["type", "default"]], ["uuid", Q(u)]])
        return u

    def junction(self, p) -> str:
        u = new_uuid()
        self.items.append(["junction", ["at", p[0], p[1]], ["diameter", 0], ["color", 0, 0, 0, 0], ["uuid", Q(u)]])
        return u

    def no_connect(self, p) -> str:
        u = new_uuid()
        self.items.append(["no_connect", ["at", p[0], p[1]], ["uuid", Q(u)]])
        return u

    def label(self, name: str, p, out: tuple[int, int]) -> str:
        """Local net label at a pin tip, text running away from the part."""
        angle, just = {(1, 0): (0, ["left", "bottom"]), (-1, 0): (180, ["right", "bottom"]),
                       (0, -1): (90, ["left", "bottom"]), (0, 1): (270, ["right", "bottom"])}[out]
        u = new_uuid()
        self.items.append(["label", Q(name), ["at", p[0], p[1], angle], ["fields_autoplaced", "yes"],
                           _effects(justify=just), ["uuid", Q(u)]])
        return u

    def global_label(self, name: str, p, text_dir: tuple[int, int], shape: str = "bidirectional") -> str:
        angle, just = {(1, 0): (0, ["left"]), (-1, 0): (180, ["right"]),
                       (0, -1): (90, ["left"]), (0, 1): (270, ["right"])}[text_dir]
        u = new_uuid()
        self.items.append(["global_label", Q(name), ["shape", shape], ["at", p[0], p[1], angle],
                           ["fields_autoplaced", "yes"], _effects(justify=just), ["uuid", Q(u)]])
        return u

    def text(self, s: str, p, size: float = 1.27, justify=("left",), color=None, italic=False, bold=False) -> str:
        u = new_uuid()
        self.items.append(["text", Q(s), ["exclude_from_sim", "no"], ["at", p[0], p[1], 0],
                           _effects(size=size, justify=list(justify), color=color, italic=italic, bold=bold),
                           ["uuid", Q(u)]])
        return u

    def group(self, name: str, members: list[str]) -> None:
        if members:
            self.groups.append((name, members))

    # ............................................................... output
    def _title_block(self) -> list | None:
        tb = self.title_block
        if not tb:
            return None
        node: list = ["title_block"]
        for k in ("title", "date", "rev", "company"):
            if tb.get(k):
                node.append([k, Q(str(tb[k]))])
        for i, c in enumerate(tb.get("comments", [])[:9], start=1):
            node.append(["comment", i, Q(str(c))])
        return node

    def document(self) -> list:
        doc: list = ["kicad_sch", ["version", SCH_FILE_VERSION], ["generator", Q(GENERATOR)],
                     ["generator_version", Q("0.1")], ["uuid", Q(self.root_uuid)], ["paper", Q(self.paper)]]
        tb = self._title_block()
        if tb:
            doc.append(tb)
        libs: list = ["lib_symbols"]
        for lib_id in sorted(self.lib_symbols):
            libs.append(self.lib_symbols[lib_id].node)
        doc.append(libs)
        doc.extend(self.items)
        for s in self.symbols:
            doc.append(self._symbol_node(s))
        for name, members in self.groups:
            doc.append(["group", Q(name), ["uuid", Q(new_uuid())], ["members", *[Q(m) for m in members]]])
        doc.append(["sheet_instances", ["path", Q("/"), ["page", Q("1")]]])
        doc.append(["embedded_fonts", "no"])
        return doc

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sexpr.dumps(self.document()) + "\n")
        return path


def write_project(path: Path, root_uuid: str) -> Path:
    """Minimal .kicad_pro; KiCad fills in every default on first open."""
    data = {
        "meta": {"filename": path.name, "version": 3},
        "sheets": [[root_uuid, "Root"]],
        "text_variables": {},
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def field_positions(inst: SymbolInstance, box: tuple[float, float, float, float],
                    pin_dirs: list[tuple[int, int]], names: list[str]) -> dict[str, tuple[float, float]]:
    """Stack Reference / Value / Label beside the body, on a side without pins."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    used = set(pin_dirs)
    line = 1.524
    n = len(names)
    widths = [max(len(_visible_text(inst, k)), 1) * 0.95 for k in names]
    wmax = max(widths) if widths else 3

    def stack(x: float, ytop: float) -> dict:
        return {k: (round(x, 3), round(ytop + i * line, 3)) for i, k in enumerate(names)}

    if (1, 0) not in used:           # right side free
        return stack(x1 + 0.8 + wmax / 2, cy - (n - 1) * line / 2)
    if (-1, 0) not in used:          # left side free
        return stack(x0 - 0.8 - wmax / 2, cy - (n - 1) * line / 2)
    if (0, -1) not in used:          # above
        return stack(cx, y0 - 1.0 - (n - 1) * line)
    if (0, 1) not in used:           # below
        return stack(cx, y1 + 1.6)
    # pins on every side (an op-amp drawn with its power pins): upper right, above the output
    return stack(x1 + 0.8 + wmax / 2, cy - 2.2 - (n - 1) * line)


def _visible_text(inst: SymbolInstance, name: str) -> str:
    if name == "Reference":
        return inst.ref
    if name == "Value":
        return inst.value
    return inst.fields.get(name, "")
