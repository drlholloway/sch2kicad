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
    pinout: str = ""             # explicit lead order, e.g. "EBC" — confirmed by the user
    footprint: str = ""          # explicit override
    lib_id: str = ""             # explicit override
    pins: dict[str, PinRef] = field(default_factory=dict)
    supply: dict[str, str] = field(default_factory=dict)   # IC power pins → nets
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
        parts.append(Part(
            ref=str(p["ref"]), type=str(p["type"]), value=str(p.get("value", "")),
            part=str(p.get("part", "")), unit=str(p.get("unit", "")), label=str(p.get("label", "")),
            pinout=str(p.get("pinout", "")).upper(), footprint=str(p.get("footprint", "")),
            lib_id=str(p.get("lib_id", "")), pins=pins,
            supply={str(k): str(v) for k, v in (p.get("supply") or {}).items()},
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
                   bom_accept={str(k): str(v) for k, v in (data.get("bom_accept") or {}).items()})


# ------------------------------------------------------------ value parsing

_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3,
         "M": 1e6, "G": 1e9, "r": 1.0, "R": 1.0, "": 1.0}


def parse_value(v: str) -> float | None:
    """'4k7' → 4700, '100nF' → 1e-7, '2.2uF' → 2.2e-6, 'A100K' → 1e5, '10' → 10."""
    s = v.strip().replace(" ", "").replace("Ω", "").replace("ohm", "")
    s = re.sub(r"^[ABCW](?=\d)", "", s)          # pot taper prefix
    s = re.sub(r"(?<=[\dkKmM])[ABCW]$", "", s)    # taper suffix (100kB, 50KA)
    s = re.sub(r"[FfHh]$", "", s)                 # unit
    m = re.fullmatch(r"(\d+)([pnuµmkKMGrR])(\d+)", s)   # RKM: 4k7, 2M2, 4n7
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _MULT[m.group(2)]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([pnuµmkKMGrR]?)", s)
    if m:
        return float(m.group(1)) * _MULT[m.group(2)]
    return None
