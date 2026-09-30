"""Turn circuit.json parts into concrete KiCad symbols, footprints and pin maps.

Every decision a board depends on — which symbol, which footprint, which lead
goes to which pad — is made here, from the profile and the pinout table, and
anything that cannot be decided is reported as a problem instead of guessed.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

from .circuit import SWITCH_ACTIONS, Circuit, Part, parse_value
from .symlib import LibSymbol, SymbolResolver

# ------------------------------------------------------------------ part types

TWO_TERMINAL = {"resistor", "capacitor", "inductor", "ldr", "fuse", "switch_spst"}
POLAR_TWO = {"capacitor_polarized"}
DIODES = {"diode", "led", "zener", "schottky"}
POTS = {"pot", "trimmer"}
BJTS = {"npn", "pnp"}
FETS = {"njfet", "pjfet", "nmos", "pmos"}
TRANSISTORS = BJTS | FETS
OPAMPS = {"opamp_dual", "opamp_single", "opamp_quad"}
SWITCHES = {"switch_spdt", "switch_dpdt"}
# Rotaries are switches but not toggles: they have no on/on-style action, and
# their throws are an order the shaft turns through rather than a pair.
ROTARIES = {"switch_2p3t"}
KNOWN_TYPES = (TWO_TERMINAL | POLAR_TWO | DIODES | POTS | TRANSISTORS | OPAMPS | SWITCHES
               | ROTARIES | {"lm386", "ic", "transformer"})

# What a switch's action means to whoever orders and wires the part. The netlist
# cannot carry it — com/a/b is identical for all three — so the schematic has to
# say it: in the value, where it shows on the sheet, and in the description,
# which is where a BOM reads from.
_ACTION_TYPES = SWITCHES | {"switch_spst"}
_ACTION_POSITIONS = {
    "on/on": "2 positions", "on/off": "2 positions",
    "on/on/on": "3 positions, no off position",
    "on/off/on": "3 positions, centre off",
}
# An on-on-on needs two poles: with one pole and no off position, two of its
# three positions would be the same connection.
_ACTION_NEEDS_POLES = {"on/on/on": 2}
_POLES = {"switch_spst": 1, "switch_spdt": 1, "switch_dpdt": 2}

# Semantic pin names each type accepts, with synonyms folded to one spelling.
_SYNONYMS = {
    "pot": {"1": "ccw", "2": "wiper", "3": "cw", "w": "wiper", "ccw": "ccw", "cw": "cw", "wiper": "wiper"},
    "cap+": {"+": "+", "-": "-", "p": "+", "n": "-", "pos": "+", "neg": "-", "1": "+", "2": "-"},
    "diode": {"a": "A", "k": "K", "anode": "A", "cathode": "K"},
    "opamp": {"+": "+", "-": "-", "out": "out", "o": "out", "in+": "+", "in-": "-",
              "v+": "V+", "v-": "V-", "vcc": "V+", "vee": "V-", "gnd": "V-"},
    "spdt": {"com": "com", "c": "com", "common": "com", "a": "a", "b": "b", "1": "a", "3": "b", "2": "com"},
    "rotary": {"com": "com", "common": "com", "pole": "com", "1": "1", "2": "2", "3": "3"},
}

# Stock fallbacks for types a profile does not name.
_DEFAULT_SYMBOLS = {
    "inductor": "Device:L",
    "fuse": "Device:Fuse",
    "switch_spst": "Switch:SW_SPST",
    "zener": "Device:D_Zener",
    "schottky": "Device:D_Schottky",
    "capacitor": "Device:C",
    "capacitor_polarized": "Device:C_Polarized",
    "resistor": "Device:R",
    "pot": "Device:R_Potentiometer",
    "trimmer": "Device:R_Potentiometer_Trim",
    "diode": "Device:D",
    "led": "Device:LED",
    "ldr": "Device:R_Photo",
}

# Drawn when no symbol resolves for a transistor, so the sheet still shows
# something while the report says why. It matches the device class: the build is
# NOT READY either way, but a P-channel JFET drawn as an NPN is a wrong part that
# reads as a right one, which is the one thing a placeholder must not do. KiCad
# has no unsuffixed JFET symbol, so those name an order the caller has rejected —
# the problem line says so, and the pins will not map.
_PLACEHOLDERS = {"npn": "Device:Q_NPN", "pnp": "Device:Q_PNP",
                 "njfet": "Device:Q_NJFET_DSG", "pjfet": "Device:Q_PJFET_DSG",
                 "nmos": "Device:Q_NMOS", "pmos": "Device:Q_PMOS"}

# The house quad op-amp is wired for the order nearly every 14-pin quad uses:
# amps on 1-2-3, 5-6-7, 8-9-10, 12-13-14 with V+ on 4 and V- on 11. These are the
# parts that order is certain for; any other quad gets a note asking for the
# datasheet, because a quad wired the other way up is a dead chip, not a swapped
# signal. Prefix match, so TL074CN and LM324N are the same part.
_STANDARD_QUADS = ("TL074", "TL084", "TL064", "TL044", "LM324", "LM2902", "LM348",
                   "LF347", "LF444", "MC3403", "TLC274", "OPA4134")


def canonical_pin(ptype: str, name: str) -> str:
    n = name.strip()
    low = n.lower()
    if ptype in POTS:
        return _SYNONYMS["pot"].get(low, n)
    if ptype in POLAR_TWO:
        return _SYNONYMS["cap+"].get(low, n)
    if ptype in DIODES:
        return _SYNONYMS["diode"].get(low, n.upper())
    if ptype in TRANSISTORS:
        return n.upper()
    if ptype in OPAMPS:
        return _SYNONYMS["opamp"].get(low, n)
    if ptype == "switch_spdt":
        return _SYNONYMS["spdt"].get(low, n)
    if ptype in ROTARIES:
        return _SYNONYMS["rotary"].get(low, n)
    return n


# ------------------------------------------------------------------- profiles

@dataclass
class Profile:
    name: str
    data: dict
    path: Path

    @property
    def symbols(self) -> dict:
        return self.data.get("symbols", {})

    def flag_for(self, net: str) -> str:
        flags = self.data.get("flags", {})
        return flags.get(net) or flags.get(net.upper()) or flags.get("other", "power:VCC")

    @property
    def pwr_flag(self) -> str:
        return self.data.get("flags", {}).get("pwr_flag", "power:PWR_FLAG")

    @property
    def ports(self) -> dict:
        return self.data.get("ports", {})


def profile_dir() -> Path:
    return Path(str(resources.files("sch2kicad") / "data" / "profiles"))


def load_profile(name_or_path: str) -> Profile:
    p = Path(name_or_path)
    if not p.suffix:
        p = profile_dir() / f"{name_or_path}.toml"
    if not p.exists():
        avail = ", ".join(sorted(x.stem for x in profile_dir().glob("*.toml")))
        raise FileNotFoundError(f"no profile {name_or_path!r} (have: {avail})")
    data = tomllib.loads(p.read_text())
    return Profile(name=data.get("name", p.stem), data=data, path=p)


def _pinout_table() -> dict:
    text = (resources.files("sch2kicad") / "data" / "pinouts.toml").read_text()
    return tomllib.loads(text)


# 4000-series CMOS, however the maker spells it: CD4069UBE, MC14013B, HEF4046BP, TC4011BP
_CMOS4000 = re.compile(r"^(?:CD|MC1|HEF|HCF|TC|SN|NTE)?(4\d{3,4})[A-Z]*$")


def normalise_part_number(pn: str) -> str:
    return re.sub(r"[\s\-_]", "", pn.upper())


def _lookup(ptype: str, part_number: str) -> tuple[str | None, str | None, str | None]:
    """(pinout, ambiguity reason, assumption note) for a part number."""
    table = _pinout_table()
    pn = normalise_part_number(part_number)
    amb = {normalise_part_number(k): v for k, v in table.get("ambiguous", {}).items()}
    if pn in amb:
        return None, amb[pn], None
    section = table.get("bjt" if ptype in BJTS else "fet", {})
    known = {normalise_part_number(k): (v, None) for k, v in section.items()}
    if ptype in BJTS:
        for k, v in table.get("assumed", {}).items():
            known[normalise_part_number(k)] = (v["order"], v["note"])
    if pn in known:
        order, note = known[pn]
        return order, None, note
    # suffix grades: 2N5088BU, BC549C, MPSA18G ...
    for k, (order, note) in sorted(known.items(), key=lambda kv: -len(kv[0])):
        if pn.startswith(k):
            return order, None, note
    return None, None, None


def lookup_pinout(ptype: str, part_number: str) -> tuple[str | None, str | None]:
    """(pinout, ambiguity reason) for a part number; (None, None) if unknown."""
    order, why, _ = _lookup(ptype, part_number)
    return order, why


def pinout_assumption(ptype: str, part_number: str) -> str | None:
    """Why a pinout is only assumed (the part also ships in another order), else None."""
    return _lookup(ptype, part_number)[2]


def _switch_action(part: Part, ptype: str) -> tuple[str, str, list[str], list[str]]:
    """A switch's value and description from its action, with what could not be settled.

    Three switches with the same six lugs and the same netlist are three different
    parts to order, and nothing downstream can work out which one the circuit
    wants, so an unmarked action is said out loud rather than left to whoever
    builds the board.
    """
    poles, npoles, action = ptype.split("_", 1)[1].upper(), _POLES.get(ptype, 1), part.action
    if action and action not in SWITCH_ACTIONS:
        return part.value or poles, "", [], [
            f"{part.ref}: action {action!r} is not one of {', '.join(SWITCH_ACTIONS)}"]
    if _ACTION_NEEDS_POLES.get(action, 1) > npoles:
        return part.value or poles, "", [], [
            f"{part.ref}: an {action} switch needs two poles and {poles} has one — "
            f"a single pole cannot make three different live positions"]
    if not action:
        if ptype not in SWITCHES:      # an SPST is on/off and there is nothing to choose
            return part.value or poles, "", [], []
        choices = " vs ".join(a for a in SWITCH_ACTIONS
                              if a != "on/off" and _ACTION_NEEDS_POLES.get(a, 1) <= npoles)
        return part.value or poles, "", [
            f"{part.ref}: {poles} action not marked on the drawing — confirm "
            f"{choices} before ordering"], []
    return (f"{poles} {action}", f"{poles} switch, {action} — {_ACTION_POSITIONS[action]}", [], [])


# The values a panel pot comes in, linear, audio and reverse audio alike (Lane). A
# sheet can ask for any value, and one that is not stocked turns into a hunt at
# ordering time or a quiet substitution nobody wrote down.
STOCK_POTS = (1e3, 2e3, 5e3, 10e3, 20e3, 25e3, 50e3, 100e3, 250e3, 500e3, 1e6, 2e6)


def _stock_pot(part: Part) -> str | None:
    """A note when a pot's value is not one the pot comes in, with the nearest that is."""
    ohms = parse_value(re.sub(r"(?<=\d)m", "M", part.value))    # no pot is milliohms: 1m A is 1M
    if ohms is None or any(abs(ohms - v) <= v * 1e-6 for v in STOCK_POTS):
        return None
    nearest = min(STOCK_POTS, key=lambda v: abs(math.log(v / ohms)))
    m = re.match(r"^([ABCW])(?=\d)", part.value.strip()) or re.search(r"(?<=[\dkKmM])([ABCW])$", part.value.strip())
    return (f"{part.ref}: {part.value} is not a stock pot value ({', '.join(_ohms(v) for v in STOCK_POTS)}); "
            f"the nearest is {(m.group(1) if m else '')}{_ohms(nearest)}")


