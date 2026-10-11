"""The circuit description Claude writes after reading a schematic.

``circuit.json`` is the contract between the reading step and everything
after it. Pins are named for what they are (``B``/``C``/``E``, ``cw``/
``wiper``/``ccw``, ``+``/``-``, ``A``/``K``) and carry the pixel position
where the wire meets the part in the source image. The build step turns those
into KiCad pin numbers and orientations; nothing downstream guesses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "sch2kicad/circuit@1"

NC = "NC"  # a pin deliberately left unconnected


@dataclass
class PinRef:
    name: str            # semantic pin name as written in circuit.json
    net: str | None      # None = no connection
    at: tuple[float, float] | None


@dataclass
class Part:
    ref: str
    type: str
    value: str = ""
    part: str = ""               # part number for semiconductors / ICs
    unit: str = ""               # "A", "B", ... for multi-unit parts; "" otherwise
    label: str = ""              # knob / switch function (FUZZ, TONE, ...)
    action: str = ""             # a toggle's action: on/on, on/off/on, on/on/on
    pinout: str = ""             # explicit lead order, e.g. "EBC" — confirmed by the user
    footprint: str = ""          # explicit override
    lib_id: str = ""             # explicit override
    pins: dict[str, PinRef] = field(default_factory=dict)
    supply: dict[str, str] = field(default_factory=dict)   # IC power pins → nets
    # where the source draws those power pins, when it draws the power unit itself
    supply_at: dict[str, tuple[float, float]] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)   # extra KiCad fields
    dnp: bool = False

    @property
    def key(self) -> str:
        return self.ref + (self.unit or "")


@dataclass
class Port:
    name: str
    net: str
    at: tuple[float, float] | None = None


@dataclass
class Text:
    text: str
    at: tuple[float, float] | None = None
    kind: str = "note"           # voltage, note, ...


@dataclass
class Block:
    name: str
    parts: list[str]
    description: str = ""


@dataclass
class Uncertain:
    note: str
    refs: list[str] = field(default_factory=list)
    at: tuple[float, float] | None = None


@dataclass
class Net:
    name: str
    kind: str = "signal"         # signal, power, ground
    draw: str = ""               # "wire" or "flag"; default: ground/power → flag


@dataclass
class Circuit:
    name: str
    source: dict
    title_block: dict
    nets: dict[str, Net]
    parts: list[Part]
    ports: list[Port]
    texts: list[Text]
    notes: list[str]
    blocks: list[Block]
    uncertain: list[Uncertain]
    path: Path | None = None
    # the build doc's parts list (rows of ref/value/type/notes) and differences from
    # it the user has looked at and accepted, ref → why
    bom: list[dict] = field(default_factory=list)
    bom_accept: dict[str, str] = field(default_factory=dict)
    # false when no 3PDT switches the jacks (buffered or JFET-switched bypass): IN and
    # OUT then get pads of their own instead of the 3PDT pad array
    true_bypass: bool = True
    # a stripboard layout this schematic was drawn from (board.json, beside circuit.json):
    # verify checks the schematic connects exactly what the board connects
    board: str = ""
    # "ours" when the positions are a schematic drawn for the reading rather than pixels
    # of a drawing: a stripboard reading's designer version (final/), which no longer
    # matches its board. A reading with a board is ours already.
    arrangement: str = ""
    # set by house style (house.py): new ref → the reading's ref, and the parts it added
    renamed: dict[str, str] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)

    @property
    def ours(self) -> bool:
        """Is the arrangement ours to lay out, rather than a drawing's to follow?"""
        return bool(self.board) or self.arrangement == "ours"

    # ------------------------------------------------------------------ nets
    def net(self, name: str) -> Net:
        return self.nets.get(name) or Net(name)

    def net_kind(self, name: str) -> str:
        return self.net(name).kind

    def all_net_names(self) -> list[str]:
        names: dict[str, None] = {}
        for p in self.parts:
            for pin in p.pins.values():
                if pin.net:
                    names[pin.net] = None
            for n in p.supply.values():
                names[n] = None
        for port in self.ports:
            names[port.net] = None
        for n in self.nets:
            names[n] = None
        return list(names)

    def board_path(self) -> Path | None:
        if not self.board:
            return None
        p = Path(self.board)
        return p if p.is_absolute() or self.path is None else self.path.parent / p

    def source_image(self) -> Path | None:
        img = self.source.get("image")
        if not img:
            return None
        p = Path(img)
        if not p.is_absolute() and self.path is not None:
            p = self.path.parent / p
        return p


