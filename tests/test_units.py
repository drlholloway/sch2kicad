"""Fast tests: no KiCad needed."""

import pytest

from sch2kicad import sexpr
from sch2kicad.circuit import parse_value
from sch2kicad.geometry import Orient, apply, matrix
from sch2kicad.parts import canonical_pin, lookup_pinout, pinout_assumption
from sch2kicad.router import normalise


def _have_kicad():
    from sch2kicad.kicad_env import kicad_cli
    from sch2kicad.symlib import SymbolResolver
    try:
        kicad_cli()
        return SymbolResolver().exists("2025_PedalDivision_Resistors:TH_Resistor_1/4w")
    except Exception:
        return False


needs_kicad = pytest.mark.skipif(not _have_kicad(), reason="needs KiCad 10 and the Pedal Division library")


def test_sexpr_round_trip():
    text = '(kicad_sch (version 20260306) (paper "A4") (text "a \\"quoted\\" word\\nnext" (at 1.27 -2.54 0)))'
    node = sexpr.parse(text)
    assert node[0] == "kicad_sch"
    t = sexpr.child(node, "text")
    assert t[1] == 'a "quoted" word\nnext'
    again = sexpr.parse(sexpr.dumps(node))
    assert again == node


def test_number_format():
    assert sexpr.fmt_num(1.27) == "1.27"
    assert sexpr.fmt_num(-0.0) == "0"
    assert sexpr.fmt_num(59.690000001) == "59.69"


def test_orientation_matches_kicad():
    # library Y is up, sheet Y is down
    assert apply(matrix(0), 3.81, 0) == (3.81, 0)
    assert apply(matrix(0), 0, 3.81) == (0, -3.81)
    # 90 rotates counter-clockwise on screen: a pin on the right ends up on top
    assert apply(matrix(90), 3.81, 0) == (0, -3.81)
    assert apply(matrix(270), 3.81, 0) == (0, 3.81)
    # mirror y flips left/right, mirror x flips up/down
    assert apply(matrix(0, "y"), 3.81, 0) == (-3.81, 0)
    assert apply(matrix(0, "x"), 0, 3.81) == (0, 3.81)
    assert len({Orient(a, m).m for a in (0, 90, 180, 270) for m in (None, "x", "y")}) == 8


def test_values():
    approx = pytest.approx
    assert parse_value("4k7") == approx(4700)
    assert parse_value("2M2") == approx(2.2e6)
    assert parse_value("3.3nF") == approx(3.3e-9)
    assert parse_value("100uF") == approx(100e-6)
    assert parse_value("A100K") == approx(100e3)
    assert parse_value("100kB") == approx(100e3)
    assert parse_value("10") == approx(10)
    assert parse_value("4n7") == approx(4.7e-9)
    assert parse_value("TL072") is None
    # DOD and most US sheets drop the leading zero; before, ".10uF" and ".1uF" could
    # not be compared at all, and ".047uF" matched its parts-list row only as text
    assert parse_value(".047uF") == approx(4.7e-8)
    assert parse_value(".10uF") == parse_value(".1uF") == approx(1e-7)
    assert parse_value(".") is None


def test_pinout_table():
    assert lookup_pinout("npn", "2N5089") == ("EBC", None)
    assert lookup_pinout("npn", "BC549C") == ("CBE", None)
    assert lookup_pinout("nmos", "BS170") == ("DGS", None)
    # 2N2222 ships both ways; through-hole builds assume E-B-C, and say so
    assert lookup_pinout("npn", "2N2222") == ("EBC", None)
    assert "C-B-E" in pinout_assumption("npn", "2N2222A")
    assert lookup_pinout("npn", "P2N2222A") == ("CBE", None)
    assert lookup_pinout("npn", "MPS2222") == ("EBC", None)
    assert lookup_pinout("npn", "MPS2222A") == ("EBC", None)
    assert pinout_assumption("npn", "2N5089") is None
    order, why = lookup_pinout("npn", "MP38A")
    assert order is None and why
    assert lookup_pinout("npn", "XYZ123") == (None, None)


def test_pin_synonyms():
    assert canonical_pin("pot", "1") == "ccw"
    assert canonical_pin("pot", "CW") == "cw"
    assert canonical_pin("capacitor_polarized", "pos") == "+"
    assert canonical_pin("diode", "k") == "K"


