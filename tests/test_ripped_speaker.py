"""End to end on a real Boneyard schematic: needs KiCad 10 and the Pedal Division library."""

import shutil
from pathlib import Path

import pytest

from sch2kicad.build import build
from sch2kicad.kicad_env import kicad_cli
from sch2kicad.symlib import SymbolResolver
from sch2kicad.verify import verify

HERE = Path(__file__).resolve().parent
CIRCUIT = HERE.parent / "samples" / "circuits" / "ripped-speaker-cb-mod.circuit.json"


def _have_kicad():
    try:
        kicad_cli()
        return SymbolResolver().exists("2025_PedalDivision_Resistors:TH_Resistor_1/4w")
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _have_kicad(), reason="needs KiCad 10 and the Pedal Division library")


SAMPLES = sorted((HERE.parent / "samples" / "circuits").glob("*.circuit.json"))

# A reading can be right and still wait on something only its owner can settle: a
# transistor nobody has confirmed the lead order of is never guessed. Such a sheet is
# held to everything else, and to waiting on exactly that and nothing more — so the
# day it is settled the entry here fails, and has to come off.
WAITING: dict[str, set[str]] = {}


def _ready(rep, circuit, whole: bool):
    waits = WAITING.get(circuit.name.split(".")[0], set())
    held = {u.split(":")[0] for u in rep.unconfirmed}
    assert (held == waits) if whole else (held <= waits), rep.text()
    rep.unconfirmed = []
    assert rep.ok, rep.text()


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_every_sample_is_ready(tmp_path, circuit):
    rep = verify(build(circuit, tmp_path / "board").sch, render=False)
    _ready(rep, circuit, whole=True)
    assert rep.erc_warnings == [], rep.text()


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_every_block_is_ready(tmp_path, circuit):
    from sch2kicad.blocks import save_blocks
    for sch in save_blocks(circuit, tmp_path / "blocks"):
        rep = verify(sch, render=False)
        _ready(rep, circuit, whole=False)
        assert rep.erc_warnings == [], rep.text()     # e.g. a local label named like a boundary label


@pytest.mark.parametrize("profile", ["tht", "smd"])
def test_build_matches_netlist(tmp_path, profile):
    res = build(CIRCUIT, tmp_path / profile, profile=profile)
    rep = verify(res.sch, render=False)
    assert rep.netlist_ok, rep.netlist_diffs
    assert rep.erc_errors == []
    assert rep.footprint_problems == []
    assert rep.unconfirmed == []
    assert rep.ok, rep.text()
    # the 2N2222s are E-B-C by assumption, and the report must say so
    if profile == "tht":
        assert sum("assumed E-B-C" in n for n in rep.notes) == 2


def test_unknown_pinout_blocks_ready(tmp_path):
    import json
    data = json.loads(CIRCUIT.read_text())
    for p in data["parts"]:
        if p.get("part") == "2N2222":
            p["part"] = "MP38A"          # germanium, lead order unknown: must ask
    data["source"]["image"] = str((CIRCUIT.parent / data["source"]["image"]).resolve())
    c = tmp_path / "c.json"
    c.write_text(json.dumps(data))
    rep = verify(build(c, tmp_path / "out").sch, render=False)
    assert len(rep.unconfirmed) == 2
    assert not rep.ok


def test_explicit_pinout_overrides_assumption(tmp_path):
    import json
    data = json.loads(CIRCUIT.read_text())
    for p in data["parts"]:
        if p.get("part") == "2N2222":
            p["pinout"] = "CBE"
    data["source"]["image"] = str((CIRCUIT.parent / data["source"]["image"]).resolve())
    c = tmp_path / "c.json"
    c.write_text(json.dumps(data))
    res = build(c, tmp_path / "out")
    rep = verify(res.sch, render=False)
    assert rep.ok, rep.text()
    # a CBE part must not land on the house E-B-C symbol
    sch = res.sch.read_text()
    assert "Transistor_BJT:Q_NPN_CBE" in sch