def _ohms(v: float) -> str:
    return f"{v / 1e6:g}M" if v >= 1e6 else f"{v / 1e3:g}K"


# ------------------------------------------------------------------ resolution

@dataclass
class Resolved:
    part: Part
    lib_id: str
    symbol: LibSymbol
    unit: int                           # KiCad unit number
    footprint: str
    value: str
    fields: dict[str, str]
    pinmap: dict[str, str]              # semantic pin → library pin number
    swappable: list[tuple[str, ...]] = field(default_factory=list)
    pinout: str = ""
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    is_supply_unit: bool = False

    @property
    def ref(self) -> str:
        return self.part.ref


def _pin_order(number: str) -> tuple:
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


def _pins_by_name(sym: LibSymbol, unit: int) -> dict[str, str]:
    out = {}
    for p in sym.pins_for(unit):
        out.setdefault(p.name, p.number)
    return out


def _transistor_pinmap(sym: LibSymbol, pinout: str) -> dict[str, str]:
    names = {p.name.upper(): p.number for p in sym.pins_for(1)}
    letters = set(pinout)
    if letters <= set(names):                     # symbol names its pins by letter
        return {ch: names[ch] for ch in letters}
    return {ch: str(i + 1) for i, ch in enumerate(pinout)}   # house symbols: pins 1-2-3 in lead order