def test_the_ota_bias_pin_leaves_from_the_bottom():
    """Why the tool ships its own LM13700 at all.

    KiCad's brings the amplifier bias current (pins 1 and 16) out on the right, a
    step below the output, so the wire from the LFO has to cross the signal it is
    meant to control. Ours takes it out of the base of the triangle, which is where
    the datasheet and every pedal schematic put it.
    """
    from pathlib import Path

    from sch2kicad.symlib import _parse_pins

    lib = Path(__file__).resolve().parent.parent / "src" / "sch2kicad" / "data" / "symbols" / "sch2kicad.kicad_sym"
    node = next(s for s in sexpr.children(sexpr.parse(lib.read_text()), "symbol") if str(s[1]) == "LM13700")
    pins = {p.number: p for p in _parse_pins(node)}
    for bias, out, plus, minus in (("1", "5", "3", "4"), ("16", "12", "14", "13")):
        assert pins[bias].outward() == (0, -1), f"pin {bias} should leave from the bottom"
        assert pins[out].outward() == (1, 0), f"pin {out} should leave to the right"
        assert pins[plus].outward() == pins[minus].outward() == (-1, 0)
    # the drawing's IC4A is pins 1-5, and on this symbol that really is unit A
    assert pins["1"].unit == 1 and pins["12"].unit == 2
    assert pins["6"].name == "V-" and pins["11"].name == "V+"


def test_normalise_splits_t_joins():
    # a horizontal run with a wire ending on its middle: split + one junction
    segs = [((0.0, 0.0), (5.08, 0.0)), ((2.54, 0.0), (2.54, 2.54))]
    out, juncs = normalise(segs, pins=[(0.0, 0.0), (5.08, 0.0), (2.54, 2.54)])
    assert len(out) == 3
    assert juncs == [(2.54, 0.0)]


@needs_kicad
def test_a_p_channel_jfet_keeps_its_own_lead_order():
    """J176 is D-G-S through-hole, not the house D-S-G. A part whose order differs
    gets the stock KiCad symbol for that order on the same TO-92 footprint, so the
    board is right without bending legs — the path added in 2b5da67, now exercised
    by a real circuit (the Gargler's Q2) rather than only by a synthetic one."""
    from sch2kicad.parts import resolve_all, load_profile
    from sch2kicad.symlib import SymbolResolver
    from sch2kicad.circuit import from_dict
    c = from_dict({"schema": "sch2kicad/circuit@1", "name": "t", "parts": [
        {"ref": "Q1", "type": "pjfet", "part": "J176",
         "pins": {"G": ["G", 10, 20], "D": ["D", 20, 10], "S": ["S", 20, 30]}}]})
    got = resolve_all(c, load_profile("tht"), SymbolResolver())[0]
    assert got.lib_id == "Device:Q_PJFET_DGS", got.lib_id
    assert "TO-92" in got.footprint, got.footprint
    assert got.problems == []


def test_switch_actions_fold_to_one_spelling():
    """Every way a publisher writes a toggle's action reads as the same action.

    It has to be read off text, because a netlist cannot carry it: com/a/b is
    identical for on/on, on/off/on and on/on/on, which are three different parts to
    order. Aion writes one switch two ways in one row — "SPDT cntr off" in the value
    column, "Toggle switch, SPDT on-off-on" in the type column — so a whole row has
    to read as well as a bare action.
    """
    from sch2kicad.circuit import parse_action
    assert parse_action("on/on/on") == "on/on/on"
    assert parse_action("ON-ON-ON") == "on/on/on"
    assert parse_action("3 position") == "on/on/on"
    assert parse_action("SPDT cntr off") == "on/off/on"
    assert parse_action("center off") == parse_action("centre off") == "on/off/on"
    assert parse_action("Toggle switch, SPDT on-off-on") == "on/off/on"
    assert parse_action("SPDT on/on") == "on/on"
    # the longest spelling wins: on-on-on is not the on-on inside it
    assert parse_action("DPDT on-on-on toggle") == "on/on/on"
    # nothing to find
    assert parse_action("3PDT") == parse_action('1/4" mono') == parse_action("100K") == ""