def test_drawn_power_units_are_placed_where_drawn():
    """PedalPCB draws each 4558's power unit beside its own filter: it must land there, and a
    single-unit TL071 must carry its power pins on the amplifier itself."""
    from sch2kicad.circuit import load
    from sch2kicad.geometry import Layout
    from sch2kicad.parts import load_profile, resolve_all
    c = load(HERE.parent / "samples" / "circuits" / "redmuffin-pedalpcb.circuit.json")
    res = resolve_all(c, load_profile("tht"), SymbolResolver())
    power = [r for r in res if r.is_supply_unit]
    assert sorted(r.part.key for r in power) == ["IC1C", "IC2C"]
    assert all(pin.at for r in power for pin in r.part.pins.values())
    assert Layout(res).unplaced == []
    ic3 = next(r for r in res if r.ref == "IC3")
    assert ic3.pinmap["V+"] == "7" and ic3.pinmap["V-"] == "4"


def test_single_opamp_by_part_number():
    """Colonel Angus's CA3140 is a stock KiCad symbol on the DIP-8 (SOIC-8 for smd). A single
    op-amp the table does not know is a problem the build reports, with nothing wired to the
    placeholder: drawn on a resistor's pins, it used to stop the layout with a KeyError."""
    from sch2kicad.circuit import from_dict
    from sch2kicad.parts import PartResolver, load_profile

    def part(number):
        return from_dict({"name": "t", "parts": [{"ref": "U1", "type": "opamp_single", "part": number,
                                                    "pins": pins, "supply": supply}]}).parts[0]
    pins = {"+": ["U1_NI", 545, 322], "-": ["U1_INV", 545, 301], "out": ["U1_OUT", 577, 307]}
    supply = {"V+": "VCC", "V-": "GND"}
    for prof in ("tht", "smd"):
        r = PartResolver(load_profile(prof), SymbolResolver()).resolve(
            part("CA3140"))
        assert r.lib_id == "Amplifier_Operational:CA3140" and r.problems == [], r.problems
        assert r.pinmap["+"] == "3" and r.pinmap["-"] == "2" and r.pinmap["out"] == "6"
    r = PartResolver(load_profile("tht"), SymbolResolver()).resolve(
        part("XYZ3141"))
    assert any("no symbol known for 'XYZ3141'" in p for p in r.problems)
    assert r.pinmap == {}


def test_ic_units_follow_pin_numbers():
    """The Tri-Vibe's LM13700: units are chosen by pin number, and on our own symbol they
    come out in the order the datasheet and the drawing both use — IC4A really is unit A.
    The package comes from the pin count."""
    from sch2kicad.circuit import load
    from sch2kicad.parts import load_profile, resolve_all
    c = load(HERE.parent / "samples" / "circuits" / "trivibe-aionfx.circuit.json")
    res = [r for r in resolve_all(c, load_profile("tht"), SymbolResolver()) if r.ref == "IC4"]
    assert {r.part.key: r.unit for r in res} == {"IC4A": 1, "IC4BUF1": 3, "IC4B": 2, "IC4BUF2": 4, "IC4E": 5}
    assert all(r.problems == [] for r in res)
    assert {r.footprint for r in res} == {"Package_DIP:DIP-16_W7.62mm"}
    smd = [r for r in resolve_all(c, load_profile("smd"), SymbolResolver()) if r.ref == "IC4"]
    assert {r.footprint for r in smd} == {"Package_SO:SOIC-16_3.9x9.9mm_P1.27mm"}


def test_cmos_logic_symbols_and_drawn_power_units():
    """The Theremin Fuzz's CD40xx chips: stock 4xxx symbols by part number, a DIP by pin
    count, and the CD4069/CD4013 power units placed where the drawing puts them."""
    from sch2kicad.circuit import load
    from sch2kicad.parts import load_profile, resolve_all
    c = load(HERE.parent / "samples" / "circuits" / "theremin-fuzz-parasit.circuit.json")
    res = resolve_all(c, load_profile("tht"), SymbolResolver())
    by_key = {r.part.key: r for r in res}
    assert by_key["U1C"].lib_id == "4xxx:4069"          # CD4069 on the drawing
    assert by_key["U2A"].lib_id == "4xxx:4013"
    assert by_key["U3"].lib_id == "4xxx:4046"
    assert by_key["U1C"].footprint == "2025_PedalDivision_OpAmps:DIP-14_W7.62mm"
    assert by_key["U3"].footprint == "Package_DIP:DIP-16_W7.62mm"
    power = {r.part.key: r for r in res if r.is_supply_unit}
    assert sorted(power) == ["U1G", "U2C"]
    assert all(pin.at for r in power.values() for pin in r.part.pins.values())
    assert power["U1G"].pinmap == {"VDD": "14", "VSS": "7"}
    assert all(r.problems == [] for r in res)