def _symbol_pinout(sym: LibSymbol) -> str | None:
    """Lead order a stock symbol encodes, e.g. 'DGS' for BS170, if its pins are lettered."""
    pins = sorted(sym.pins_for(1), key=lambda p: p.number)
    letters = "".join(p.name.upper()[:1] for p in pins)
    return letters if len(letters) == 3 and set(letters) in ({"B", "C", "E"}, {"G", "D", "S"}) else None


def _footprint_rule(profile: Profile, ptype: str, part: Part, sym: LibSymbol | None = None) -> str | None:
    for rule in profile.data.get("footprints", []):
        types = rule.get("type")
        if isinstance(types, str):
            types = [types]
        if types and ptype not in types:
            continue
        # an IC's package by its pin count (stock LM13700 and 4xxx symbols carry no footprint)
        if "pins" in rule and (sym is None or len({p.number for p in sym.pins}) != int(rule["pins"])):
            continue
        if "part" in rule and not re.search(rule["part"], part.part or part.value, re.I):
            continue
        if "value" in rule and not re.search(rule["value"], part.value, re.I):
            continue
        if "min_farads" in rule or "max_farads" in rule:
            v = parse_value(part.value)
            if v is None:
                continue
            if "min_farads" in rule and v < float(rule["min_farads"]):
                continue
            if "max_farads" in rule and v > float(rule["max_farads"]):
                continue
        return rule["footprint"]
    return None