def test_a_switch_states_its_action_and_says_so_when_it_cannot():
    """The value on the sheet and the description name the action, or the build asks.

    A wrong action does not kill a board the way a wrong pinout does — the setting
    just isn't there — so an unmarked one is a note and stays READY. But a one-pole
    switch cannot have three live positions, and claiming it can is a problem.
    """
    from sch2kicad.circuit import Part
    from sch2kicad.parts import _switch_action

    value, desc, notes, problems = _switch_action(Part(ref="SW1", type="switch_dpdt",
                                                       action="on/on/on"), "switch_dpdt")
    assert value == "DPDT on/on/on"
    assert desc == "DPDT switch, on/on/on — 3 positions, no off position"
    assert (notes, problems) == ([], [])

    value, desc, notes, problems = _switch_action(Part(ref="S2", type="switch_spdt"), "switch_spdt")
    assert (value, desc, problems) == ("SPDT", "", [])
    assert notes == ["S2: SPDT action not marked on the drawing — confirm "
                     "on/on vs on/off/on before ordering"]      # never on/on/on: one pole

    _, _, _, problems = _switch_action(Part(ref="S2", type="switch_spdt", action="on/on/on"),
                                       "switch_spdt")
    assert problems and "needs two poles" in problems[0]

    _, _, _, problems = _switch_action(Part(ref="S2", type="switch_spdt", action="on/maybe"),
                                       "switch_spdt")
    assert problems and "is not one of" in problems[0]


def test_a_zener_lands_on_a_house_footprint():
    """The Fix'd Fuzz brought the corpus its first zener. The profiles mapped every
    other diode and not this one, so it fell through to KiCad's own D_Zener — which
    carries no footprint, and a part with no footprint is not a board. It keeps that
    symbol, so nobody fits a signal diode where it goes, on the house packages."""
    from sch2kicad.circuit import Part
    from sch2kicad.parts import _footprint_rule, load_profile
    tht, smd = load_profile("tht"), load_profile("smd")
    glass = Part(ref="D1", type="zener", value="5245", part="1N5245")      # 500 mW
    watt = Part(ref="D2", type="zener", value="4742", part="1N4742A")      # 1 W
    assert _footprint_rule(tht, "zener", glass) == "2025_PedalDivision_Diodes:DO-35_P7.62mm"
    assert _footprint_rule(tht, "zener", watt) == "2025_PedalDivision_Diodes:DO-41_P7.62mm"
    assert _footprint_rule(smd, "zener", glass) == "Diode_SMD:D_SOD-123"


def test_an_on_off_toggle_lands_on_the_house_spdt():
    """The Gnomeratron's SW1 and SW6 are on/off toggles, and the house library has no
    SPST, so KiCad's SW_SPST came through with no footprint and the build was not
    READY. The part to order is the mini SPDT with a throw unused: SW_SPST's pins 1
    and 2 land on a throw and the middle lug, which is the common."""
    from sch2kicad.circuit import Part
    from sch2kicad.parts import _footprint_rule, load_profile
    sw = Part(ref="SW1", type="switch_spst", action="on/off")
    for profile in ("tht", "smd"):
        assert _footprint_rule(load_profile(profile), "switch_spst", sw) == \
            "2025_PedalDivision_Switches:Mini Switch SPDT"


