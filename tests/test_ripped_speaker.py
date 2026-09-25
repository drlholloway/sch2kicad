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


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_every_sample_is_ready(tmp_path, circuit):
    rep = verify(build(circuit, tmp_path / "board").sch, render=False)
    assert rep.ok, rep.text()
    assert rep.erc_warnings == [], rep.text()


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_every_block_is_ready(tmp_path, circuit):
    from sch2kicad.blocks import save_blocks
    for sch in save_blocks(circuit, tmp_path / "blocks"):
        rep = verify(sch, render=False)
        assert rep.ok, rep.text()
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
    got = {u: {p.name: p.number for p in s.pins_for(u)} for u in range(1, 6)}
    assert got[1] == {"~": "1", "-": "2", "+": "3"}
    assert got[2] == {"+": "5", "-": "6", "~": "7"}
    assert got[3] == {"~": "8", "-": "9", "+": "10"}
    assert got[4] == {"+": "12", "-": "13", "~": "14"}
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
