"""Lane's house style for a board, applied to the reading before it is built.

Lane's hand edits of the Bulgarian Fuzz and the Memory Lapse (October 2026) did the
same five things to both sheets, none of which changes a connection the reading has:

- the knobs are VR1, VR2... and the toggles SW1, SW2..., numbered in the reading's
  order, the knob's name kept in its Label (PedalPCB names a pot by its knob);
- the power section is numbered from 100: D100 the reverse-polarity diode, D101 the
  status LED, R100 its resistor, then the supply's own series parts, electrolytics
  and regulator, in the order the supply runs through them;
- a status LED, from the protected supply through 4K7 to the 3PDT's SW lug, where the
  drawing has none (the Memory Lapse's layout has none; Lane added it);
- the loose ground pads (GND, GNDL, GNDJ1...) go: the 3PDT array has a GND lug, and
  the jacks come in on sockets (``_port_parts``), each with its ground pin.

The reading on disk is not touched: the drawing and board checks read it as it is.
"""
from __future__ import annotations

import copy
import re

from .circuit import Block, Circuit, Part, PinRef, Port

POWER_REFS = 100
LED_VALUE = "RED"
LED_RESISTOR = "4K7"
SUPPLY_NAME = re.compile(r"^(\+?\d+(\.\d+)?\s*V(\(\w+\))?|VCC|V\+|\+|POWER|PWR|DC(\s*IN)?)$", re.I)


def _prefix(ref: str) -> str:
    return re.match(r"[A-Za-z_]*", ref).group(0) or ref


def _two(p: Part) -> list[PinRef]:
    return list(p.pins.values()) if len(p.pins) == 2 else []