# ---------------------------------------------------------------- loading

def _xy(v, where: str) -> tuple[float, float] | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(c, (int, float)) for c in v):
        return (float(v[0]), float(v[1]))
    raise ValueError(f"{where}: expected [x, y], got {v!r}")


def _pin(name: str, v, where: str) -> PinRef:
    # accepted forms: {"net": "X", "at": [x, y]} | ["X", x, y] | "X"
    if isinstance(v, dict):
        net = v.get("net")
        at = _xy(v.get("at"), where)
    elif isinstance(v, list) and len(v) == 3:
        net, at = v[0], _xy(v[1:], where)
    elif isinstance(v, str) or v is None:
        net, at = v, None
    else:
        raise ValueError(f"{where}: pin {name!r} must be {{net, at}}, [net, x, y] or a net name")
    if isinstance(net, str) and net.upper() in (NC, "N/C", "NONE", ""):
        net = None
    return PinRef(name=name, net=net, at=at)


def load(path: str | Path) -> Circuit:
    path = Path(path)
    data = json.loads(path.read_text())
    c = from_dict(data)
    c.path = path.resolve()
    return c


def from_dict(data: dict) -> Circuit:
    if data.get("schema") not in (SCHEMA, None):
        raise ValueError(f"unknown schema {data.get('schema')!r}; expected {SCHEMA}")
    nets = {}
    for name, v in (data.get("nets") or {}).items():
        v = v or {}
        nets[name] = Net(name=name, kind=v.get("kind", "signal"), draw=v.get("draw", ""))
    parts = []
    for i, p in enumerate(data.get("parts") or []):
        where = f"parts[{i}] ({p.get('ref', '?')})"
        if "ref" not in p or "type" not in p:
            raise ValueError(f"{where}: every part needs ref and type")
        pins = {str(k): _pin(str(k), v, f"{where}.pins.{k}") for k, v in (p.get("pins") or {}).items()}
        # supply pins take a net name, or [net, x, y] like pins when the power unit is drawn
        supply = {str(k): _pin(str(k), v, f"{where}.supply.{k}") for k, v in (p.get("supply") or {}).items()}
        parts.append(Part(
            ref=str(p["ref"]), type=str(p["type"]), value=str(p.get("value", "")),
            part=str(p.get("part", "")), unit=str(p.get("unit", "")), label=str(p.get("label", "")),
            action=_action_of(p),
            pinout=str(p.get("pinout", "")).upper(), footprint=str(p.get("footprint", "")),
            lib_id=str(p.get("lib_id", "")), pins=pins,
            supply={k: str(v.net) for k, v in supply.items()},
            supply_at={k: v.at for k, v in supply.items() if v.at is not None},
            fields={str(k): str(v) for k, v in (p.get("fields") or {}).items()},
            dnp=bool(p.get("dnp", False)),
        ))
    ports = [Port(name=str(x["name"]), net=str(x["net"]), at=_xy(x.get("at"), f"ports.{x.get('name')}"))
             for x in data.get("ports") or []]
    texts = [Text(text=str(x["text"]), at=_xy(x.get("at"), "texts"), kind=str(x.get("kind", "note")))
             for x in data.get("texts") or []]
    blocks = [Block(name=str(b["name"]), parts=[str(r) for r in b.get("parts", [])],
                    description=str(b.get("description", ""))) for b in data.get("blocks") or []]
    uncertain = [Uncertain(note=str(u["note"]), refs=[str(r) for r in u.get("refs", [])],
                           at=_xy(u.get("at"), "uncertain")) for u in data.get("uncertain") or []]
    name = str(data.get("name") or (data.get("title_block") or {}).get("title") or "converted")
    return Circuit(name=name, source=dict(data.get("source") or {}),
                   title_block=dict(data.get("title_block") or {}), nets=nets, parts=parts,
                   ports=ports, texts=texts, notes=[str(n) for n in data.get("notes") or []],
                   blocks=blocks, uncertain=uncertain,
                   bom=[dict(r) for r in data.get("bom") or []],
                   bom_accept={str(k): str(v) for k, v in (data.get("bom_accept") or {}).items()},
                   true_bypass=bool(data.get("true_bypass", True)),
                   board=str(data.get("board") or ""),
                   arrangement=str(data.get("arrangement") or ""))


