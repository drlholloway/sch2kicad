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
from .kicad_env import BUNDLED_SYM_LIB, GENERATOR, SCH_FILE_VERSION, bundled_symbols
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
        self.wires: list[tuple[tuple[float, float], tuple[float, float]]] = []
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
        # a field of the same name replaces the standard one rather than being added
        # beside it: KiCad takes two properties with one name as a broken symbol.
        standard = ("Reference", "Value", "Footprint", "Datasheet", "Description")
        props: list[tuple[str, str]] = [("Reference", s.ref), ("Value", s.value),
                                        ("Footprint", s.footprint),
                                        ("Datasheet", lib_props.get("Datasheet", "")),
                                        ("Description", s.fields.get("Description")
                                         or lib_props.get("Description", ""))]
        for k, v in s.fields.items():
            if k not in standard:
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
        self.wires.append((a, b))
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


# KiCad's default ERC pin-conflict matrix (0 ok, 1 warning, 2 error), in the pin-type
# order of ELECTRICAL_PINTYPE: input, output, bidi, tristate, passive, free,
# unspecified, power in, power out, open collector, open emitter, not connected
_PIN_MAP = [
    [0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 2],
    [0, 2, 0, 1, 0, 0, 1, 0, 2, 2, 2, 2],
    [0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 1, 2],
    [0, 1, 0, 0, 0, 0, 1, 1, 2, 1, 1, 2],
    [0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 2],
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2],
    [1, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1, 2],
    [0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 2],
    [0, 2, 1, 2, 0, 0, 1, 0, 2, 2, 2, 2],
    [0, 2, 1, 2, 0, 0, 1, 0, 2, 0, 0, 2],
    [0, 2, 1, 1, 0, 0, 1, 0, 2, 0, 0, 2],
    [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2],
]
_POWER_OUT, _OPEN_COLLECTOR, _OPEN_EMITTER = 8, 9, 10


def _pin_map_allowing_open_on_power() -> list[list[int]]:
    """A transistor pin on a rail is normal; KiCad's default matrix calls it a conflict.

    The house transistor symbols type collector and emitter as open drivers, so a
    grounded emitter — or a collector on the supply — meets the net's PWR_FLAG and
    errors. Only that pair is allowed; every other conflict keeps KiCad's severity.
    """
    pin_map = [row[:] for row in _PIN_MAP]
    for other in (_OPEN_COLLECTOR, _OPEN_EMITTER):
        pin_map[_POWER_OUT][other] = 0
        pin_map[other][_POWER_OUT] = 0
    return pin_map


def write_project(path: Path, root_uuid: str, allow_open_on_power: bool = False) -> Path:
    """Minimal .kicad_pro; KiCad fills in every default on first open."""
    data = {
        "meta": {"filename": path.name, "version": 3},
        "sheets": [[root_uuid, "Root"]],
        "text_variables": {},
    }
    if allow_open_on_power:
        data["erc"] = {"erc_exclusions": [], "meta": {"version": 0},
                       "pin_map": _pin_map_allowing_open_on_power(), "rule_severities": {}}
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def copy_bundled_symbols(out_dir: Path, lib_ids) -> Path | None:
    """A symbol KiCad has no library for travels with the project.

    The schematic embeds its own copy of every symbol, so it opens either way,
    but without the library KiCad calls the symbol missing and ERC complains.
    """
    if not any(str(lib_id).startswith(BUNDLED_SYM_LIB + ":") for lib_id in lib_ids):
        return None
    dest = out_dir / bundled_symbols().name
    dest.write_text(bundled_symbols().read_text())
    table = out_dir / "sym-lib-table"
    uri = "${KIPRJMOD}/" + dest.name
    entry = f'  (lib (name "{BUNDLED_SYM_LIB}") (type "KiCad") (uri "{uri}") (options "") (descr "symbols sch2kicad ships for parts KiCad has none of"))'
    if table.exists() and BUNDLED_SYM_LIB in table.read_text():
        return dest
    if table.exists():   # keep whatever else the project already lists
        text = table.read_text().rstrip()
        text = text[:text.rindex(")")].rstrip() + "\n" + entry + "\n)\n"
    else:
        text = "(sym_lib_table\n  (version 7)\n" + entry + "\n)\n"
    table.write_text(text)
    return dest