def test_shipped_symbol_library_travels_with_the_project(tmp_path):
    """KiCad has no CD4517: the symbol comes from the tool's own library, and the built
    project gets a copy of it and a sym-lib-table so the lib_id still resolves."""
    from sch2kicad.circuit import load
    from sch2kicad.parts import load_profile, resolve_all
    circuit = HERE.parent / "samples" / "circuits" / "higgs-phaser-parasit.circuit.json"
    c = load(circuit)
    res = {r.part.key: r for r in resolve_all(c, load_profile("tht"), SymbolResolver())}
    assert res["U4A"].lib_id == "sch2kicad:CD4517"
    # the halves are told apart by their pin numbers, and unit C is the supply box
    assert (res["U4A"].unit, res["U4B"].unit, res["U4C"].unit) == (1, 2, 3)
    assert res["U4C"].pinmap == {"VDD": "16", "VSS": "8"}
    assert res["U4A"].footprint == "Package_DIP:DIP-16_W7.62mm"
    out = tmp_path / "board"
    build(circuit, out)
    assert (out / "sch2kicad.kicad_sym").exists()
    assert 'name "sch2kicad"' in (out / "sym-lib-table").read_text()


@pytest.mark.parametrize("part", ["LM565CN", "NE565N"])
def test_the_lm565_pll_is_a_shipped_symbol(tmp_path, part):
    """KiCad has no LM565 (the Sick Pitch King Jr.'s PLL): ours is pinned as TI and
    Signetics number it, V- on 1 and V+ on 10, and lands on a DIP-14."""
    import json as _json
    from sch2kicad.circuit import load
    from sch2kicad.parts import load_profile, resolve_all
    pins = {"1": "GND", "2": "IN", "3": "GND", "4": "VCO", "5": "VCO", "6": "REF", "7": "REF",
            "8": "+9V", "9": "CT", "10": "+9V", "11": "NC", "12": "NC", "13": "NC", "14": "NC"}
    f = tmp_path / "pll.circuit.json"
    f.write_text(_json.dumps({"schema": "sch2kicad/circuit@1", "name": "PLL", "parts": [
        {"ref": "IC1", "type": "ic", "value": part, "part": part,
         "pins": {k: [n, 100 + 10 * i, 100] for i, (k, n) in enumerate(pins.items())}}]}))
    feet = {}
    for profile in ("tht", "smd"):
        r = resolve_all(load(f), load_profile(profile), SymbolResolver())[0]
        assert r.lib_id == "sch2kicad:LM565"
        assert r.problems == []
        feet[profile] = r.footprint
    assert feet["tht"] == "2025_PedalDivision_OpAmps:DIP-14_W7.62mm"


def test_the_btdr3h_reverb_brick_is_a_shipped_symbol(tmp_path):
    """KiCad has no Belton/Accutronics BTDR-3H (the Troglodyte's reverb): ours is
    numbered as the datasheet and Aleph Null number it, and plugs into a 1x10 socket."""
    import json as _json
    from sch2kicad.circuit import load
    from sch2kicad.parts import load_profile, resolve_all
    pins = {"1": "VC", "2": "GND", "3": "IN", "4": "GND", "5": "OUT2", "6": "OUT1",
            "7": "P1A", "8": "P1B", "9": "P2A", "10": "P2B"}
    f = tmp_path / "brick.circuit.json"
    f.write_text(_json.dumps({"schema": "sch2kicad/circuit@1", "name": "Brick", "parts": [
        {"ref": "U3", "type": "ic", "value": "BTDR-3H", "part": "BTDR-3H",
         "pins": {k: [n, 100 + 10 * i, 100] for i, (k, n) in enumerate(pins.items())}}]}))
    for profile in ("tht", "smd"):
        r = resolve_all(load(f), load_profile(profile), SymbolResolver())[0]
        assert r.lib_id == "sch2kicad:BTDR-3H"
        assert r.problems == []
        assert r.footprint == "Connector_PinSocket_2.54mm:PinSocket_1x10_P2.54mm_Vertical"


