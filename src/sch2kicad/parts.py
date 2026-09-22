"""Turn circuit.json parts into concrete KiCad symbols, footprints and pin maps.

Every decision a board depends on — which symbol, which footprint, which lead
goes to which pad — is made here, from the profile and the pinout table, and
anything that cannot be decided is reported as a problem instead of guessed.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .circuit import Circuit, Part, parse_value
from .symlib import LibSymbol, SymbolResolver

# ------------------------------------------------------------------ part types

TWO_TERMINAL = {"resistor", "capacitor", "inductor", "ldr", "fuse", "switch_spst"}
POLAR_TWO = {"capacitor_polarized"}
DIODES = {"diode", "led", "zener", "schottky"}
POTS = {"pot", "trimmer"}
BJTS = {"npn", "pnp"}
FETS = {"njfet", "pjfet", "nmos", "pmos"}
TRANSISTORS = BJTS | FETS
OPAMPS = {"opamp_dual", "opamp_single"}
SWITCHES = {"switch_spdt", "switch_dpdt"}
KNOWN_TYPES = TWO_TERMINAL | POLAR_TWO | DIODES | POTS | TRANSISTORS | OPAMPS | SWITCHES | {"lm386", "ic"}

# Semantic pin names each type accepts, with synonyms folded to one spelling.
_SYNONYMS = {
    "pot": {"1": "ccw", "2": "wiper", "3": "cw", "w": "wiper", "ccw": "ccw", "cw": "cw", "wiper": "wiper"},
    "cap+": {"+": "+", "-": "-", "p": "+", "n": "-", "pos": "+", "neg": "-", "1": "+", "2": "-"},
    "diode": {"a": "A", "k": "K", "anode": "A", "cathode": "K"},
    "opamp": {"+": "+", "-": "-", "out": "out", "o": "out", "in+": "+", "in-": "-",
              "v+": "V+", "v-": "V-", "vcc": "V+", "vee": "V-", "gnd": "V-"},
    "spdt": {"com": "com", "c": "com", "common": "com", "a": "a", "b": "b", "1": "a", "3": "b", "2": "com"},
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


def _footprint_rule(profile: Profile, ptype: str, part: Part) -> str | None:
    for rule in profile.data.get("footprints", []):
        types = rule.get("type")
        if isinstance(types, str):
            types = [types]
        if types and ptype not in types:
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
        elif not lib_id and ptype in ("opamp_single", "ic", "opamp_dual", "lm386"):
            named = self.profile.data.get("named_parts", {})
            key = normalise_part_number(part.part or part.value)
            named = {normalise_part_number(k): v for k, v in named.items()}
            if ptype == "opamp_dual":
                lib_id = self._symbol_for_type("opamp_dual")
            elif ptype == "lm386":
                lib_id = self._symbol_for_type("lm386")
            else:
                lib_id = named.get(key, "")
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
            if unit > sym.unit_count:
                problems.append(f"{part.key}: {lib_id} has only {sym.unit_count} unit(s)")
                unit = 1

        # ---- pins
        pinmap, swappable, p3 = self._pinmap(part, sym, unit, pinout)
        problems += p3

        # ---- footprint
        footprint = part.footprint or _footprint_rule(self.profile, ptype, part) or sym.props.get("Footprint", "")
        if not footprint:
            problems.append(f"{part.ref}: no footprint (symbol {lib_id} has none and no profile rule matched)")

        # ---- value and fields
        value = part.value
        if ptype in TRANSISTORS or ptype in OPAMPS or ptype in ("ic", "lm386"):
            value = part.part or part.value
        elif ptype in DIODES and part.part and not value:
            value = part.part
        if not value:
            value = part.part or sym.props.get("Value", "")
        fields = dict(part.fields)
        if part.label:
            fields["Label"] = part.label
        return Resolved(part=part, lib_id=lib_id, symbol=sym, unit=unit, footprint=footprint,
                        value=value, fields=fields, pinmap=pinmap, swappable=swappable,
                        pinout=pinout, notes=notes, problems=problems)

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
        problems.append(f"{part.ref}: no symbol for a {ptype} with lead order {use}")
        return self._symbol_for_type(ptype) or "Device:Q_NPN", "", problems, notes

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
                pmap = {"com": com[0], "a": throws[0], "b": throws[1]}
                swappable.append(("a", "b"))
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
        """The power unit (V+/V-) for an IC whose circuit.json entry lists ``supply``."""
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
        for k in part.supply:
            key = _SYNONYMS["opamp"].get(k.lower(), k)
            num = by_name.get(key) or by_name.get(k)
            if num is None:
                problems.append(f"{part.ref}: supply pin {k!r} not in {sym.lib_id} unit {unit}")
            else:
                pmap[key] = num
        from .circuit import PinRef
        sup = Part(ref=part.ref, type="supply", value=parent.value, part=part.part,
                   unit=chr(ord("A") + unit - 1),
                   pins={(_SYNONYMS["opamp"].get(k.lower(), k)): PinRef(k, n, None) for k, n in part.supply.items()})
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
        if p.supply and p.ref not in supplies:
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