@needs_kicad
def test_with_no_true_bypass_the_jacks_come_in_on_sockets():
    """The Abductor II switches its delay send with a JFET and wires its jacks straight
    to the board: no 3PDT. On the 3PDT pad array its IN and OUT would sit among pads
    for a switch nobody fits, so each jack comes in on the house socket, tip to the
    signal and the other pin to ground — the way the house boards wire their sockets
    to the array's JI and JO pads."""
    from sch2kicad.build import _port_parts
    from sch2kicad.circuit import from_dict
    from sch2kicad.parts import load_profile
    from sch2kicad.symlib import SymbolResolver

    def pads(true_bypass: bool, sockets: bool = True, jacks: bool = True) -> dict[str, tuple]:
        circuit = from_dict({"schema": "sch2kicad/circuit@1", "name": "t", "parts": [], "jacks": jacks,
                             "true_bypass": true_bypass, "nets": {"GND": {"kind": "ground"}},
                             "ports": [{"name": "IN", "net": "IN", "at": [0, 0]},
                                       {"name": "OUTPUT", "net": "OUT", "at": [50, 0]}]})
        profile = load_profile("tht")
        if not sockets:
            profile.data["ports"] = {k: v for k, v in profile.data["ports"].items() if k != "jacks"}
        ports, problems = _port_parts(circuit, profile, SymbolResolver())
        assert not problems
        return {r.part.ref + r.part.unit: (r.lib_id.split(":")[1], r.value,
                                           {r.pinmap[k]: v.net for k, v in r.part.pins.items()})
                for r in ports}

    array = ("3pdt_Switch_Connectors+Jacks", "")
    assert pads(True) == {"Switch_Connectors1A": array + ({"1": "IN"},),
                          "Switch_Connectors1D": array + ({"4": "OUT"},),
                          "Switch_Connectors1B": array + ({"2": "GND"},),
                          "Switch_Connectors1C": array + ({"3": None},),
                          "Switch_Connectors1E": array + ({"5": "JIN"},),
                          "Switch_Connectors1F": array + ({"6": "JOUT"},),
                          # with a 3PDT the jacks come in on the same sockets, on JI and JO
                          # (Lane's Bulgarian Fuzz and Memory Lapse)
                          "J1": ("Conn_01x02_Socket_Jack_In", "JIN", {"1": "JIN", "2": "GND"}),
                          "J2": ("Conn_01x02_Socket_Jack_Out", "JOUT", {"1": "JOUT", "2": "GND"})}
    # where the drawing shows no jacks, the plain array: the jacks wire to the footswitch
    # off the board, with no JI/JO lugs and no sockets (Lane's Colonel Angus)
    plain = ("3pdt_Switch_Connectors", "")
    assert pads(True, jacks=False) == {"Switch_Connectors1A": plain + ({"1": "IN"},),
                                       "Switch_Connectors1D": plain + ({"4": "OUT"},),
                                       "Switch_Connectors1B": plain + ({"2": "GND"},),
                                       "Switch_Connectors1C": plain + ({"3": None},)}
    assert pads(False) == {"J1": ("Conn_01x02_Socket_Jack_In", "JIN", {"1": "IN", "2": "GND"}),
                           "J2": ("Conn_01x02_Socket_Jack_Out", "JOUT", {"1": "OUT", "2": "GND"})}
    # a profile with no sockets still keeps the jacks off a 3PDT that is not there
    assert pads(False, sockets=False) == {"H1": ("Small_Pad_Round", "IN", {"1": "IN"}),
                                          "H2": ("Small_Pad_Round", "OUTPUT", {"1": "OUT"})}


def test_a_drawing_shows_its_jacks_by_a_jack_pad():
    """A JI or JO pad, or a jack's own ground pad (the Bulgarian Fuzz's GNDJ1), says the
    drawing has jacks; IN, OUT, a plain GND or a LED+ pad does not. circuit.json can say."""
    from sch2kicad.circuit import from_dict

    def jacks(*names, **extra):
        return from_dict({"schema": "sch2kicad/circuit@1", "name": "t", "parts": [], **extra,
                          "ports": [{"name": n, "net": n, "at": [0, 0]} for n in names]}).jacks

    for shown in ("JI", "JO", "J_IN", "J_OUT", "GNDJ1", "GNDJ", "JACK IN", "INPUT JACK"):
        assert jacks("IN", "OUT", shown), shown
    assert not jacks("IN", "OUT", "GND", "GNDL", "LED+", "9V", "SW")
    assert jacks("IN", "OUT", jacks=True) and not jacks("JI", jacks=False)


def test_a_pot_value_nobody_stocks_is_said_out_loud():
    """Pots come in 1K, 2K, 5K, 10K, 20K, 25K, 50K, 100K, 250K, 500K, 1M and 2M,
    in every taper. The Abductor II draws DEPTH as 47k and its parts list says 47kB:
    a value that has to be hunted for or quietly swapped at ordering time. The build
    says so and names the nearest stock value, keeping the taper."""
    from sch2kicad.circuit import Part
    from sch2kicad.parts import _stock_pot

    def note(value):
        return _stock_pot(Part(ref="VR1", type="pot", value=value))

    assert note("B47k").endswith("the nearest is B50K")
    assert note("47kA").endswith("the nearest is A50K")
    assert note("C220K").endswith("the nearest is C250K")
    # the Acapulco writes its 1M audio pot "1m A": no pot is a milliohm
    for stocked in ("A1M", "1m A", "B250K", "C10K", "W100K", "B2M", "25K"):
        assert note(stocked) is None, stocked