def test_a_4p4t_rotary_is_the_house_symbol_a_unit_a_pole(tmp_path):
    """Shin-ei's ME-7 selects its effect on a 4P4T rotary (Dirtbox's lugs A-D and 1-16).
    The house symbol is a unit a pole, its throws pins 1-16 and its commons A-D pins
    17-20; it carries no footprint, so the profile puts it on a row of wiring pads."""
    import json as _json
    parts = []
    for u, pole in enumerate("ABCD"):
        pins = {str(17 + u): [f"COM_{pole}", 100, 100 + 40 * u]}
        pins.update({str(4 * u + k): [f"T{4 * u + k}", 120, 100 + 40 * u + 5 * k] for k in range(1, 5)})
        parts.append({"ref": "SW1", "unit": pole, "type": "ic", "value": "4P4T", "part": "4P4T", "pins": pins})
    f = tmp_path / "selector.circuit.json"
    f.write_text(_json.dumps({"schema": "sch2kicad/circuit@1", "name": "Selector", "parts": parts}))
    from sch2kicad.circuit import load
    from sch2kicad.parts import load_profile, resolve_all
    for r in resolve_all(load(f), load_profile("tht"), SymbolResolver()):
        assert r.lib_id == "2025_PedalDivision_Switches:SW_Rotary_4x4" and r.problems == []
        assert r.footprint == "Connector_PinHeader_2.54mm:PinHeader_1x20_P2.54mm_Vertical"
    rep = verify(build(f, tmp_path / "board").sch, render=False)
    assert rep.netlist_ok, rep.netlist_diffs
    assert rep.footprint_problems == []
    # the sheet's copy must read back as the library's (a Datasheet of "~" did not)
    assert not [w for w in rep.erc_warnings if "lib_symbol_mismatch" in w]


def test_open_driver_on_a_power_net_is_allowed(tmp_path):
    """The house transistor symbols call collector and emitter open drivers; a grounded
    emitter then meets the net's PWR_FLAG, which KiCad's default matrix calls an error."""
    import json as _json
    circuit = HERE.parent / "samples" / "circuits" / "higgs-phaser-parasit.circuit.json"
    res = build(circuit, tmp_path / "board")          # Q1's emitter sits on GND
    pin_map = _json.loads(res.pro.read_text())["erc"]["pin_map"]
    assert pin_map[8][9] == 0 and pin_map[8][10] == 0     # power out against open collector/emitter
    assert pin_map[8][8] == 2                             # two power outputs still conflict
    assert any("open drivers" in n for n in res.notes)
    other = build(HERE.parent / "samples" / "circuits" / "theremin-fuzz-parasit.circuit.json",
                  tmp_path / "other")                 # no transistor: no override
    assert "erc" not in _json.loads(other.pro.read_text())


def _as_fet(data: dict, ptype: str, part: str, pinout: str | None) -> dict:
    """Turn the Ripped Speaker's BS170 into some other three-lead FET."""
    for p in data["parts"]:
        if p["ref"] == "Q4":
            p["type"], p["part"] = ptype, part
            p.pop("pinout", None)
            if pinout:
                p["pinout"] = pinout
    return data


@pytest.mark.parametrize("ptype,part,pinout,want", [
    ("njfet", "J201", None, "2025_PedalDivision_Transistors:TH_JFET"),   # the house order
    ("njfet", "2N5457", "GDS", "Device:Q_NJFET_GDS"),
    ("pjfet", "J176", "DSG", "Device:Q_PJFET_DSG"),
    ("pjfet", "J176", "SGD", "Device:Q_PJFET_SGD"),
    ("pmos", "BS250", "GDS", "Transistor_FET:Q_PMOS_GDS"),
])
def test_a_fet_outside_the_house_order_gets_the_symbol_for_its_order(ptype, part, pinout, want):
    """KiCad keeps the JFETs in Device and only the MOSFETs in Transistor_FET, so the
    fallback template has to name the right library — it named Transistor_FET for both, and
    nothing noticed because no sample has a JFET in any order but the house D-S-G."""
    import json
    from sch2kicad.circuit import from_dict
    from sch2kicad.parts import load_profile, resolve_all
    data = _as_fet(json.loads(CIRCUIT.read_text()), ptype, part, pinout)
    res = resolve_all(from_dict(data), load_profile("tht"), SymbolResolver())
    q4 = next(r for r in res if r.ref == "Q4")
    assert q4.lib_id == want, q4.problems
    assert q4.problems == []