FIELD_LINE = 1.524          # mm between stacked field lines
_SHIFTS = (0.0, 1.27, -1.27, 2.54, -2.54, 3.81, -3.81, 5.08, -5.08, 6.35, -6.35)
_PUSHES = (0.0, 1.27, 2.54, 3.81, 5.08, 6.35)


def text_box(at: tuple[float, float], text: str) -> tuple[float, float, float, float]:
    """Rough extent of centred 1.27 mm field text.

    Always horizontal: a property's stored angle is read in the symbol's own frame,
    so the 90° written for a turned symbol is what makes its text read across the
    page (see ``_symbol_node``), not up it.
    """
    w = max(len(text), 1) * 0.95
    return (at[0] - w / 2, at[1] - 0.85, at[0] + w / 2, at[1] + 0.85)


def _overlap_area(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def field_positions(inst: SymbolInstance, box: tuple[float, float, float, float],
                    pin_dirs: list[tuple[int, int]], names: list[str],
                    blocked: list[tuple[float, float, float, float]] = ()) -> dict[str, tuple[float, float]]:
    """Stack Reference / Value / Label beside the body, clear of pins and of everything else.

    Sides without pins come first, then the rest; on each side the stack slides
    along the body and steps further out until it lands on nothing in
    ``blocked`` (other symbols' bodies and pin lines, text already placed). When
    every spot is taken, the one that covers the least wins: a value half over a
    wire still reads, and a value is worth more than a tidy rule.
    """
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    used = set(pin_dirs)
    line = FIELD_LINE
    n = len(names)
    texts = [_visible_text(inst, k) for k in names]
    wmax = max((max(len(t), 1) * 0.95 for t in texts), default=3)
    thick = (n - 1) * line + 1.7
    hx, hy = wmax / 2, thick / 2

    def stack(sx: float, sy: float) -> dict:
        """Centre of the whole stack → where each line goes."""
        return {k: (round(sx, 3), round(sy + (i - (n - 1) / 2) * line, 3))
                for i, k in enumerate(names)}

    def at_side(side: tuple[int, int], push: float, slide: float) -> dict:
        if side == (1, 0):
            return stack(x1 + 0.8 + hx + push, cy + slide)
        if side == (-1, 0):
            return stack(x0 - 0.8 - hx - push, cy + slide)
        if side == (0, -1):
            return stack(cx + slide, y0 - 0.8 - hy - push)
        return stack(cx + slide, y1 + 0.8 + hy + push)

    def spoilt(pos: dict) -> float:
        """How much of the text lands on something — 0 when it is in the clear."""
        return sum(_overlap_area(text_box(pos[k], t), b)
                   for k, t in zip(names, texts) for b in blocked)

    order = [(1, 0), (-1, 0), (0, -1), (0, 1)]
    free = [s for s in order if s not in used]
    first = at_side(free[0], 0.0, 0.0) if free else at_side((1, 0), 0.0, -2.2 - hy)
    if not blocked:
        return first
    best, worst = first, spoilt(first)
    for sides in (free, [s for s in order if s in used]):
        for push in _PUSHES:
            for slide in _SHIFTS:
                for side in sides:
                    pos = at_side(side, push, slide)
                    hidden = spoilt(pos)
                    if hidden <= 0:
                        return pos
                    if hidden < worst:     # nowhere is clear: covered least still reads best
                        best, worst = pos, hidden
    return best


def _visible_text(inst: SymbolInstance, name: str) -> str:
    if name == "Reference":
        return inst.ref
    if name == "Value":
        return inst.value
    return inst.fields.get(name, "")