# ------------------------------------------------------------ value parsing

_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3,
         "M": 1e6, "G": 1e9, "r": 1.0, "R": 1.0, "": 1.0}


# A toggle's action: how many positions it has, and whether the middle one is
# off. It is not in the netlist — a com/a/b netlist is the same for all three —
# so the only record is what the drawing or the parts list writes, and the three
# are different parts to order. Publishers spell them every way there is: Aion
# writes "SPDT cntr off" in the value column and "on-off-on" in the type column
# of the same row, so every spelling folds to one.
SWITCH_ACTIONS = ("on/on", "on/off/on", "on/on/on", "on/off")   # on/off is the SPST
_ACTION_SPELLINGS = {
    "onon": "on/on", "2position": "on/on",
    "ononon": "on/on/on", "3position": "on/on/on",
    "onoffon": "on/off/on", "cntroff": "on/off/on", "ctroff": "on/off/on",
    "centeroff": "on/off/on", "centreoff": "on/off/on",
    "onoff": "on/off",
}


def parse_action(text: str) -> str:
    """The switch action named anywhere in a string, canonically spelt, else "".

    Reads a whole parts-list row as happily as a bare action, so "Toggle switch,
    SPDT on-off-on" and "cntr off" both come back "on/off/on". Longest spelling
    wins, so "on-on-on" is not read as the "on-on" inside it.
    """
    s = re.sub(r"[\s\-_./,()]", "", text).lower()
    for spelling in sorted(_ACTION_SPELLINGS, key=len, reverse=True):
        if spelling in s:
            return _ACTION_SPELLINGS[spelling]
    return ""


def _action_of(p: dict) -> str:
    """A part's switch action, canonical where it can be read, raw where it can't.

    An unreadable spelling is kept rather than dropped, so the build can say it
    does not know it instead of quietly shipping a switch with no action. A sheet
    read before ``action`` existed wrote it into the value ("SPDT on/on"), so a
    switch falls back to whatever its value names.
    """
    raw = str(p.get("action", "")).strip()
    if raw:
        return parse_action(raw) or raw
    if str(p.get("type", "")).startswith("switch"):
        return parse_action(str(p.get("value", "")))
    return ""


def parse_value(v: str) -> float | None:
    """'4k7' → 4700, '100nF' → 1e-7, '2.2uF' → 2.2e-6, 'A100K' → 1e5, '10' → 10, '.047uF' → 4.7e-8."""
    s = v.strip().replace(" ", "").replace("Ω", "").replace("ohm", "")
    s = re.sub(r"^[ABCW](?=\d)", "", s)          # pot taper prefix
    s = re.sub(r"(?<=[\dkKmM])[ABCW]$", "", s)    # taper suffix (100kB, 50KA)
    s = re.sub(r"[FfHh]$", "", s)                 # unit
    m = re.fullmatch(r"(\d+)([pnuµmkKMGrR])(\d+)", s)   # RKM: 4k7, 2M2, 4n7
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _MULT[m.group(2)]
    # a bare leading point is how US sheets write a cap: DOD's .047uF, .0068uF
    m = re.fullmatch(r"(\d+(?:\.\d+)?|\.\d+)([pnuµmkKMGrR]?)", s)
    if m:
        return float(m.group(1)) * _MULT[m.group(2)]
    return None