def test_an_smd_p_mosfet_takes_the_sot23_order_not_the_to92_one():
    """The Fix'd Fuzz's VP3203 is S-G-D in TO-92 (Lane), and SMD drew it S-G-D on a
    SOT-23 footprint with no word said. Every SOT-23 P-MOSFET checked — AO3401A,
    BSS84, Si2301CDS, IRLML6402 — is G-S-D on pads 1-2-3, as the N-channel ones are,
    so a board built from the TO-92 order would have gate and source swapped."""
    import json
    from sch2kicad.circuit import from_dict
    from sch2kicad.parts import load_profile, resolve_all
    fixd = CIRCUIT.parent / "fixd-fuzz-roberts.circuit.json"
    res = resolve_all(from_dict(json.loads(fixd.read_text())), load_profile("smd"), SymbolResolver())
    q1 = next(r for r in res if r.ref == "Q1")
    assert q1.lib_id == "Transistor_FET:Q_PMOS_GSD", q1.problems
    assert any("SMD package order G-S-D" in n for n in q1.notes), q1.notes

def test_a_p_channel_jfet_is_never_drawn_as_an_npn(tmp_path):
    """A part with no symbol fell through to Device:Q_NPN. The build is NOT READY either
    way, but a P-channel JFET drawn as an NPN is a wrong part that reads as a right one."""
    import json
    data = _as_fet(json.loads(CIRCUIT.read_text()), "pjfet", "J176", "DSG")
    data["source"]["image"] = str((CIRCUIT.parent / data["source"]["image"]).resolve())
    c = tmp_path / "c.json"
    c.write_text(json.dumps(data))
    res = build(c, tmp_path / "out")
    sch = res.sch.read_text()
    assert "Device:Q_PJFET_DSG" in sch
    # Q4's three leads still have to land on the right pins of it
    rep = verify(res.sch, render=False)
    assert rep.netlist_ok, rep.text()
    assert rep.erc_warnings == [] and rep.erc_errors == []


def test_an_unknown_lead_order_still_asks(tmp_path):
    """J270 is not in the pinout table: the P-JFET symbol must not make it look settled.

    This used to use the J176, until Lane read its datasheet (D-G-S, 2026-09-23) and
    it went into the table. A test whose premise is a gap has to move when the gap is
    filled — the thing being checked is that an unknown part still asks, not that any
    one part is unknown."""
    import json
    from sch2kicad.circuit import from_dict
    from sch2kicad.parts import load_profile, resolve_all
    data = _as_fet(json.loads(CIRCUIT.read_text()), "pjfet", "J270", None)
    res = resolve_all(from_dict(data), load_profile("tht"), SymbolResolver())
    q4 = next(r for r in res if r.ref == "Q4")
    assert any("not in the pinout table" in p for p in q4.problems)


# ------------------------------------------------------------- quad op-amps

QUAD = HERE.parent / "samples" / "circuits" / "synthetic-quad-opamp.circuit.json"


@pytest.mark.parametrize("sym", ["2025_PedalDivision_ICs:TH_Op_amp_Quad",
                                 "2025_PedalDivision_ICs:SMD_Op_amp_Quad"])
def test_the_house_quad_is_wired_like_every_other_quad(sym):
    """The house symbol lives outside this repo, so the order it is drawn in is
    checked here: amps on 1-2-3, 5-6-7, 8-9-10 and 12-13-14 with V+ on 4 and V- on
    11, the way a TL074, a TL084 and an LM324 all are. Getting this upside down
    would not swap a signal, it would cook the chip."""
    s = SymbolResolver().get(sym)
    assert s.unit_count == 5
    # an output is known by its type, as the builder finds it: its name is blank
    got = {u: {"out" if p.etype == "output" else p.name: p.number for p in s.pins_for(u)} for u in range(1, 6)}
    assert got[1] == {"out": "1", "-": "2", "+": "3"}
    assert got[2] == {"+": "5", "-": "6", "out": "7"}
    assert got[3] == {"out": "8", "-": "9", "+": "10"}
    assert got[4] == {"+": "12", "-": "13", "out": "14"}
    assert got[5] == {"V-": "11", "V+": "4"}


