"""circuit.json → KiCad project: the whole build in one place."""

from __future__ import annotations

import json
import math
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from .circuit import Circuit, Part, PinRef, load
from .geometry import (GRID, Layout, Orient, Placed, apply, fit_orientation, pin_outward, pin_world,
                       snap, spiral)
from .kicad_env import kicad_cli
from .parts import Profile, Resolved, canonical_pin, load_profile, resolve_all
from .router import Cell, NetRoute, Router, Terminal, cell_of, normalise
from .schematic import (SchematicWriter, SymbolInstance, copy_bundled_symbols, field_positions,
                        text_box as _text_box, write_project)
from .symlib import LibSymbol, SymbolResolver

RED = (200, 0, 0, 1)
GREY = (90, 90, 90, 1)
OPEN_PINS = {"open_emitter", "open_collector"}
SHARE_MM = 7.62      # two flags of one rail closer than this are one node drawn twice


@dataclass
class BuildResult:
    sch: Path
    pro: Path
    expected: dict[str, list[str]]          # net → ["R1.1", ...] (real parts only)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    labelled_nets: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    blocks: list[Path] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)


def safe_name(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "-", s).strip()
    return re.sub(r"\s+", " ", s) or "converted"


# ------------------------------------------------------------------- ports

def _port_parts(circuit: Circuit, profile: Profile, symbols: SymbolResolver) -> tuple[list[Resolved], list[str]]:
    """Each off-board connection becomes a pad: a unit of the 3PDT pad array or its own pad."""
    cfg = profile.ports
    problems: list[str] = []
    out: list[Resolved] = []
    # the array is the 3PDT's pads; with no 3PDT in the circuit it would only strand
    # them, and the jacks come to the board on sockets of their own instead
    array_id = cfg.get("array") if circuit.true_bypass else None
    jacks = {} if circuit.true_bypass else {k.upper(): v for k, v in (cfg.get("jacks") or {}).items()}
    array_pins = {k.upper(): v for k, v in (cfg.get("array_pins") or {}).items()}
    aliases = {k.upper(): v.upper() for k, v in (cfg.get("aliases") or {}).items()}
    ground = next((n for n in circuit.nets if circuit.net_kind(n) == "ground"), "GND")
    taken = {p.ref for p in circuit.parts}
    used_array: dict[str, str] = {}
    used_jacks: set[str] = set()
    pad_n = jack_n = 0
    for port in circuit.ports:
        name = port.name.strip()
        key = aliases.get(name.upper(), name.upper())
        if key in jacks and key not in used_jacks:
            sym = symbols.get(jacks[key])
            by_name = {p.name.upper(): p.number for p in sym.pins_for(1)}
            if not {"T", "R"} <= set(by_name):
                problems.append(f"port {name}: {jacks[key]} has no T and R pins")
                continue
            jack_n += 1
            while f"J{jack_n}" in taken:
                jack_n += 1
            part = Part(ref=f"J{jack_n}", type="port", value=f"J{key}",
                        pins={"T": PinRef("T", port.net, port.at), "R": PinRef("R", ground, None)})
            out.append(Resolved(part=part, lib_id=jacks[key], symbol=sym, unit=1,
                                footprint=sym.props.get("Footprint", ""), value=f"J{key}", fields={},
                                pinmap={"T": by_name["T"], "R": by_name["R"]}))
            used_jacks.add(key)
        elif array_id and key in array_pins and key not in used_array and key != "GND":
            sym = symbols.get(array_id)
            pin = array_pins[key]
            unit = sym.unit_of_pin(pin)
            part = Part(ref="Switch_Connectors1", type="port", value="", unit=chr(ord("A") + unit - 1),
                        pins={"1": PinRef("1", port.net, port.at)})
            out.append(Resolved(part=part, lib_id=array_id, symbol=sym, unit=unit,
                                footprint=sym.props.get("Footprint", ""), value="", fields={},
                                pinmap={"1": pin}))
            used_array[key] = pin
        else:
            is_power = circuit.net_kind(port.net) in ("power", "ground") or re.search(r"\d\s*V|VCC|V\+", name, re.I)
            lib_id = cfg.get("power_pad" if is_power else "pad") or cfg.get("pad")
            if not lib_id:
                problems.append(f"port {name}: profile has no pad symbol")
                continue
            sym = symbols.get(lib_id)
            pad_n += 1
            part = Part(ref=f"H{pad_n}", type="port", value=name, pins={"1": PinRef("1", port.net, port.at)})
            out.append(Resolved(part=part, lib_id=lib_id, symbol=sym, unit=1,
                                footprint=sym.props.get("Footprint", ""), value=name, fields={},
                                pinmap={"1": sym.pins_for(1)[0].number}))
    if used_array:
        # the array's other pads exist on the board whether or not the drawing uses them
        sym = symbols.get(array_id)
        for key, pin in array_pins.items():
            if key in used_array:
                continue
            unit = sym.unit_of_pin(pin)
            net = "GND" if key == "GND" else None
            part = Part(ref="Switch_Connectors1", type="port", unit=chr(ord("A") + unit - 1),
                        pins={"1": PinRef("1", net, None)})
            out.append(Resolved(part=part, lib_id=array_id, symbol=sym, unit=unit,
                                footprint=sym.props.get("Footprint", ""), value="", fields={},
                                pinmap={"1": pin}))
    return out, problems


# ------------------------------------------------------------------ builder

