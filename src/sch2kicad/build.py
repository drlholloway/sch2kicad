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
from .geometry import GRID, Layout, Orient, Placed, apply, fit_orientation, pin_outward, pin_world, snap
from .kicad_env import kicad_cli
from .parts import Profile, Resolved, canonical_pin, load_profile, resolve_all
from .router import NetRoute, Router, Terminal, cell_of, normalise
from .schematic import SchematicWriter, SymbolInstance, field_positions, write_project
from .symlib import LibSymbol, SymbolResolver

RED = (200, 0, 0, 1)
GREY = (90, 90, 90, 1)


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
    array_id = cfg.get("array")
    array_pins = {k.upper(): v for k, v in (cfg.get("array_pins") or {}).items()}
    aliases = {k.upper(): v.upper() for k, v in (cfg.get("aliases") or {}).items()}
    used_array: dict[str, str] = {}
    pad_n = 0
    for port in circuit.ports:
        name = port.name.strip()
        key = aliases.get(name.upper(), name.upper())
        if array_id and key in array_pins and key not in used_array and key != "GND":
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
        self.flag_stubs: dict[str, list] = {}     # routed net → PWR_FLAG stub segments
        self.flag_pins: dict[str, list] = {}      # routed net → PWR_FLAG pin points

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

    def _place_unplaced(self) -> None:
        """Supply units and spare pads have no source position: park them tidily."""
        lay = self.layout
        spare_pads = [p for p in lay.unplaced if p.res.part.type == "port"]
        others = [p for p in lay.unplaced if p.res.part.type != "port"]
        bx0, by0, bx1, by1 = lay.bounds()
        x = bx1
        for pl in others:   # op-amp power units: a tidy row under the drawing, right to left
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

    def _orient_single_pin(self, pl: Placed, toward: tuple[float, float]) -> None:
        """Turn a one-pin symbol (pad) so its lead points at ``toward`` (sheet mm)."""
        pin = pl.pins()[0]
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
        # pads face the part they wire to
        for pl in lay.placed:
            if pl.res.part.type == "port" and len(pl.pins()) == 1:
                net = pl.res.part.pins["1"].net
                mates = [p2 for p2 in lay.placed if p2 is not pl and any(
                    self.pin_net(p2, pn.number) == net for pn in p2.pins())]
                if mates:
                    tgt = min((p2.pin_pos(pn.number) for p2 in mates for pn in p2.pins()
                               if self.pin_net(p2, pn.number) == net),
                              key=lambda q: math.dist(q, pl.pin_pos(pl.pins()[0].number)))
                    self._orient_single_pin(pl, tgt)
        self._place_unplaced()
        self.w.paper = lay.sheet.paper
        bx0, by0, bx1, by1 = lay.bounds(margin=12.7)
        router = Router((bx0 - 30, by0 - 30, bx1 + 60, by1 + 60))

        # symbols and their obstacles
        insts: dict[str, SymbolInstance] = {}
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
            dirs = [pl.pin_dir(p.number) for p in pl.pins()]
            inst.field_pos = field_positions(inst, pl.box(), dirs, names) if names else {}
            for k, at in inst.field_pos.items():   # flags keep off this text
                self.text_boxes.append(_text_box(at, inst.value if k == "Value" else
                                                 inst.ref if k == "Reference" else inst.fields.get(k, "")))
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
        flagged = [n for n in terms if self._draw_as_flags(n)]
        for net in flagged:
            for t in terms[net]:
                self._attach_flag(net, t, router, owner_part.get(t.owner))
        self._pwr_flags(terms, router)

        # route everything else, shortest nets first
        routed: dict[str, NetRoute] = {}
        order = sorted((n for n in terms if n not in flagged and len(terms[n]) >= 2),
                       key=lambda n: (len(terms[n]), _hpwl(terms[n])))
        for net in order:
            r = router.route_net(net, terms[net])
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
            if net not in routed:   # a one-pin net never reached the router
                for a, b in stub_segs:
                    self.net_items.setdefault(net, []).append(self.w.wire(a, b))
        for name, anchor, text_dir in pending_labels:
            if not (name in routed and routed[name].labelled):   # its pins carry the label
                self.w.global_label(name, anchor, text_dir)
        # dangling single-terminal nets that are not ports: mark them for the report
        for net, ts in terms.items():
            if len(ts) == 1 and net not in flagged:
                self.problems.append(f"net {net} connects to only {ts[0].owner}")

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
        write_project(pro, self.w.root_uuid)
        _kicad_resave(sch)
        expected = self.expected_netlist()
        moved = [p.res.ref for p in lay.placed if p.moved and p.res.part.type != "port"]
        return BuildResult(sch=sch, pro=pro, expected=expected, problems=self.problems, notes=self.notes,
                           labelled_nets=self.labelled, moved=sorted(set(moved)),
                           uncertain=[u.note for u in self.c.uncertain])

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
            dx, dy = want[0] - near.pos[0], want[1] - near.pos[1]
            text_dir = (1 if dx > 0 else -1, 0) if abs(dx) >= abs(dy) else (0, 1 if dy > 0 else -1)
            anchor = (snap(want[0]), snap(want[1]))
            if text_dir[0] == 0:
                anchor = (near.pos[0], anchor[1])
            else:
                anchor = (anchor[0], near.pos[1])
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
                     sym: LibSymbol | None = None, ref: str = "#PWR?") -> bool:
        """Hang a power flag off a pin: upright if at all possible, like a hand-drawn sheet."""
        if sym is None:
            sym, value = self._flag_symbol(net)
        else:
            value = sym.props.get("Value", "")
        pin = sym.pins_for(1)[0]
        body_lib = (-pin.outward()[0], -pin.outward()[1])     # from the pin into the body
        upright = Orient(0, None)
        up_dir = tuple(int(round(v)) for v in apply(upright.m, *body_lib))

        def attempt(path: list[tuple[float, float]], o: Orient):
            tip = path[-1]
            dx, dy = apply(o.m, pin.x, pin.y)
            origin = (round(tip[0] - dx, 4), round(tip[1] - dy, 4))
            box = _flag_box(sym, origin, o, value)
            if (not self._box_free(box, router, net, None, 0) or not self._path_free(path, router, net)
                    or any(_overlap(box, tb) for tb in self.text_boxes)):
                return None
            return (path, o, origin, box)

        def pt(p, d, k):
            return (round(p[0] + d[0] * k, 4), round(p[1] + d[1] * k, 4))

        cands = []
        if t.out == up_dir:                        # pin already points the flag's way
            cands += [([t.pos, pt(t.pos, t.out, s)], upright) for s in (2.54, 3.81, 5.08, 1.27, 7.62)]
        elif t.out[1] == 0:                        # sideways pin: out, then turn to stand the flag up
            cands += [([t.pos, pt(t.pos, t.out, s1), pt(pt(t.pos, t.out, s1), up_dir, s2)], upright)
                      for s1 in (2.54, 3.81, 5.08) for s2 in (2.54, 3.81, 5.08)]
        for s in (2.54, 3.81, 5.08, 1.27, 7.62):   # last resort: turn the flag to face the pin
            for o in (Orient(a, None) for a in (0, 90, 180, 270)):
                bd = apply(o.m, *body_lib)
                if (round(bd[0]), round(bd[1])) == t.out:
                    cands.append(([t.pos, pt(t.pos, t.out, s)], o))
        best = None
        for path, o in cands:
            best = attempt(path, o)
            if best:
                break
        if best is None:
            if ref == "#PWR?":  # nothing free: label the pin instead
                u = self.w.label(net, t.pos, t.out)
                self.term_owner_items.setdefault(t.owner, []).append(u)
            return False
        path, o, origin, box = best
        inst = SymbolInstance(lib=sym, ref=ref, unit=1, at=origin, orient=o, value=value)
        inst.field_pos = _lib_field_positions(sym, inst.at, o)
        self.w.add_symbol(inst)
        items = [inst.uuid]
        # on a net that is routed as wire, a route may join this stub anywhere along it,
        # so the stub is drawn with the net's wires, where joins get split end to end
        routed = not self._draw_as_flags(net)
        for a, b in zip(path, path[1:]):
            if routed:
                self.flag_stubs.setdefault(net, []).append((a, b))
            else:
                items.append(self.w.wire(a, b))
            router.add_segment(net, cell_of(a), cell_of(b))
        if routed:
            self.flag_pins.setdefault(net, []).append(path[-1])
        self.term_owner_items.setdefault(t.owner, []).extend(items)
        router.block_box(box, inflate=0.3)
        router.pin_net[cell_of(path[-1])] = net
        return True

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
            router.block_box((x - 2.54, y - 6.35, x + 10.16, y + 6.35), inflate=0.3)
            x += 20.32

    # ............................................................... texts
    def _texts(self) -> None:
        lay = self.layout
        for t in self.c.texts:
            if t.at is None:
                continue
            p = lay.to_sheet(t.at)
            color = GREY if t.kind == "voltage" else None
            self.w.text(t.text, (snap(p[0], 0.635), snap(p[1], 0.635)), size=1.27, color=color,
                        italic=t.kind == "voltage")
        bx0, by0, bx1, by1 = lay.bounds()
        y = by1 + 30.48
        if self.c.notes:
            # wrapped so a long note doesn't run far past the drawing
            lines = [ln for n in self.c.notes
                     for ln in textwrap.wrap(n, 110, subsequent_indent="   ") or [""]]
            self.w.text("NOTES:\n" + "\n".join(lines), (snap(bx0), snap(y)), size=1.27, bold=False)
            y += 2.54 * (len(lines) + 2)
        for u in self.c.uncertain:
            label = "CHECK: " + u.note
            if u.at:
                p = lay.to_sheet(u.at)
                self.w.text("?", (snap(p[0], 0.635), snap(p[1], 0.635)), size=2.0, color=RED, bold=True)
            self.w.text(label, (snap(bx0), snap(y)), size=1.27, color=RED)
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


def _text_box(at: tuple[float, float], text: str) -> tuple[float, float, float, float]:
    """Rough extent of centred 1.27 mm field text."""
    w = max(len(text), 1) * 0.95
    return (at[0] - w / 2, at[1] - 0.85, at[0] + w / 2, at[1] + 0.85)


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


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
        **(extra or {}),
    }
    if b.c.bom:
        # verify compares the values KiCad reports for these parts with the doc's parts list
        meta["bom"] = {"rows": b.c.bom, "accept": b.c.bom_accept,
                       "parts": {p.ref: {"type": p.type, "label": p.label} for p in b.c.parts}}
    work = out_dir / "sch2kicad"
    work.mkdir(exist_ok=True)
    (work / "build.json").write_text(json.dumps(meta, indent=2) + "\n")