@pytest.mark.parametrize("profile,footprint", [
    ("tht", "2025_PedalDivision_OpAmps:DIP-14_W7.62mm"),
    ("smd", "2025_PedalDivision_OpAmps:SOIC-14_3.9x8.7mm_P1.27mm"),
])
def test_a_quad_fills_four_units_and_stands_its_supply_apart(profile, footprint):
    """A, B, C, D land on the four amplifiers and the supply on the fifth unit."""
    from sch2kicad.circuit import load
    from sch2kicad.parts import load_profile, resolve_all
    res = [r for r in resolve_all(load(QUAD), load_profile(profile), SymbolResolver())
           if r.ref == "U1"]
    amps = [r for r in res if not r.is_supply_unit]
    assert [r.unit for r in amps] == [1, 2, 3, 4]
    assert [r.pinmap for r in amps] == [
        {"-": "2", "+": "3", "out": "1"}, {"-": "6", "+": "5", "out": "7"},
        {"-": "9", "+": "10", "out": "8"}, {"-": "13", "+": "12", "out": "14"}]
    supply = next(r for r in res if r.is_supply_unit)
    assert supply.unit == 5 and supply.pinmap == {"V+": "4", "V-": "11"}
    assert {r.footprint for r in res} == {footprint}
    assert not [p for r in res for p in r.problems]


def test_an_unfamiliar_quad_says_which_supply_order_it_drew():
    """The TL074 order is nearly universal but not quite, and a quad wired the other
    way up is a dead chip. A part the table does not know says so in the report,
    the way an assumed transistor lead order does."""
    import json

    from sch2kicad.circuit import from_dict
    from sch2kicad.parts import load_profile, resolve_all
    for part, wanted in (("TL074", False), ("TL074CN", False), ("LM324N", False), ("RC4136", True)):
        data = json.loads(QUAD.read_text())
        for p in data["parts"]:
            if p["ref"] == "U1":
                p["part"] = p["value"] = part
        res = resolve_all(from_dict(data), load_profile("tht"), SymbolResolver())
        notes = [n for r in res if r.ref == "U1" for n in r.notes]
        assert bool([n for n in notes if "V+ on 4 and V- on 11" in n]) is wanted, (part, notes)


def test_a_three_position_dpdt_says_so_on_the_sheet(tmp_path):
    """The Disorder's DPDT is an on-on-on, and the schematic has to be the record.

    Its two poles are chained so the toggle picks one of three signals, which a plain
    on/on DPDT cannot do — but the netlist is identical either way, six lugs and the
    same com/a/b, so nothing downstream can tell. Ordering the wrong one loses the
    middle setting, and the only place that can say which to buy is the sheet.
    """
    from sch2kicad import sexpr

    disorder = HERE.parent / "samples" / "circuits" / "disorder-22358.circuit.json"
    res = build(disorder, tmp_path / "board")
    node = sexpr.parse((tmp_path / "board" / res.sch.name).read_text())
    units = [s for s in sexpr.children(node, "symbol")
             if any(p[1] == "Reference" and p[2] == "SW1" for p in sexpr.children(s, "property"))]
    assert len(units) == 2, "both poles of SW1"
    for u in units:
        names = [p[1] for p in sexpr.children(u, "property")]
        props = {p[1]: p[2] for p in sexpr.children(u, "property")}
        assert props["Value"] == "DPDT on/on/on"
        assert props["Description"] == "DPDT switch, on/on/on — 3 positions, no off position"
        # ours replaces the library's rather than standing beside it: KiCad reads two
        # properties of one name as a broken symbol
        assert names.count("Description") == 1, names


def test_both_poles_of_a_dpdt_throw_the_same_way():
    """A toggle's lever carries both commons to the same end: lugs 1 and 4 in one
    position, 3 and 6 in the other. The Fix'd Fuzz draws each stage switch's pole B
    mirrored, and the fit matched that by swapping pole B's throws instead of mirroring
    the symbol — BYP landed on lug 1 of one pole and lug 6 of the other, so each stage
    took its input from one position and gave its output in the other, and the pedal
    passed nothing either way. Same nets, so verify could not see it."""
    from sch2kicad.circuit import load
    from sch2kicad.geometry import fit_orientation
    from sch2kicad.parts import load_profile, resolve_all

    fixd = HERE.parent / "samples" / "circuits" / "fixd-fuzz-roberts.circuit.json"
    res = resolve_all(load(fixd), load_profile("tht"), SymbolResolver())
    lugs = {(r.ref, r.part.unit): fit_orientation(r).pinmap for r in res if r.part.type == "switch_dpdt"}
    assert len(lugs) == 8
    for sw in ("SW1", "SW2", "SW3", "SW4"):
        a, b = lugs[(sw, "A")], lugs[(sw, "B")]
        assert (a["a"], b["a"]) == ("1", "4"), (sw, a, b)    # BYP, both ends, one position
        assert (a["b"], b["b"]) == ("3", "6"), (sw, a, b)    # the stage, in the other