class Builder:
    def __init__(self, circuit: Circuit, profile: Profile, project: str, spacing: float = 1.15,
                 ports_as: str = "pads"):
        self.c = circuit
        self.profile = profile
        self.project = project
        self.symbols = SymbolResolver()
        self.problems: list[str] = []
        self.notes: list[str] = []
        self.resolved = resolve_all(circuit, profile, self.symbols)
        # ports become pads on a board, global labels in a block file
        self.label_ports = list(circuit.ports) if ports_as == "labels" else []
        if ports_as == "pads":
            ports, p = _port_parts(circuit, profile, self.symbols)
            self.problems += p
            self.resolved += ports
        for r in self.resolved:
            self.problems += r.problems
            self.notes += r.notes
        self.layout = Layout(self.resolved, spacing=spacing)
        self.w = SchematicWriter(project)
        self.flag_items: dict[str, list[str]] = {}    # part key → uuids of flags/stubs attached to it
        self.term_owner_items: dict[str, list[str]] = {}
        self.net_items: dict[str, list[str]] = {}
        self.labelled: list[str] = []
        self.text_boxes: list[tuple] = []           # visible field text of placed symbols
        self.flag_stubs: dict[str, list] = {}     # net → wires drawn to its flags
        self.flag_pins: dict[str, list] = {}      # net → the points those wires end on
        self.net_terms: dict[str, list[Terminal]] = {}
        self.open_on_power: set[str] = set()      # nets with an open driver pin and a PWR_FLAG
        self.flag_boxes: list[tuple] = []         # every flag symbol drawn, with its value text

    # ............................................................ helpers
    def pin_net(self, pl: Placed, number: str) -> str | None:
        res = pl.res
        inv = {v: k for k, v in pl.pinmap.items()}
        sem = inv.get(number)
        if sem is None:
            return None
        pins = {canonical_pin(res.part.type, k): v for k, v in res.part.pins.items()}
        if res.is_supply_unit:
            pins = res.part.pins
        pr = pins.get(sem)
        return pr.net if pr else None

    def all_placed(self) -> list[Placed]:
        return self.layout.placed + self.layout.unplaced

    def _beside_kin(self, pl: Placed) -> bool:
        """Put an IC's power unit beside the amplifier it feeds, as a hand-drawn sheet does.

        The source did not draw it anywhere, so there is nothing to be faithful to —
        and a sheet reads far better with IC1's supply above IC1 than in a row of
        anonymous power units under the drawing.
        """
        lay = self.layout
        # beside one unit, not in the middle of them all: a dual op-amp's halves can sit
        # at opposite corners, and the space between them belongs to the circuit
        kin = sorted((p for p in lay.placed if p.res.ref == pl.res.part.ref and p is not pl),
                     key=lambda p: p.res.unit)
        if not kin:
            return False
        ex0, ey0, ex1, ey1 = pl.extent()
        w, h = ex1 - ex0, ey1 - ey0
        dx, dy = (ex0 + ex1) / 2 - pl.origin[0], (ey0 + ey1) / 2 - pl.origin[1]

        def spot(k: Placed, side: int, gap: float):
            x0, y0, x1, y1 = k.extent()
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            return ((cx, y0 - gap - h / 2), (x1 + gap + w / 2, cy),
                    (x0 - gap - w / 2, cy), (cx, y1 + gap + h / 2))[side]

        # Only where its own two rails still have somewhere to hang their flags — a power
        # unit beside its amp is worth nothing if it costs the sheet two flags, and every
        # one of these pins is a rail. Among the spots that work, take the one standing in
        # the most open space: a big symbol dropped into a gap between parts is a wall the
        # router then has to find its way around.
        best, best_score = None, None
        for gi, gap in enumerate((5.08, 7.62, 10.16, 12.7, 17.78)):
            for side in (0, 1, 2, 3):
                for k in kin:
                    tx, ty = spot(k, side, gap)
                    pl.origin = (snap(tx - dx), snap(ty - dy))
                    if lay.conflicts(pl, lay.placed, margin=2.54) or not self._flag_room(pl):
                        continue
                    score = self._openness(pl) - 1.5 * gi - 0.5 * side
                    if best_score is None or score > best_score:
                        best, best_score = pl.origin, score
        if best is None:
            return False
        pl.origin = best
        return True

    def _flag_room(self, pl: Placed, reach: float = 8.9, half: float = 2.54) -> bool:
        """Is the space each pin points into clear enough for a flag symbol to stand there?

        Clear of the other parts, and on the paper. A power unit pushed past an edge
        takes its rail's flag with it, and a flag off the page prints as nothing.
        """
        exts = [p.extent(0.5) for p in self.layout.placed if p is not pl]
        px0, py0, px1, py1 = self.layout.sheet.usable
        for p in pl.pins():
            tip = pl.pin_pos(p.number)
            d = pl.pin_dir(p.number)
            far = (tip[0] + d[0] * reach, tip[1] + d[1] * reach)
            box = (min(tip[0], far[0]) - (half if d[0] == 0 else 0),
                   min(tip[1], far[1]) - (half if d[1] == 0 else 0),
                   max(tip[0], far[0]) + (half if d[0] == 0 else 0),
                   max(tip[1], far[1]) + (half if d[1] == 0 else 0))
            if any(_overlap(box, e) for e in exts):
                return False
            if box[0] < px0 or box[1] < py0 or box[2] > px1 or box[3] > py1:
                return False
        return True

    def _rails_need_room(self, pl: Placed) -> bool:
        """Is this part jammed — against a neighbour, or with a rail that has nowhere to go?"""
        return not self._flag_room(pl) or self.layout.touching(pl)

    def _room_for_supply_rails(self, radius: int = 16) -> None:
        """Ease a drawn power unit off its neighbours until its rails have room.

        A source can draw a chip's supply as a box a tenth the size of KiCad's symbol
        of it, so what the drawing shows as a tidy row lands as a staircase of touching
        boxes, each one's ground pin pointing straight into the next one's body with
        nowhere to hang a flag. A power unit is the one part whose position says
        nothing — it carries two rails and no signal — so it is the one part worth
        moving. The smallest move that frees it is the one taken: the rail that feeds
        the whole column is routed as wire, and it still has to find all of them.
        """
        lay = self.layout
        for pl in [p for p in lay.placed if p.res.is_supply_unit]:
            if not self._rails_need_room(pl):
                continue
            keep = pl.origin
            others = [p for p in lay.placed if p is not pl]
            for dx, dy in spiral(radius):
                pl.origin = (snap(keep[0] + dx * GRID), snap(keep[1] + dy * GRID))
                if not lay.conflicts(pl, others, margin=2.54) and not self._rails_need_room(pl):
                    pl.moved = True
                    break
            else:
                pl.origin = keep      # nowhere better: the sheet says it is drawn touching

    def _openness(self, pl: Placed, reach: float = 25.4) -> float:
        """How far the nearest other part is, capped: high means room to spare."""
        me = pl.extent()
        near = reach
        for o in self.layout.placed:
            if o is pl:
                continue
            e = o.extent()
            gap = math.hypot(max(me[0] - e[2], e[0] - me[2], 0.0), max(me[1] - e[3], e[1] - me[3], 0.0))
            near = min(near, gap)
        return near

    def _place_unplaced(self) -> None:
        """Supply units and spare pads have no source position: park them tidily."""
        lay = self.layout
        spare_pads = [p for p in lay.unplaced if p.res.part.type == "port"]
        others = []
        for pl in [p for p in lay.unplaced if p.res.part.type != "port"]:
            if pl.res.is_supply_unit and self._beside_kin(pl):
                lay.placed.append(pl)
            else:
                others.append(pl)
        bx0, by0, bx1, by1 = lay.bounds()
        x = bx1
        for pl in others:   # nowhere near their own IC: a tidy row under the drawing, right to left
            pl.origin = (snap(x - 7.62), snap(by1 + 17.78))
            if lay.conflicts(pl, lay.placed, margin=2.54):
                lay.find_spot(pl, lay.placed, radius=30)
            lay.placed.append(pl)
            x -= 20.32
        if spare_pads:
            # a column of pads, like the 3PDT pad array on the board, left of the drawing
            anchor = next((p for p in lay.placed if p.res.ref == spare_pads[0].res.ref), None)
            bx0, by0, bx1, by1 = lay.bounds()
            x = snap((anchor.extent()[0] if anchor else bx0) - 5.08)
            y0 = snap(anchor.origin[1] + 7.62 if anchor else by1 + 10.16)
            for shift in range(0, 40):
                ok = True
                for k, pl in enumerate(spare_pads):
                    pl.origin = (snap(x - shift * GRID), snap(y0 + k * 5.08))
                    if lay.conflicts(pl, lay.placed, margin=2.54):
                        ok = False
                        break
                if ok:
                    break
            lay.placed.extend(spare_pads)
        lay.unplaced = []

    def _orient_single_pin(self, pl: Placed, toward: tuple[float, float], number: str | None = None) -> None:
        """Turn a pad (or a socket, by pin ``number``) so its lead points at ``toward`` (sheet mm)."""
        pin = next(p for p in pl.pins() if p.number == number) if number else pl.pins()[0]
        tip = pl.pin_pos(pin.number)
        dx, dy = toward[0] - tip[0], toward[1] - tip[1]
        want = (1 if dx > 0 else -1, 0) if abs(dx) >= abs(dy) else (0, 1 if dy > 0 else -1)
        for o in (Orient(0, None), Orient(0, "y"), Orient(90, None), Orient(270, None),
                  Orient(180, None), Orient(90, "x"), Orient(0, "x"), Orient(270, "x")):
            if pin_outward(o, pin) == want:
                old_tip = tip
                pl.orient = o
                new_tip = pl.pin_pos(pin.number)
                pl.origin = (snap(pl.origin[0] + old_tip[0] - new_tip[0]), snap(pl.origin[1] + old_tip[1] - new_tip[1]))
                return

    # ................................................................ build
    def run(self, out_dir: Path) -> BuildResult:
        lay = self.layout
        extra = [t.at for t in self.c.texts if t.at] + [p.at for p in self.c.ports if p.at]
        lay.place(extra_points=extra)
        # pads face the part they wire to; a jack socket by its tip, the one pin the
        # drawing places, its ground pin beside it on the same side
        for pl in lay.placed:
            drawn = [s for s, p in pl.res.part.pins.items() if p.at is not None]
            if pl.res.part.type == "port" and len(drawn) == 1:
                net, number = pl.res.part.pins[drawn[0]].net, pl.pinmap[drawn[0]]
                mates = [p2 for p2 in lay.placed if p2 is not pl and any(
                    self.pin_net(p2, pn.number) == net for pn in p2.pins())]
                if mates:
                    tgt = min((p2.pin_pos(pn.number) for p2 in mates for pn in p2.pins()
                               if self.pin_net(p2, pn.number) == net),
                              key=lambda q: math.dist(q, pl.pin_pos(number)))
                    if len(pl.pins()) > 1:
                        # a jack comes in from the side of the box: level, toward its
                        # mate, or toward the middle when the mate is straight above
                        tip = pl.pin_pos(number)
                        if abs(tgt[0] - tip[0]) < GRID:
                            bx0, _, bx1, _ = lay.bounds()
                            tgt = ((bx0 + bx1) / 2, tip[1])
                        tgt = (tgt[0], tip[1])
                    self._orient_single_pin(pl, tgt, number)
                # a socket is two pins and a body where the drawing had a dot for a pad:
                # the Abductor's OUT is drawn on VOLUME's wiper, and the socket stood on
                # the pot and pushed its ground flag into it. Off the parts, the router
                # brings its tip back.
                others = [p2 for p2 in lay.placed if p2 is not pl]
                if len(pl.pins()) > 1 and lay.conflicts(pl, others, margin=2.54):
                    lay.find_spot(pl, others, radius=30)
        self._room_for_supply_rails()
        self._place_unplaced()
        self.w.paper = lay.sheet.paper
        bx0, by0, bx1, by1 = lay.bounds(margin=12.7)
        router = Router((bx0 - 30, by0 - 30, bx1 + 60, by1 + 60))

        # symbols and their obstacles
        insts: dict[str, SymbolInstance] = {}
        specs: list[tuple[Placed, SymbolInstance, list[str]]] = []
        for pl in lay.placed:
            res = pl.res
            inst = SymbolInstance(lib=res.symbol, ref=res.ref, unit=res.unit, at=pl.origin, orient=pl.orient,
                                  value=res.value, footprint=res.footprint, fields=dict(res.fields),
                                  dnp=res.part.dnp)
            # a knob named by its reference (PedalPCB's PRE, TONE) or a value that only
            # repeats the reference ("LED") is not shown twice
            names = (["Reference"] + (["Value"] if inst.value != res.ref else [])
                     + (["Label"] if "Label" in inst.fields and inst.fields["Label"] != res.ref else []))
            if res.part.type == "port" and res.ref.startswith("Switch_Connectors"):
                names = []
                inst.hidden_fields |= {"Reference", "Value"}
            specs.append((pl, inst, names))

        # every body and pin line is known before the first field is placed, so a
        # reference can be put where it does not land on another part. Small symbols
        # choose first: a resistor has fewer places to put its text than an IC has.
        parts_boxes = [_grow(pl.box(), 0.3) for pl in lay.placed] + [b for pl in lay.placed for b in _pin_boxes(pl)]
        for pl, inst, names in sorted(specs, key=lambda s: _area(s[0].box())):
            if not names:
                continue
            dirs = [pl.pin_dir(p.number) for p in pl.pins()]
            inst.field_pos = field_positions(inst, pl.box(), dirs, names,
                                             blocked=parts_boxes + self.text_boxes)
            for k, at in inst.field_pos.items():   # flags and wires keep off this text
                self.text_boxes.append(_text_box(at, _field_text(inst, k)))

        for pl, inst, names in specs:
            for k in list(inst.fields) + ["Value"]:
                if k not in names:
                    inst.hidden_fields.add(k)
            self.w.add_symbol(inst)
            pl.uuid = inst.uuid
            insts[pl.key] = inst
            router.block_box(pl.box(), owner=pl.key)
            for p in pl.pins():
                router.block_pin(pl.pin_pos(p.number), pl.pin_dir(p.number), p.length, self.pin_net(pl, p.number))
        for pl in lay.placed:
            for p in pl.pins():
                router.open_escape(pl.pin_pos(p.number), pl.pin_dir(p.number), pl.key)
        for box in self.text_boxes:
            router.avoid(box)   # a wire may cross a value, but pays for it

        # terminals per net
        terms: dict[str, list[Terminal]] = {}
        owner_part: dict[str, Placed] = {}
        for pl in lay.placed:
            for p in pl.pins():
                net = self.pin_net(pl, p.number)
                pos = pl.pin_pos(p.number)
                owner = f"{pl.res.ref}.{p.number}"
                owner_part[owner] = pl
                if net is None:
                    if p.etype not in ("no_connect",) and not p.hidden:
                        u = self.w.no_connect(pos)
                        self.flag_items.setdefault(pl.key, []).append(u)
                    continue
                terms.setdefault(net, []).append(Terminal(pos, pl.pin_dir(p.number), owner))

        pending_labels = self._label_terminals(terms, router)

        # power and ground nets drawn with flags
        self.net_terms = terms
        flagged = [n for n in terms if self._draw_as_flags(n)]
        by_flag = set(flagged)
        orphans: dict[str, list[Terminal]] = {}
        for net in flagged:
            lead, follow = self._flag_leaders(net, terms[net])
            pending = [t for t in lead
                       if not self._attach_flag(net, t, router, owner_part.get(t.owner),
                                                label_fail=False, first=True)]
            if pending + follow:
                orphans[net] = pending + follow
        self._pwr_flags(terms, router)

        router.guide = self._guides(terms)
        # route everything else, shortest nets first
        routed: dict[str, NetRoute] = {}
        order = sorted((n for n in terms if n not in flagged and len(terms[n]) >= 2),
                       key=lambda n: (len(terms[n]), _hpwl(terms[n])))
        for net in order:
            r = router.route_net(net, terms[net])
            if r.failed_owners:
                # a pin whose lead points straight into a neighbour's body has one move
                # left: turn where it stands. A wire that turns at a pin still says what
                # is connected to what, which two net labels only claim
                router.rip_up(net)
                r = router.route_net(net, terms[net], turn_at_tip=True)
            if r.failed_owners:
                router.rip_up(net)
                r = NetRoute(net, labelled=True)
                self.labelled.append(net)
            routed[net] = r
        boundary = {port.net for port in self.label_ports}
        for net, r in routed.items():
            if r.labelled:
                for t in terms[net]:
                    if net in boundary:
                        # a block's boundary net: its global label is already the label, and
                        # the pins take global labels too (a local label of the same name
                        # would be a different net in KiCad's eyes)
                        if t.owner.startswith("label:"):
                            continue
                        u = self.w.global_label(net, t.pos, t.out)
                    else:
                        u = self.w.label(net, t.pos, t.out)
                    self.term_owner_items.setdefault(t.owner, []).append(u)
                for a, b in self.flag_stubs.get(net, []):
                    self.net_items.setdefault(net, []).append(self.w.wire(a, b))
                continue
            stubs = [t.stub for t in terms[net] if t.stub]
            segs, juncs = normalise(r.segments + stubs + self.flag_stubs.get(net, []),
                                    [t.pos for t in terms[net]] + self.flag_pins.get(net, []))
            uu = [self.w.wire(a, b) for a, b in segs] + [self.w.junction(j) for j in juncs]
            self.net_items.setdefault(net, []).extend(uu)
        for net, stub_segs in self.flag_stubs.items():
            if net not in routed and net not in by_flag:   # never reached the router
                for a, b in stub_segs:
                    self.net_items.setdefault(net, []).append(self.w.wire(a, b))
        for net in flagged:
            # signal routes have had their pick of the space; now a pin left without a
            # flag of its own may reach for one this net already has
            self._share_flags(net, orphans.get(net, []), router)
            segs, juncs = normalise(self.flag_stubs.get(net, []),
                                    [t.pos for t in terms[net]] + self.flag_pins.get(net, []))
            self.net_items.setdefault(net, []).extend(
                [self.w.wire(a, b) for a, b in segs] + [self.w.junction(p) for p in juncs])
            # after normalise every join is end to end, so a pin the net reaches is an end
            ends = {(round(p[0], 3), round(p[1], 3)) for seg in segs for p in seg}
            for t in orphans.get(net, []):
                if (round(t.pos[0], 3), round(t.pos[1], 3)) not in ends:
                    self._label_pin(net, t)
        for name, anchor, text_dir in pending_labels:
            if not (name in routed and routed[name].labelled):   # its pins carry the label
                self.w.global_label(name, anchor, text_dir)
        # dangling single-terminal nets that are not ports: mark them for the report
        for net, ts in terms.items():
            if len(ts) == 1 and net not in flagged:
                self.problems.append(f"net {net} connects to only {ts[0].owner}")

        self._reflow_fields(specs)
        tight = lay.cramped()
        if tight:
            self.notes.append(
                "drawn touching, because the source draws these far smaller than KiCad's "
                "symbols are: " + ", ".join(f"{a}/{b}" for a, b in tight)
                + ". Pull them apart in KiCad if you want the room. A power unit is eased "
                "apart by the builder, since where one sits means nothing; these are parts "
                "the drawing put where they are on purpose, so they stay.")
        if self.open_on_power:
            self.notes.append(
                "the house transistor symbols call collector and emitter open drivers, which "
                "KiCad's default ERC reads as a conflict with the PWR_FLAG on "
                f"{', '.join(sorted(self.open_on_power))}; the project's ERC matrix allows that "
                "one pair, so a grounded emitter is not an error")
        self._texts()
        self._groups(terms)
        tb = dict(self.c.title_block)
        comments = list(tb.get("comments", []))
        if tb.get("author"):
            comments.insert(0, f"Author: {tb['author']}")
        src = self.c.source.get("file")
        if src:
            comments.append(f"Converted from {src} by sch2kicad")
        tb["comments"] = comments
        self.w.title_block = tb

        out_dir.mkdir(parents=True, exist_ok=True)
        sch = out_dir / f"{self.project}.kicad_sch"
        pro = out_dir / f"{self.project}.kicad_pro"
        self.w.write(sch)
        write_project(pro, self.w.root_uuid, allow_open_on_power=bool(self.open_on_power))
        copy_bundled_symbols(out_dir, self.w.lib_symbols)
        _kicad_resave(sch)
        expected = self.expected_netlist()
        moved = [p.res.ref for p in lay.placed if p.moved and p.res.part.type != "port"]
        return BuildResult(sch=sch, pro=pro, expected=expected, problems=self.problems, notes=self.notes,
                           labelled_nets=self.labelled, moved=sorted(set(moved)),
                           uncertain=[u.note for u in self.c.uncertain])

    def _guides(self, terms: dict[str, list[Terminal]]) -> dict[str, set[Cell]]:
        """The grid cells each net's drawn wires fall on, for the router to follow.

        The drawing's wires come through the same linear map as its parts, but each
        part is then snapped and centred on its own pins, so a wire can land a grid
        step beside the pin it leads to. A line that comes within reach of one of its
        net's pins is moved onto that pin's column or row, and a line that comes near
        none of them is left out: following a guide that is out of true would put in
        the very jog the guide is there to take out.
        """
        from .trace import drawn_wires
        try:
            drawn = drawn_wires(self.c)
        except FileNotFoundError:       # no source beside the reading: route as before
            return {}
        out: dict[str, set[Cell]] = {}
        for net, segs in drawn.items():
            ts = terms.get(net, [])
            if len(ts) < 2 or self._draw_as_flags(net):
                continue        # a rail is a flag at each pin here, whatever bus the drawing ran
            cells: set[Cell] = set()
            for sg in segs:
                a, b = self.layout.to_sheet(sg.a), self.layout.to_sheet(sg.b)
                axis = 1 if abs(a[1] - b[1]) < abs(a[0] - b[0]) else 0   # the coordinate it holds
                held = (a[axis] + b[axis]) / 2
                near = [t.pos[axis] for t in ts if abs(t.pos[axis] - held) <= 1.5 * GRID]
                if not near:
                    continue        # lands between pins: following it can only add a jog
                held = min(near, key=lambda v: abs(v - held))
                k = int(round(held / GRID))
                lo, hi = sorted((int(round(a[1 - axis] / GRID)), int(round(b[1 - axis] / GRID))))
                cells.update((k, j) if axis == 0 else (j, k) for j in range(lo, hi + 1))
            out[net] = cells
        return out

    # ........................................................ global labels
    def _label_terminals(self, terms: dict[str, list[Terminal]], router: Router) -> list:
        """Block files: each net leaving the block ends in a global label, placed where it leaves."""
        pending = []
        for port in self.label_ports:
            ts = terms.get(port.net, [])
            if not ts or port.at is None:
                continue
            want = self.layout.to_sheet(port.at)
            near = min(ts, key=lambda t: math.dist(t.pos, want))
            # just off that pin, pointing away from the part: a block is spread out to fit
            # its symbols, so where the net leaves it in the source says little
            text_dir = near.out
            anchor = (snap(near.pos[0] + text_dir[0] * 2 * GRID),
                      snap(near.pos[1] + text_dir[1] * 2 * GRID))
            for _ in range(40):
                c = cell_of(anchor)
                # free, and far enough from the net's own pins to leave room for a wire
                room = min(abs(c[0] - t.cell[0]) + abs(c[1] - t.cell[1]) for t in ts) >= 2
                if room and c not in router.hard and c not in router.pin_net and c not in router.occ:
                    break
                anchor = (snap(anchor[0] + text_dir[0] * GRID), snap(anchor[1] + text_dir[1] * GRID))
            length = 2.54 + 1.0 * len(port.name)
            x2, y2 = anchor[0] + text_dir[0] * length, anchor[1] + text_dir[1] * length
            # the label's body starts past its connection point, so wires can still reach
            # that point from the sides
            sx, sy = anchor[0] + text_dir[0] * GRID * 0.75, anchor[1] + text_dir[1] * GRID * 0.75
            box = (min(sx, x2), min(sy, y2), max(sx, x2), max(sy, y2))
            box = (box[0] - (1.27 if text_dir[0] == 0 else 0), box[1] - (1.27 if text_dir[1] == 0 else 0),
                   box[2] + (1.27 if text_dir[0] == 0 else 0), box[3] + (1.27 if text_dir[1] == 0 else 0))
            c0 = cell_of(anchor)
            router.block_box(box, inflate=0.2)
            router.hard.discard(c0)
            router.pin_net[c0] = port.net
            terms.setdefault(port.net, []).append(
                Terminal(anchor, (-text_dir[0], -text_dir[1]), f"label:{port.name}"))
            pending.append((port.name, anchor, text_dir))
        return pending

    # ............................................................... flags
    def _draw_as_flags(self, net: str) -> bool:
        n = self.c.net(net)
        if n.draw:
            return n.draw == "flag"
        return n.kind in ("power", "ground")

    def _flag_symbol(self, net: str) -> tuple[LibSymbol, str]:
        kind = self.c.net_kind(net)
        lib_id = self.profile.flag_for("GND" if kind == "ground" and net.upper() in ("GND", "0V") else net)
        sym = self.symbols.get(lib_id)
        return sym, net

    def _attach_flag(self, net: str, t: Terminal, router: Router, pl: Placed | None,
                     sym: LibSymbol | None = None, ref: str = "#PWR?",
                     label_fail: bool = True, first: bool = False) -> bool:
        """Hang a power flag off a pin: upright if at all possible, like a hand-drawn sheet.

        ``first`` is the pass before any signal is routed, when the pin has a better
        answer still to come — a wire to a flag this rail already has. So it takes no
        flag turned to face the pin (a ground symbol standing on its head reads as
        anything but ground) and none that would stand beside one of its own rail's:
        two symbols a few millimetres apart say one node twice. Either way the pin
        waits for ``_share_flags``, which turns or crowds a flag only if nothing joins.
        """
        if sym is None:
            sym, value = self._flag_symbol(net)
        else:
            value = sym.props.get("Value", "")
        pin = sym.pins_for(1)[0]
        body_lib = (-pin.outward()[0], -pin.outward()[1])     # from the pin into the body
        upright = Orient(0, None)
        up_dir = tuple(int(round(v)) for v in apply(upright.m, *body_lib))

        def attempt(path: list[tuple[float, float]], o: Orient, over_text: bool):
            if _doubles_back(path):
                return None
            tip = path[-1]
            dx, dy = apply(o.m, pin.x, pin.y)
            origin = (round(tip[0] - dx, 4), round(tip[1] - dy, 4))
            box = _flag_box(sym, origin, o, value)
            if not self._box_free(box, router, net, None, 0) or not self._path_free(path, router, net):
                return None
            if not over_text and any(_overlap(box, tb) for tb in self.text_boxes):
                return None
            if first and any(math.dist(tip, f) < SHARE_MM for f in self.flag_pins.get(net, [])):
                return None
            return (path, o, origin, box)

        def pt(p, d, k):
            return (round(p[0] + d[0] * k, 4), round(p[1] + d[1] * k, 4))

        near, far = (2.54, 3.81, 5.08), (2.54, 3.81, 5.08, 7.62)
        cands = []
        if t.out == up_dir:                        # pin already points the flag's way
            cands += [([t.pos, pt(t.pos, t.out, s)], upright) for s in (2.54, 3.81, 5.08, 1.27, 7.62)]
        elif t.out[1] == 0:                        # sideways pin: out, then turn to stand the flag up
            cands += [([t.pos, pt(t.pos, t.out, s1), pt(pt(t.pos, t.out, s1), up_dir, s2)], upright)
                      for s1 in far for s2 in far]
        # step aside and come back to upright: the only way for a pin that points the
        # wrong way, and a second chance for the rest when something is in the way
        for s1 in near:
            p1 = pt(t.pos, t.out, s1)
            for side in ((1, 0), (-1, 0)) if t.out[1] else ((0, -1), (0, 1)):
                for s2 in far:
                    p2 = pt(p1, side, s2)
                    cands += [([t.pos, p1, p2, pt(p2, up_dir, s3)], upright) for s3 in near]
        for s in () if first else (2.54, 3.81, 5.08, 1.27, 7.62):   # last resort: face the pin
            for o in (Orient(a, None) for a in (0, 90, 180, 270)):
                bd = apply(o.m, *body_lib)
                if (round(bd[0]), round(bd[1])) == t.out:
                    cands.append(([t.pos, pt(t.pos, t.out, s)], o))
        best = None
        # a flag over a value is fixable — the fields move afterwards — so it beats no flag
        for over_text in (False, True):
            for path, o in cands:
                best = attempt(path, o, over_text)
                if best:
                    break
            if best:
                break
        if best is None:
            if label_fail and ref == "#PWR?":  # nothing free: label the pin instead
                self._label_pin(net, t)
            return False
        path, o, origin, box = best
        inst = SymbolInstance(lib=sym, ref=ref, unit=1, at=origin, orient=o, value=value)
        inst.field_pos = _lib_field_positions(sym, inst.at, o)
        self.w.add_symbol(inst)
        # something may join this stub anywhere along it — another pin's route, or a pin
        # of this same net with no room for a flag of its own — so the stub is drawn with
        # the net's other wires, at the end, where joins get split end to end
        for a, b in zip(path, path[1:]):
            self.flag_stubs.setdefault(net, []).append((a, b))
            router.add_segment(net, cell_of(a), cell_of(b))
        self.flag_pins.setdefault(net, []).append(path[-1])
        self.term_owner_items.setdefault(t.owner, []).append(inst.uuid)
        router.block_box(box, inflate=0.3)
        router.pin_net[cell_of(path[-1])] = net
        self.flag_boxes.append(box)
        return True

    def _flag_leaders(self, net: str, ts: list[Terminal]) -> tuple[list[Terminal], list[Terminal]]:
        """Which of a rail's pins stand a flag of their own, and which share a neighbour's.

        Two pins of one rail a few millimetres apart are one node, and a pair of symbols
        that close reads as two — the Tri-Vibe had VB arrows in step beside each other on
        an op-amp's output and the cap across it. A drawn sheet joins such pins and hangs
        one flag off the pair. The pin that keeps it is the one already pointing the way
        the symbol stands, so the flag needs no dog-leg to get upright.
        """
        sym, _ = self._flag_symbol(net)
        pin = sym.pins_for(1)[0]
        up = tuple(int(round(v)) for v in apply(Orient(0, None).m, -pin.outward()[0], -pin.outward()[1]))
        lead: list[Terminal] = []
        follow: list[Terminal] = []
        for t in sorted(ts, key=lambda t: (t.out != up, t.pos[1], t.pos[0])):
            (follow if any(math.dist(t.pos, u.pos) <= SHARE_MM for u in lead) else lead).append(t)
        return lead, follow

    def _label_pin(self, net: str, t: Terminal) -> None:
        u = self.w.label(net, t.pos, t.out)
        self.term_owner_items.setdefault(t.owner, []).append(u)

    def _share_flags(self, net: str, orphans: list[Terminal], router: Router) -> None:
        """Wire a pin with no room for a flag of its own to one of this net's that has one.

        Two ground pins facing each other across a few millimetres is the commonest way
        to run out of room: neither has anywhere to stand a symbol, and each on its own
        can only give up and take a net label. A sheet drawn by hand answers this without
        thinking — join them, and let one ground symbol speak for both. Which is also the
        honest drawing: they are one node, and a wire says so where two labels only claim
        it. The join may land on another pin, on a flag's stub or on a wire this pass has
        already added, so every one of them is a place a later orphan can reach too.
        """
        for t in orphans:
            # only a terminal the net's wires actually reach can be shared with: route_net
            # seeds its tree from those cells, and would go drawing a wire to any other
            cells = router.net_cells.get(net, set())
            if t.cell in cells:
                # already on the rail: a neighbour's flag stub runs through this pin
                # (two pins facing each other, one lead's length apart)
                continue
            reach = [u for u in self.net_terms[net] if u is not t and u.cell in cells]
            # along its own lead first, as a wire should read; failing that, turning at
            # the tip, which is all a pin walled in on its own side can do
            r = router.route_net(net, [t, *reach]) if reach else None
            if r is None or r.failed_owners:
                r = router.route_net(net, [t, *reach], turn_at_tip=True) if reach else None
            if r is None or r.failed_owners or not r.segments:
                # no neighbour to share with, or no way through to one: stand its own
                self._attach_flag(net, t, router, None, label_fail=False)
                continue
            self.flag_stubs.setdefault(net, []).extend(r.segments)
            if t.stub:                       # an off-grid pin: reach its own grid point
                self.flag_stubs[net].append(t.stub)

    def _path_free(self, path, router: Router, net: str) -> bool:
        cells = []
        for a, b in zip(path, path[1:]):
            ca, cb = cell_of(a), cell_of(b)
            dx = (cb[0] > ca[0]) - (cb[0] < ca[0])
            dy = (cb[1] > ca[1]) - (cb[1] < ca[1])
            n = max(abs(cb[0] - ca[0]), abs(cb[1] - ca[1]))
            cells += [(ca[0] + dx * k, ca[1] + dy * k) for k in range(1, n + 1)]
        for c in cells:
            if c in router.hard or (c in router.pin_net and router.pin_net[c] != net) \
                    or any(n != net for n in router.occ.get(c, {})):
                return False
        return True

    def _box_free(self, box, router: Router, net: str, t, stub: float) -> bool:
        x0, y0, x1, y1 = box
        for i in range(math.floor(x0 / GRID), math.ceil(x1 / GRID) + 1):
            for j in range(math.floor(y0 / GRID), math.ceil(y1 / GRID) + 1):
                c = (i, j)
                if c in router.hard:
                    return False
                if c in router.pin_net and router.pin_net[c] != net:
                    return False
                if any(n != net for n in router.occ.get(c, {})):
                    return False
        return True

    def _pwr_flags(self, terms: dict[str, list[Terminal]], router: Router) -> None:
        """ERC wants every power input driven: one PWR_FLAG per net that has power-input pins."""
        need = []
        for net in terms:
            types = {p.etype for pl in self.layout.placed for p in pl.pins() if self.pin_net(pl, p.number) == net}
            # a net an output already drives (an op-amp buffering VREF) gets no PWR_FLAG:
            # that would be an output-to-power-output conflict; verify explains the ERC note
            if (self._draw_as_flags(net) or "power_in" in types) and not types & {"output", "power_out"}:
                need.append(net)
                if types & OPEN_PINS:
                    # the house transistor symbols call their emitter and collector open
                    # drivers, and KiCad's default matrix calls those a conflict with the
                    # PWR_FLAG this net needs — which every grounded emitter and every
                    # collector on the supply would raise
                    self.open_on_power.add(net)
        if not need:
            return
        pwr = self.symbols.get(self.profile.pwr_flag)
        bx0, by0, bx1, by1 = self.layout.bounds()
        x = snap(bx0)
        y = snap(by1 + 15.24)
        for net in need:
            if not self._draw_as_flags(net):
                # a wired net: hang the PWR_FLAG off one of its pins, preferring one facing up
                cands = sorted(terms[net], key=lambda t: (t.out != (0, -1), t.pos[1], t.pos[0]))
                if not any(self._attach_flag(net, t, router, None, sym=pwr, ref="#FLG?") for t in cands):
                    self.problems.append(f"net {net}: no room for a PWR_FLAG; ERC will flag its power inputs")
                continue
            sym, value = self._flag_symbol(net)
            # net flag and PWR_FLAG side by side, joined by a short wire
            fpin = sym.pins_for(1)[0]
            o = Orient(0, None)
            f_origin = (x - fpin.x, y + fpin.y)
            inst = SymbolInstance(lib=sym, ref="#PWR?", unit=1, at=f_origin, orient=o, value=value)
            inst.field_pos = _lib_field_positions(sym, f_origin, o)
            ppin = pwr.pins_for(1)[0]
            p_origin = (x + 7.62 - ppin.x, y + ppin.y)
            pinst = SymbolInstance(lib=pwr, ref="#FLG?", unit=1, at=p_origin, orient=o, value="PWR_FLAG")
            pinst.field_pos = _lib_field_positions(pwr, p_origin, o)
            self.w.add_symbol(inst)
            self.w.wire((x, y), (x + 7.62, y))
            self.w.add_symbol(pinst)
            pair = (x - 2.54, y - 6.35, x + 10.16, y + 6.35)
            router.block_box(pair, inflate=0.3)
            self.flag_boxes.append(pair)
            x += 20.32

    # ........................................................ field reflow
    def _reflow_fields(self, specs) -> None:
        """Move any field text a wire ended up crossing.

        Placing text before routing can only guess where the wires will go; once they
        are drawn it is no longer a guess. Text carries no connection, so moving it
        cannot change the netlist — which is why this runs last rather than the router
        being made to treat text as a wall.
        """
        wire_boxes = [_grow(_seg_box(a, b), 0.25) for a, b in self.w.wires]
        world = list(self.flag_boxes) + wire_boxes
        own: dict[int, list] = {}
        for pl, inst, names in specs:
            own[id(inst)] = [pl.box()] + _pin_boxes(pl)
            world += own[id(inst)]
        mine: dict[int, list] = {id(inst): _inst_boxes(inst) for _, inst, _ in specs}
        for pl, inst, names in specs:
            if not inst.field_pos:
                continue
            skip = {id(b) for b in own[id(inst)]}
            around = ([b for b in world if id(b) not in skip]
                      + [b for k, v in mine.items() if k != id(inst) for b in v])
            if not any(_overlap(m, b) for m in mine[id(inst)] for b in around):
                continue
            dirs = [pl.pin_dir(p.number) for p in pl.pins()]
            inst.field_pos = field_positions(inst, pl.box(), dirs, names, blocked=around)
            mine[id(inst)] = _inst_boxes(inst)
        self.text_boxes = [b for v in mine.values() for b in v]

    # ............................................................... texts
    def _drawn_boxes(self) -> list[tuple]:
        """Everything already on the sheet that a free-standing text should not land on."""
        return ([pl.box() for pl in self.layout.placed] + list(self.text_boxes)
                + list(self.flag_boxes)
                + [b for pl in self.layout.placed for b in _pin_boxes(pl, 0.2)])

    def _place_note(self, text: str, at: tuple[float, float], blocked: list[tuple]) -> tuple[float, float]:
        """A voltage or a note belongs at the node it annotates; nudge it until it is legible."""
        box = _note_box(at, text)
        if not any(_overlap(box, b) for b in blocked) and not _crosses(self.w.wires, box):
            return at
        for r in (1, 2, 3, 4, 5, 6):
            for dx, dy in ((0, -1), (0, 1), (1, 0), (-1, 0), (1, -1), (-1, -1), (1, 1), (-1, 1)):
                p = (snap(at[0] + dx * r * GRID, 0.635), snap(at[1] + dy * r * GRID, 0.635))
                box = _note_box(p, text)
                if not any(_overlap(box, b) for b in blocked) and not _crosses(self.w.wires, box):
                    return p
        return at

    def _texts(self) -> None:
        lay = self.layout
        blocked = self._drawn_boxes()
        for t in self.c.texts:
            if t.at is None:
                continue
            p = lay.to_sheet(t.at)
            p = (snap(p[0], 0.635), snap(p[1], 0.635))
            p = self._place_note(t.text, p, blocked)
            color = GREY if t.kind == "voltage" else None
            self.w.text(t.text, p, size=1.27, color=color, italic=t.kind == "voltage")
            blocked.append(_note_box(p, t.text))
        bx0, by0, bx1, by1 = lay.bounds()
        # under everything, flags and parked power units included — not just under the parts
        y = snap(max([by1 + 12.7] + [b[3] + 10.16 for b in self.flag_boxes]))
        if self.c.notes:
            # wrapped so a long note doesn't run far past the drawing
            lines = [ln for n in self.c.notes
                     for ln in textwrap.wrap(n, 110, subsequent_indent="   ") or [""]]
            self.w.text("NOTES:\n" + "\n".join(lines), (snap(bx0), y), size=1.27, bold=False,
                        justify=("left", "top"))
            y += 2.54 * (len(lines) + 2)
        for u in self.c.uncertain:
            label = "CHECK: " + u.note
            if u.at:
                p = lay.to_sheet(u.at)
                self.w.text("?", (snap(p[0], 0.635), snap(p[1], 0.635)), size=2.0, color=RED, bold=True)
            self.w.text(label, (snap(bx0), y), size=1.27, color=RED, justify=("left", "top"))
            y += 2.54

    # .............................................................. groups
    def _groups(self, terms: dict[str, list[Terminal]]) -> None:
        owner_part: dict[str, Placed] = {}
        for pl in self.layout.placed:
            for p in pl.pins():
                owner_part[f"{pl.res.ref}.{p.number}"] = pl
        for blk in self.c.blocks:
            names = set(blk.parts)
            inside = [pl for pl in self.layout.placed if self._in_block(pl, names)]
            members: list[str] = []
            for pl in inside:
                members.append(pl.uuid)
                members += self.flag_items.get(pl.key, [])
            ids = {id(pl) for pl in inside}
            for owner, items in self.term_owner_items.items():
                pl = owner_part.get(owner)
                if pl is not None and id(pl) in ids:
                    members += items
            for net, items in self.net_items.items():
                owners = [owner_part.get(t.owner) for t in terms.get(net, [])]
                if owners and all(o is not None and id(o) in ids for o in owners):
                    members += items
            self.w.group(blk.name, members)

    def _in_block(self, pl: Placed, names: set[str]) -> bool:
        part = pl.res.part
        if part.ref in names or part.key in names:
            return True
        if pl.res.is_supply_unit:   # the power unit travels with the unit that listed "supply"
            return any(p.ref == part.ref and p.supply and (p.key in names) for p in self.c.parts)
        return False

    # ........................................................... expected
    def expected_netlist(self) -> dict[str, list[str]]:
        nets: dict[str, set[str]] = {}
        for pl in self.layout.placed:
            for p in pl.pins():
                net = self.pin_net(pl, p.number)
                node = f"{pl.res.ref}.{p.number}"
                key = net if net is not None else f"__nc__{node}"
                nets.setdefault(key, set()).add(node)
        return {k: sorted(v) for k, v in sorted(nets.items())}