def apply(c: Circuit, ports: dict | None = None) -> tuple[Circuit, list[str]]:
    """A copy of ``c`` in house style, and notes saying what changed.

    ``ports`` is the profile's port table: with no 3PDT array (no true bypass, or a
    block cut out as labels) nothing is added or dropped, only renamed.
    """
    c = copy.deepcopy(c)
    notes: list[str] = []
    cfg = ports or {}
    aliases = {k.upper(): v.upper() for k, v in (cfg.get("aliases") or {}).items()}
    array = bool(cfg.get("array")) and c.true_bypass
    ground = {n for n in c.all_net_names() if c.net_kind(n) == "ground"}

    def key(port: Port) -> str:
        return aliases.get(port.name.strip().upper(), port.name.strip().upper())

    supply = next((p for p in c.ports if p.net not in ground and SUPPLY_NAME.match(p.name.strip())), None)
    sw = next((p for p in c.ports if key(p) == "SW"), None)

    # the supply, port onwards: series parts while the next node is a rail
    chain: list[Part] = []
    shunts: list[Part] = []
    if supply:
        net, seen = supply.net, set()
        for _ in range(4):
            regs, others = [], []
            for p in c.parts:
                if p.ref in seen:
                    continue
                nets = {pr.net for pr in p.pins.values()}
                if net not in nets:
                    continue
                if p.type == "capacitor_polarized" and nets - {net} <= ground:
                    shunts.append(p)
                    seen.add(p.ref)
                elif p.type in ("diode", "schottky", "resistor", "inductor") and len(p.pins) == 2:
                    other = next(pr.net for pr in p.pins.values() if pr.net != net) if len(nets) == 2 else None
                    if other and other not in ground and (c.net_kind(other) == "power" or not chain) \
                            and not (p.type == "resistor" and _divider(c, other, ground)):
                        others.append((p, other))
                elif p.type == "ic" and {"VI", "VO"} <= set(p.pins) and p.pins["VI"].net == net:
                    regs.append((p, p.pins["VO"].net))
            # a regulator first; past a fork (two ways on) the supply is no longer one chain
            nxt = regs[0] if len(regs) == 1 else others[0] if len(others) == 1 and not regs else None
            if not nxt:
                break
            chain.append(nxt[0])
            seen.add(nxt[0].ref)
            net = nxt[1]
        # caps on the last node
        for p in c.parts:
            if p.ref not in seen and p.type == "capacitor_polarized" and net in {pr.net for pr in p.pins.values()} \
                    and {pr.net for pr in p.pins.values()} - {net} <= ground:
                shunts.append(p)
                seen.add(p.ref)
    rail = next((pr.net for p in chain[:1] for pr in p.pins.values() if pr.net != supply.net), None) \
        if supply else None
    rail = rail or (supply.net if supply else None)

    # the status LED: an LED whose cathode is on the SW lug, and its resistor
    led = res = None
    if sw:
        led = next((p for p in c.parts if p.type == "led" and p.pins.get("K") and p.pins["K"].net == sw.net), None)
        if led:
            a = led.pins["A"].net
            res = next((p for p in c.parts if p.type == "resistor" and a in {pr.net for pr in p.pins.values()}), None)
    elif _off_board_led_resistor(c, ground) is not None:
        res = _off_board_led_resistor(c, ground)          # RLED: the LED is off the board
    elif array and supply and rail and not any(p.type == "led" for p in c.parts):
        taken = set(c.all_net_names())
        a_net = next(n for n in ("LED_A", "STATUS_LED_A", "STATUS_LED_A2") if n not in taken)
        sw_net = next(n for n in ("SW", "STATUS_SW", "STATUS_SW2") if n not in taken)
        res = Part(ref="__R_LED", type="resistor", value=LED_RESISTOR,
                   pins={"1": PinRef("1", rail, None), "2": PinRef("2", a_net, None)})
        led = Part(ref="__D_LED", type="led", value=LED_VALUE,
                   pins={"A": PinRef("A", a_net, None), "K": PinRef("K", sw_net, None)})
        c.parts += [res, led]
        c.added += [res.ref, led.ref]
        c.ports.append(Port(name="SW", net=sw_net, at=None))
        notes.append(f"status LED added (house style): {rail} through {LED_RESISTOR} to a {LED_VALUE} LED "
                     f"on the footswitch's SW lug; the drawing has none")
        # into the block the supply is in
        home = next((b for b in c.blocks if chain and chain[0].ref in b.parts), None)
        if home:
            home.parts += [res.ref, led.ref]

    # the loose ground pads: the array's GND lug and the jack sockets' grounds have them
    if array:
        loose = [p for p in c.ports if p.net in ground and key(p) not in ("IN", "OUT", "SW", "JI", "JO")]
        if loose:
            c.ports = [p for p in c.ports if p not in loose]
            notes.append(f"ground pad{'s' if len(loose) > 1 else ''} {', '.join(p.name for p in loose)} "
                         f"left off (house style): the 3PDT array's GND lug and the jack sockets carry ground")

    # new references
    renames: dict[str, str] = {}
    taken = {p.ref for p in c.parts}

    def give(old: str, new_prefix: str, start: int, used: dict) -> None:
        if old in renames:
            return
        n = used.get(new_prefix, start)
        while f"{new_prefix}{n}" in taken - {old} or f"{new_prefix}{n}" in renames.values():
            n += 1
        renames[old] = f"{new_prefix}{n}"
        used[new_prefix] = n + 1

    used: dict[str, int] = {}
    diodes = [p for p in chain if p.type in ("diode", "schottky")][:1]
    for p in diodes:
        give(p.ref, "D", POWER_REFS, used)
    if led:
        give(led.ref, "D", POWER_REFS, used)
    if res:
        give(res.ref, "R", POWER_REFS, used)
    for p in chain:
        if p not in diodes:
            give(p.ref, "D" if p.type in ("diode", "schottky") else _prefix(p.ref), POWER_REFS, used)
    for p in shunts:
        give(p.ref, "C", POWER_REFS, used)
    for kind, prefix in (("pot", "VR"), ("switch", "SW")):
        group: list[str] = []
        for p in c.parts:
            if (p.type == "pot" if kind == "pot" else p.type.startswith("switch_")) and p.ref not in group:
                group.append(p.ref)
        if group and all(re.fullmatch(prefix + r"\d+", r) for r in group):
            continue
        for r in group:
            give(r, prefix, 1, used)
    renames = {k: v for k, v in renames.items() if k != v}
    if not renames:
        return c, notes
    for p in c.parts:
        if p.ref in renames:
            # a knob named by its reference keeps the name where it shows
            if p.type == "pot" or p.type.startswith("switch_"):
                if not p.label and not re.search(r"\d", p.ref):
                    p.label = p.ref
            p.ref = renames[p.ref]
    for b in c.blocks:
        b.parts = [_rename_key(r, renames) for r in b.parts]
    for u in c.uncertain:
        u.refs = [_rename_key(r, renames) for r in u.refs]
    c.bom_accept = {renames.get(k, k): v for k, v in c.bom_accept.items()}
    c.renamed = {v: k for k, v in renames.items() if not k.startswith("__")}
    c.added = [renames.get(r, r) for r in c.added]
    shown = [f"{k} → {v}" for k, v in renames.items() if not k.startswith("__")]
    if shown:
        notes.append("house references: " + ", ".join(shown))
    return c, notes


def _off_board_led_resistor(c: Circuit, ground: set[str]) -> Part | None:
    """The resistor feeding an LED pad from a rail (RLED), if the LED is off the board."""
    rails = {n for n in c.all_net_names() if c.net_kind(n) == "power"} | \
            {p.net for p in c.ports if SUPPLY_NAME.match(p.name.strip())}
    rails |= {pr.net for p in c.parts if p.type in ("diode", "schottky") for pr in p.pins.values()
              if any(pr2.net in rails for pr2 in p.pins.values())} - ground
    for port in c.ports:
        if "LED" not in port.name.upper() or port.net in rails or port.net in ground:
            continue
        for p in c.parts:
            nets = [pr.net for pr in p.pins.values()]
            if p.type == "resistor" and port.net in nets and set(nets) - {port.net} <= rails and len(set(nets)) == 2:
                return p
    return None


def _divider(c: Circuit, net: str, ground: set[str]) -> bool:
    """Is ``net`` the middle of a divider: a resistor from it to ground (Pocket Comb's VR)?"""
    return any(p.type == "resistor" and {pr.net for pr in p.pins.values()} - {net} <= ground
               and net in {pr.net for pr in p.pins.values()} for p in c.parts)


def _rename_key(r: str, renames: dict[str, str]) -> str:
    """A block or uncertain entry: a ref, or a ref with its unit (IC1A, SW1B)."""
    if r in renames:
        return renames[r]
    for old, new in renames.items():
        if r.startswith(old) and re.fullmatch(r"[A-Z]", r[len(old):]):
            return new + r[len(old):]
    return r