def test_a_rotary_pole_keeps_its_throws_in_the_order_the_shaft_turns():
    """The Gargler's 2P3T, which had been three bare pads with the wiring in a note.

    A rotary is not a toggle: its throws are the order the shaft sweeps through, so
    unlike an SPDT's a and b they are not interchangeable, and swapping two would
    reverse the sweep without changing a net. The two throws the drawing leaves
    dangling stay NC rather than being tidied onto the bus.
    """
    from sch2kicad.circuit import load
    from sch2kicad.parts import load_profile, resolve_all

    gargler = HERE.parent / "samples" / "circuits" / "gargler-escobedo.circuit.json"
    res = resolve_all(load(gargler), load_profile("tht"), SymbolResolver())
    poles = {r.part.unit: r for r in res if r.ref == "SW1"}
    assert set(poles) == {"A", "B"}
    for unit, pole in poles.items():
        assert pole.problems == [], pole.problems
        assert pole.footprint == "2025_PedalDivision_Switches:2P4T.MINI"
        assert pole.pinmap == {"com": unit, "1": f"{unit}1", "2": f"{unit}2", "3": f"{unit}3"}
        assert pole.swappable == [], "a rotary's throws are an order, not a pair"
    # A-down and B-up go nowhere on the sheet
    assert poles["A"].part.pins["3"].net is None
    assert poles["B"].part.pins["1"].net is None


def test_a_logic_chips_vdd_finds_the_pin_kicad_calls_vcc():
    """The Christine writes its 4049's supply as VDD and VSS; KiCad's 4049 names pin 1
    VCC. The chip's VSS goes to the Power Starve pot, not ground, so it has to land."""
    from sch2kicad.circuit import load
    from sch2kicad.parts import load_profile, resolve_all
    c = load(HERE.parent / "samples" / "circuits" / "christine-tonegod.circuit.json")
    res = {r.part.key: r for r in resolve_all(c, load_profile("tht"), SymbolResolver())}
    assert res["U1G"].problems == []
    assert res["U1G"].pinmap == {"VDD": "1", "VSS": "8"}


def test_a_net_of_nothing_but_pads_is_not_an_erc_error():
    """The Halsted St OD carries each jack line out through one pad and back in through
    another; nothing on the board can drive that net. A pad beside a part still can."""
    from sch2kicad.verify import fold_pads_only
    errs = ["[pin_not_driven] Input pin not driven by any Output pins — Symbol H6 Pin 1 [1, Input, Line]",
            "[pin_not_driven] Input pin not driven by any Output pins — Symbol H9 Pin 1 [1, Input, Line]"]
    nets = {"J_IN": {"H6.1", "Switch_Connectors1.5"}, "X": {"H9.1", "R1.2"}}
    keep, notes = fold_pads_only(errs, nets, {"H6", "H9", "Switch_Connectors1"})
    assert keep == errs[1:] and len(notes) == 1


def test_a_chip_input_fed_only_from_a_pad_is_not_an_erc_error():
    """The Seppuku Memory Loss wires its input jack straight to the LM386's pin 2: the
    guitar drives it, off the board. A chip input on a net with no pad, or with a
    pad and another part, stays an error."""
    from sch2kicad.verify import fold_fed_off_board
    errs = ["[pin_not_driven] Input pin not driven by any Output pins — Symbol IC1 Pin 2 [-, Input, Line]",
            "[pin_not_driven] Input pin not driven by any Output pins — Symbol IC2 Pin 3 [+, Input, Line]",
            "[pin_not_driven] Input pin not driven by any Output pins — Symbol IC3 Pin 3 [+, Input, Line]"]
    nets = {"IN": {"IC1.2", "Switch_Connectors1.1"}, "X": {"IC2.3", "H2.1", "R4.2"}, "Y": {"IC3.3", "IC3.2"}}
    pintypes = {"IC1.2": "input", "Switch_Connectors1.1": "input", "IC2.3": "input", "H2.1": "input",
                "R4.2": "passive", "IC3.3": "input", "IC3.2": "input"}
    keep, notes = fold_fed_off_board(errs, nets, {"Switch_Connectors1", "H2"}, pintypes)
    assert keep == errs[1:] and len(notes) == 1 and "IC1.2 is fed" in notes[0]