def _hpwl(ts: list[Terminal]) -> float:
    xs = [t.pos[0] for t in ts]
    ys = [t.pos[1] for t in ts]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def _doubles_back(path: list[tuple[float, float]]) -> bool:
    """Does this path turn back along itself?

    Stepping aside and returning on the same axis draws one wire over another. Two
    wires of one net on the same line are harmless to the netlist and invisible on the
    sheet, so nothing caught it — until the wires of a rail started going through
    normalise(), which drops the duplicate and leaves the longer one's far end in the
    air. It was never a path worth drawing: it ends up where a shorter one would.
    """
    steps = [((b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1]))
             for a, b in zip(path, path[1:])]
    return any(n == (-d[0], -d[1]) for d, n in zip(steps, steps[1:]))


def _flag_box(sym: LibSymbol, origin, o: Orient, value: str = ""):
    x0, y0, x1, y1 = sym.body_bbox(1)
    pts = [apply(o.m, x, y) for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))]
    xs = [origin[0] + p[0] for p in pts]
    ys = [origin[1] + p[1] for p in pts]
    box = (min(xs), min(ys), max(xs), max(ys))
    at = _lib_field_positions(sym, origin, o).get("Value")
    if at and value:    # the value text ("GND") drawn beside the symbol
        box = _union(box, _text_box(at, value))
    return box


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _grow(box, m: float):
    return (box[0] - m, box[1] - m, box[2] + m, box[3] + m)