class PartResolver:
    def __init__(self, profile: Profile, symbols: SymbolResolver):
        self.profile = profile
        self.symbols = symbols

    def _symbol_for_type(self, ptype: str) -> str | None:
        return self.profile.symbols.get(ptype) or _DEFAULT_SYMBOLS.get(ptype)

    def resolve(self, part: Part) -> Resolved:
        ptype = part.type
        problems: list[str] = []
        notes: list[str] = []
        pinout = ""
        lib_id = part.lib_id

        # ---- choose the symbol
        if not lib_id and ptype in TRANSISTORS:
            lib_id, pinout, p2, n2 = self._transistor_symbol(part)
            problems += p2
            notes += n2
        elif not lib_id and ptype in ("opamp_single", "ic", "opamp_dual", "opamp_quad", "lm386"):
            named = self.profile.data.get("named_parts", {})
            key = normalise_part_number(part.part or part.value)
            named = {normalise_part_number(k): v for k, v in named.items()}
            if ptype in ("opamp_dual", "opamp_quad"):
                lib_id = self._symbol_for_type(ptype)
                if ptype == "opamp_quad" and not key.startswith(_STANDARD_QUADS):
                    notes.append(f"{part.ref}: {part.part or part.value or 'this quad'} drawn with "
                                 f"V+ on 4 and V- on 11 (the TL074/LM324 order) — check the "
                                 f"datasheet, and set \"lib_id\" if it is wired another way")
            elif ptype == "lm386":
                lib_id = self._symbol_for_type("lm386")
            else:
                # suffix grades name the same die: LM13700N, TL071CP
                lib_id = named.get(key) or next(
                    (v for k, v in sorted(named.items(), key=lambda kv: -len(kv[0])) if key.startswith(k)), "")
                m = None if lib_id else _CMOS4000.match(key)
                if m and self.symbols.exists(f"4xxx:{m.group(1)}"):
                    lib_id = f"4xxx:{m.group(1)}"          # CD4069UBE, MC14013B, HEF4046BP ...
                if not lib_id:
                    problems.append(f"{part.ref}: no symbol known for {part.part or part.value!r}; "
                                    f"set lib_id in circuit.json")
        elif not lib_id:
            lib_id = self._symbol_for_type(ptype) or ""
            if ptype not in KNOWN_TYPES and not lib_id:
                problems.append(f"{part.ref}: unknown part type {ptype!r}")
        if not lib_id:
            lib_id = "Device:R"  # placeholder so the build can still draw something
            problems.append(f"{part.ref}: no symbol resolved; drew a placeholder")
        try:
            sym = self.symbols.get(lib_id)
        except KeyError as e:
            problems.append(f"{part.ref}: {e}")
            lib_id = "Device:R"
            sym = self.symbols.get(lib_id)

        # ---- unit
        unit = 1
        if part.unit:
            u = part.unit.upper()
            if u.isdigit():
                unit = int(u)
            elif len(u) == 1 and u.isalpha():
                unit = ord(u) - ord("A") + 1
            if unit > sym.unit_count and not (ptype == "ic" and part.pins and sym.unit_count > 1):
                problems.append(f"{part.key}: {lib_id} has only {sym.unit_count} unit(s)")
                unit = 1
        if ptype == "ic" and sym.unit_count > 1 and part.pins:
            # an IC's pins are numbered on the drawing, so they say which unit it is;
            # the source's own unit names (IC4A, IC4_BUF1) needn't follow KiCad's order
            nums = set(part.pins)
            fits = [u for u in range(1, sym.unit_count + 1) if nums <= {p.number for p in sym.pins_for(u)}]
            if fits and unit not in fits:
                unit = fits[0]
                notes.append(f"{part.key}: pins {', '.join(sorted(nums, key=_pin_order))} are unit "
                             f"{chr(ord('A') + unit - 1)} of {lib_id}")
            elif not fits:
                problems.append(f"{part.key}: no unit of {lib_id} has all of pins "
                                f"{', '.join(sorted(nums, key=_pin_order))}")
        if unit > sym.unit_count:
            unit = 1

        # ---- pins
        # an op-amp entry with a supply and no signal pins is a power unit the source
        # draws on its own (PedalPCB's "IC1" with pins 8/4)
        power_only = ptype in OPAMPS | {"ic", "lm386"} and bool(part.supply) and not part.pins
        if power_only:
            pinmap, swappable, p3 = {}, [], []
        else:
            pinmap, swappable, p3 = self._pinmap(part, sym, unit, pinout)
        problems += p3

        # ---- footprint
        footprint = part.footprint or _footprint_rule(self.profile, ptype, part, sym) or sym.props.get("Footprint", "")
        if not footprint:
            problems.append(f"{part.ref}: no footprint (symbol {lib_id} has none and no profile rule matched)")

        # ---- value and fields
        value = part.value
        if ptype in TRANSISTORS or ptype in OPAMPS or ptype in ("ic", "lm386", "transformer"):
            value = part.part or part.value
        elif ptype in DIODES and part.part and not value:
            value = part.part
        if not value:
            value = part.part or sym.props.get("Value", "")
        fields = dict(part.fields)
        if part.label:
            fields["Label"] = part.label
        if ptype == "pot" and (stock := _stock_pot(part)):
            notes.append(stock)
        if ptype in _ACTION_TYPES:
            value, desc, n2, p2 = _switch_action(part, ptype)
            if desc:
                fields.setdefault("Description", desc)
            notes += n2
            problems += p2
        res = Resolved(part=part, lib_id=lib_id, symbol=sym, unit=unit, footprint=footprint,
                       value=value, fields=fields, pinmap=pinmap, swappable=swappable,
                       pinout=pinout, notes=notes, problems=problems)
        if power_only:
            if sym.unit_count == 1:
                problems.append(f"{part.ref}: {lib_id} carries its power pins on the amplifier; "
                                f"put \"supply\" on the amplifier's entry")
                return res
            sup = self.supply_unit(res)
            if sup is not None:
                sup.problems = problems + sup.problems
                return sup
        return res

    # .............................................................. transistors
    def _transistor_symbol(self, part: Part) -> tuple[str, str, list[str], list[str]]:
        ptype = part.type
        problems: list[str] = []
        notes: list[str] = []
        pkg = self.profile.data.get("package_pinouts", {}).get(ptype)
        if pkg:
            lib_id = self._symbol_for_type(ptype) or ""
            notes.append(f"{part.ref}: SMD package order {'-'.join(pkg)} — confirm the SOT-23 "
                         f"substitute for {part.part or part.value}")
            return lib_id, pkg, problems, notes
        pinout = part.pinout
        if not pinout and part.part:
            pinout, why = lookup_pinout(ptype, part.part)
            if why:
                problems.append(f"{part.ref}: pinout unconfirmed for {part.part} — {why}. "
                                f"Ask which order the parts are, then set \"pinout\"")
        elif not pinout:
            problems.append(f"{part.ref}: no part number, so no pinout; set \"part\" and \"pinout\"")
        if pinout and not part.pinout:
            assumed = pinout_assumption(ptype, part.part)
            if assumed:
                notes.append(f"{part.ref}: {part.part} assumed {'-'.join(pinout)} for through-hole — {assumed}")
            else:
                notes.append(f"{part.ref}: {part.part} lead order {'-'.join(pinout)} (from the pinout table)")
        elif pinout:
            notes.append(f"{part.ref}: {part.part or part.value} lead order {'-'.join(pinout)} (set in circuit.json)")
        elif not pinout and part.part and not problems:
            problems.append(f"{part.ref}: {part.part} is not in the pinout table; ask for its lead "
                            f"order and set \"pinout\"")
        house = self.profile.data.get("house_pinouts", {}).get(ptype)
        provisional = not pinout
        use = pinout or house or ("EBC" if ptype in BJTS else "DSG")
        if provisional:
            notes.append(f"{part.ref}: drawn with a provisional {'-'.join(use)} order until confirmed")
        if house and use == house:
            return self._symbol_for_type(ptype), ("" if provisional else use), problems, notes
        named = {normalise_part_number(k): v for k, v in self.profile.data.get("named_parts", {}).items()}
        cand = named.get(normalise_part_number(part.part))
        if cand and self.symbols.exists(cand) and _symbol_pinout(self.symbols.get(cand)) == use:
            return cand, ("" if provisional else use), problems, notes
        tmpl = self.profile.data.get("pinout_fallbacks", {}).get(ptype)
        if tmpl:
            lib_id = tmpl.format(pinout=use)
            if self.symbols.exists(lib_id):
                return lib_id, ("" if provisional else use), problems, notes
        problems.append(f"{part.ref}: no symbol for a {ptype} with lead order {use}; "
                        f"drew a {ptype} placeholder")
        return self._symbol_for_type(ptype) or _PLACEHOLDERS.get(ptype, "Device:Q_NPN"), "", problems, notes

    # ...................................................................... pins
    def _pinmap(self, part: Part, sym: LibSymbol, unit: int, pinout: str):
        ptype = part.type
        problems: list[str] = []
        swappable: list[tuple[str, ...]] = []
        sem = {canonical_pin(ptype, k): k for k in part.pins}
        pmap: dict[str, str] = {}
        unit_pins = sym.pins_for(unit)
        numbers = [p.number for p in unit_pins]
        by_name = _pins_by_name(sym, unit)

        def need(names: set[str]):
            missing = names - set(sem)
            extra = set(sem) - names
            if missing:
                problems.append(f"{part.key}: missing pin(s) {sorted(missing)} for a {ptype}")
            if extra:
                problems.append(f"{part.key}: unexpected pin(s) {sorted(extra)} for a {ptype}")

        if ptype in TWO_TERMINAL:
            keys = list(part.pins)
            if len(keys) != 2 or len(numbers) < 2:
                problems.append(f"{part.key}: a {ptype} needs exactly two pins")
            nums = sorted(numbers)[:2]
            for k, n in zip(keys, nums):
                pmap[canonical_pin(ptype, k)] = n
            swappable.append(tuple(canonical_pin(ptype, k) for k in keys))
        elif ptype in POLAR_TWO:
            need({"+", "-"})
            plus = by_name.get("+") or "1"
            minus = by_name.get("-") or ("2" if plus == "1" else "1")
            pmap = {"+": plus, "-": minus}
        elif ptype in DIODES:
            need({"A", "K"})
            pmap = {"A": by_name.get("A", "2"), "K": by_name.get("K", "1")}
        elif ptype in POTS:
            need({"ccw", "wiper", "cw"})
            pmap = {"ccw": "1", "wiper": "2", "cw": "3"}
        elif ptype in TRANSISTORS:
            letters = {"B", "C", "E"} if ptype in BJTS else {"G", "D", "S"}
            need(letters)
            order = pinout or self.profile.data.get("house_pinouts", {}).get(ptype) or \
                ("EBC" if ptype in BJTS else "DSG")
            pmap = _transistor_pinmap(sym, order)
        elif ptype in OPAMPS:
            names = {"+", "-", "out"} | ({"V+", "V-"} & set(sem))
            need(names)
            outs = [p.number for p in unit_pins if p.etype == "output" or p.name in ("~", "OUT", "out")]
            pmap = {"+": by_name.get("+", ""), "-": by_name.get("-", ""),
                    "out": outs[0] if outs else by_name.get("~", "")}
            if "V+" in sem or "V-" in sem:
                problems.append(f"{part.key}: put op-amp power pins in \"supply\", not \"pins\"")
        elif ptype in ROTARIES:
            need({"com", "1", "2", "3"})
            ends = sorted(unit_pins, key=lambda p: p.number)
            com = [p.number for p in ends if p.x < 0]
            throws = [p.number for p in ends if p.number not in com]
            if len(com) == 1 and len(throws) == 3:
                # not swappable, unlike a toggle's throws: 1, 2, 3 are the order
                # the shaft turns through, and swapping two reverses the sweep
                pmap = {"com": com[0], "1": throws[0], "2": throws[1], "3": throws[2]}
            else:
                problems.append(f"{part.key}: could not tell the rotary's common from its throws")
        elif ptype == "switch_spdt":
            need({"com", "a", "b"})
            ends = sorted((p for p in unit_pins), key=lambda p: p.number)
            com = [p.number for p in ends if p.x < 0] or [ends[1].number]
            throws = [p.number for p in ends if p.number not in com]
            pmap = {"com": com[0], "a": throws[0], "b": throws[-1]}
            swappable.append(("a", "b"))
        elif ptype == "switch_dpdt":
            need({"com", "a", "b"})
            ends = sorted(unit_pins, key=lambda p: p.number)
            com = [p.number for p in ends if p.x < 0]
            throws = [p.number for p in ends if p.number not in com]
            if com and len(throws) == 2:
                # not swappable: both poles throw the same way, so "a" is lug 1 on one
                # pole and lug 4 on the other. Swapping one pole's throws to match the
                # drawing (the Fix'd Fuzz draws pole B mirrored) paired BYP with a stage
                # output; a mirror of the symbol matches the drawing without that.
                pmap = {"com": com[0], "a": throws[0], "b": throws[1]}
            else:
                problems.append(f"{part.key}: could not identify DPDT pole pins")
        else:  # ic, lm386, anything with explicit numbers
            for k in part.pins:
                if k in numbers:
                    pmap[k] = k
                elif k in by_name:
                    pmap[k] = by_name[k]
                else:
                    problems.append(f"{part.key}: {sym.lib_id} unit {unit} has no pin {k!r}")
        # report semantic names in circuit.json that did not map
        for s, orig in sem.items():
            if s not in pmap and not any(s in g for g in swappable):
                if ptype in TWO_TERMINAL:
                    continue
                if f"{part.key}: unexpected" not in " ".join(problems):
                    problems.append(f"{part.key}: pin {orig!r} does not map to {sym.lib_id}")
        return pmap, swappable, problems

    # ............................................................ supply units
    def supply_unit(self, parent: Resolved) -> Resolved | None:
        """The power unit (V+/V-) for an IC whose circuit.json entry lists ``supply``.

        A single-unit symbol (TL071) has its power pins on the amplifier itself; they
        are added to ``parent`` and there is no separate unit."""
        part = parent.part
        if not part.supply:
            return None
        sym = parent.symbol
        # the unit that owns the V+ pin
        power_units = sorted({p.unit for p in sym.pins if p.etype == "power_in" and p.unit})
        if not power_units:
            return None
        unit = power_units[-1]
        by_name = _pins_by_name(sym, unit)
        pmap, problems = {}, []
        # a logic sheet writes VDD where KiCad's 4049 names the pin VCC, and so on
        alias = {"V+": ("VDD", "VCC"), "V-": ("VSS", "VEE", "GND"),
                 "VDD": ("VCC",), "VCC": ("VDD",), "VSS": ("VEE", "GND"), "VEE": ("VSS",)}
        for k in part.supply:
            key = _SYNONYMS["opamp"].get(k.lower(), k)
            num = by_name.get(key) or by_name.get(k) or next(
                (by_name[a] for a in alias.get(key, ()) if a in by_name), None)
            if num is None:
                problems.append(f"{part.ref}: supply pin {k!r} not in {sym.lib_id} unit {unit}")
            else:
                pmap[key] = num
        from .circuit import PinRef
        if unit == parent.unit and part.pins:
            # power pins on the amplifier's own unit (a TL071). Drawn apart from it
            # (PedalPCB's IC3.2), they must not steer how the amplifier is turned, so
            # they join it without a position
            pins = dict(part.pins)
            for k, n in part.supply.items():
                pins[_SYNONYMS["opamp"].get(k.lower(), k)] = PinRef(k, n, None)
            parent.part = replace(part, pins=pins)
            parent.pinmap.update(pmap)
            parent.problems += problems
            return None
        sup = Part(ref=part.ref, type="supply", value=parent.value, part=part.part,
                   unit=chr(ord("A") + unit - 1),
                   pins={(_SYNONYMS["opamp"].get(k.lower(), k)): PinRef(k, n, part.supply_at.get(k))
                         for k, n in part.supply.items()})
        return Resolved(part=sup, lib_id=parent.lib_id, symbol=sym, unit=unit, footprint=parent.footprint,
                        value=parent.value, fields=dict(parent.fields), pinmap=pmap, problems=problems,
                        is_supply_unit=True)


def resolve_all(circuit: Circuit, profile: Profile, symbols: SymbolResolver) -> list[Resolved]:
    r = PartResolver(profile, symbols)
    out: list[Resolved] = []
    supplies: dict[str, Resolved] = {}
    for p in circuit.parts:
        res = r.resolve(p)
        out.append(res)
        if p.supply and p.ref not in supplies and not res.is_supply_unit:
            s = r.supply_unit(res)
            if s:
                supplies[p.ref] = s
    out.extend(supplies.values())
    # multi-unit parts must agree on symbol and footprint
    by_ref: dict[str, list[Resolved]] = {}
    for res in out:
        by_ref.setdefault(res.ref, []).append(res)
    for ref, group in by_ref.items():
        libs = {g.lib_id for g in group}
        if len(libs) > 1:
            group[0].problems.append(f"{ref}: units resolve to different symbols {sorted(libs)}")
        units = [g.unit for g in group]
        if len(units) != len(set(units)):
            group[0].problems.append(f"{ref}: the same unit appears twice")
    return out