def test_one_parts_own_outputs_tied_together_are_not_an_erc_error():
    """The Pocket Comb ties the MN3207's two outputs, 7 and 8, as BBD circuits do. Two
    different parts' outputs on one net are still a short."""
    from sch2kicad.verify import fold_tied_outputs
    errs = ["[pin_to_pin] Pins of type Output and Output are connected — Symbol IC2 Pin 7 [OUT1, Output, Line]; Symbol IC2 Pin 8 [OUT2, Output, Line]",
            "[pin_to_pin] Pins of type Output and Output are connected — Symbol U1 Pin 1 [~, Output, Line]; Symbol U2 Pin 1 [~, Output, Line]"]
    nets = {"BBD_OUT": {"IC2.7", "IC2.8", "R17.1"}, "X": {"U1.1", "U2.1"}}
    pintypes = {"IC2.7": "output", "IC2.8": "output", "U1.1": "output", "U2.1": "output"}
    keep, notes = fold_tied_outputs(errs, nets, pintypes)
    assert keep == errs[1:] and len(notes) == 1


def test_one_parts_own_inputs_strapped_together_are_not_an_erc_error():
    """The Seppuku Space Fuzz straps the LM386's gain pins, 1 and 8, for gain 200: a
    net of only that chip's inputs. An input on a net with another part (even another
    chip's input), beside a pin that is not an input, or alone, stays an error."""
    from sch2kicad.verify import fold_strapped_inputs
    errs = ["[pin_not_driven] Input pin not driven by any Output pins — Symbol IC1 Pin 1 [GAIN, Input, Line]",
            "[pin_not_driven] Input pin not driven by any Output pins — Symbol IC2 Pin 3 [+, Input, Line]",
            "[pin_not_driven] Input pin not driven by any Output pins — Symbol IC3 Pin 2 [-, Input, Line]",
            "[pin_not_driven] Input pin not driven by any Output pins — Symbol IC4 Pin 3 [+, Input, Line]",
            "[pin_not_driven] Input pin not driven by any Output pins — Symbol IC6 Pin 1 [~, Input, Line]"]
    nets = {"GAIN": {"IC1.1", "IC1.8"}, "X": {"IC2.3", "IC2.2", "R1.1"}, "Y": {"IC3.2"},
            "Z": {"IC4.3", "IC5.3"}, "W": {"IC6.1", "IC6.5"}}
    pintypes = {"IC1.1": "input", "IC1.8": "input", "IC2.3": "input", "IC2.2": "input",
                "R1.1": "passive", "IC3.2": "input", "IC4.3": "input", "IC5.3": "input",
                "IC6.1": "input", "IC6.5": "passive"}
    keep, notes = fold_strapped_inputs(errs, nets, pintypes)
    assert keep == errs[1:] and len(notes) == 1 and "IC1.1 and IC1.8" in notes[0]


def test_a_chips_gnd_pin_on_a_declared_rail_is_not_an_erc_warning():
    """DOD's FX96 runs the MN3005's pin called GND on a filtered +9V (the chip is
    P-channel); the reading says V_BBD is a power rail, so that was meant. A GND pin
    on a signal net is still worth the warning."""
    from sch2kicad.verify import fold_ground_on_rail
    warns = ["[ground_pin_not_ground] Pin GND not connected to ground net — Symbol U4 Pin 1 [GND, Power input, Line]",
             "[ground_pin_not_ground] Pin GND not connected to ground net — Symbol U9 Pin 4 [GND, Power input, Line]"]
    expected = {"V_BBD": ["U4.1", "R25.2"], "N_7": ["U9.4", "R1.1"]}
    keep, notes = fold_ground_on_rail(warns, expected, {"V_BBD", "+9V"})
    assert keep == warns[1:] and len(notes) == 1 and "V_BBD" in notes[0]