def _seg_box(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _pin_boxes(pl: Placed, half: float = 0.5) -> list[tuple[float, float, float, float]]:
    """A thin box along each pin's line, so text is not laid over the pin marks."""
    out = []
    for p in pl.pins():
        tip = pl.pin_pos(p.number)
        d = pl.pin_dir(p.number)
        root = (tip[0] - d[0] * p.length, tip[1] - d[1] * p.length)
        out.append((min(tip[0], root[0]) - half, min(tip[1], root[1]) - half,
                    max(tip[0], root[0]) + half, max(tip[1], root[1]) + half))
    return out


def _note_box(at: tuple[float, float], text: str, size: float = 1.27):
    """Extent of a left-justified, vertically centred free-standing text."""
    lines = str(text).split("\n")
    w = max((len(ln) for ln in lines), default=1) * size * 0.75
    h = len(lines) * size * 1.2
    return (at[0] - 0.3, at[1] - h / 2, at[0] + w + 0.3, at[1] + h / 2)


def _crosses(wires, box) -> bool:
    x0, y0, x1, y1 = box[0] + 0.3, box[1] + 0.3, box[2] - 0.3, box[3] - 0.3
    if x1 <= x0 or y1 <= y0:
        return False
    for a, b in wires:
        if abs(a[1] - b[1]) < 1e-6:
            if y0 < a[1] < y1 and min(a[0], b[0]) < x1 and x0 < max(a[0], b[0]):
                return True
        elif abs(a[0] - b[0]) < 1e-6:
            if x0 < a[0] < x1 and min(a[1], b[1]) < y1 and y0 < max(a[1], b[1]):
                return True
    return False


def _inst_boxes(inst: SymbolInstance) -> list[tuple]:
    return [_text_box(at, _field_text(inst, k)) for k, at in inst.field_pos.items()]


def _field_text(inst: SymbolInstance, name: str) -> str:
    if name == "Reference":
        return inst.ref
    if name == "Value":
        return inst.value
    return inst.fields.get(name, "")


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _lib_field_positions(sym: LibSymbol, at, o: Orient) -> dict[str, tuple[float, float]]:
    from . import sexpr
    out = {}
    for p in sexpr.children(sym.node, "property"):
        a = sexpr.child(p, "at")
        if a is None:
            continue
        dx, dy = apply(o.m, float(a[1]), float(a[2]))
        out[str(p[1])] = (round(at[0] + dx, 4), round(at[1] + dy, 4))
    return out


def _kicad_resave(sch: Path) -> None:
    """Let KiCad re-save the file through its own writer (normalises everything)."""
    try:
        cli = kicad_cli()
    except FileNotFoundError:
        return
    r = subprocess.run([str(cli), "sch", "upgrade", "--force", str(sch)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"KiCad could not load the generated schematic:\n{r.stdout}\n{r.stderr}")


def build(circuit_path: Path, out_dir: Path, profile: str = "tht", name: str | None = None,
          spacing: float = 1.15) -> BuildResult:
    circuit = load(circuit_path)
    prof = load_profile(profile)
    project = safe_name(name or circuit.name)
    b = Builder(circuit, prof, project, spacing=spacing)
    result = b.run(out_dir)
    write_build_json(b, result, circuit_path, out_dir)
    return result


def write_build_json(b: "Builder", result: BuildResult, circuit_path: Path, out_dir: Path,
                     extra: dict | None = None) -> None:
    """What verify checks the schematic against."""
    meta = {
        "circuit": str(circuit_path.resolve()),
        "profile": b.profile.name,
        "expected": result.expected,
        "problems": result.problems,
        "notes": result.notes,
        "labelled_nets": result.labelled_nets,
        "moved": result.moved,
        "uncertain": result.uncertain,
        "pinouts": {r.ref: r.pinout for r in b.resolved if r.pinout},
        "pads": sorted({r.ref for r in b.resolved if r.part.type == "port"}),
        # the supplies the reading declares, so a chip's GND pin on one reads as meant
        "rails": sorted(n for n in b.c.all_net_names() if b.c.net_kind(n) == "power"),
        **(extra or {}),
    }
    if b.c.bom:
        # verify compares the values KiCad reports for these parts with the doc's parts list
        meta["bom"] = {"rows": b.c.bom, "accept": b.c.bom_accept,
                       "parts": {p.ref: {"type": p.type, "label": p.label} for p in b.c.parts}}
    work = out_dir / "sch2kicad"
    work.mkdir(exist_ok=True)
    (work / "build.json").write_text(json.dumps(meta, indent=2) + "\n")
