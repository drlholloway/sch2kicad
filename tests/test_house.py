"""Lane's house style: what Lane did by hand to the Bulgarian Fuzz and the Memory Lapse."""

from pathlib import Path

from sch2kicad import bom, house
from sch2kicad.build import Builder
from sch2kicad.circuit import from_dict, load
from sch2kicad.parts import load_profile

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent / "samples"
PORTS = load_profile("tht").ports


def _bulgarian(**extra) -> dict:
    """The Bulgarian Fuzz's supply, LED, knobs and pads, as the drawing names them."""
    def p(ref, typ, val, pins, **kw):
        return {"ref": ref, "type": typ, "value": val,
                "pins": {k: [n, x, 0] for x, (k, n) in enumerate(pins.items())}, **kw}
    return {
        "name": "Bulgarian", "nets": {"GND": {"kind": "ground"}, "+9V": {"kind": "power"}},
        "parts": [
            p("GAIN", "pot", "B1M", {"ccw": "A", "wiper": "B", "cw": "B"}, label="GAIN"),
            p("TONE", "pot", "A25k", {"ccw": "B", "wiper": "C", "cw": "C"}),
            p("SW1", "switch_spdt", "", {"com": "A", "a": "C", "b": "NC"}, action="on/on"),
            p("R1", "resistor", "10k", {"1": "+9V", "2": "A"}),
            p("D1", "schottky", "1N5817", {"A": "9V_IN", "K": "+9V"}),
            p("R8", "resistor", "6k8", {"1": "+9V", "2": "LED_A"}),
            p("LED", "led", "LED", {"A": "LED_A", "K": "CTRL"}),
        ],
        "ports": [{"name": n, "net": net, "at": [0, 0]} for n, net in
                  [("IN", "A"), ("OUT", "C"), ("9V", "9V_IN"), ("CTRL", "CTRL"),
                   ("GND", "GND"), ("GNDL", "GND"), ("GNDJ1", "GND"), ("GNDJ2", "GND")]],
        "blocks": [{"name": "Power", "parts": ["D1", "R8", "LED"]}],
        **extra,
    }


def test_knobs_are_vr_and_the_supply_is_numbered_from_100():
    """Lane renamed GAIN to VR1 (keeping GAIN as its label), D1 to D100, the status
    LED to D101 and its resistor to R100; the toggle was SW1 already."""
    c, notes = house.apply(from_dict(_bulgarian()), PORTS)
    refs = {p.ref: p for p in c.parts}
    assert {"VR1", "VR2", "SW1", "D100", "D101", "R100", "R1"} == set(refs)
    assert refs["VR1"].label == "GAIN" and refs["VR2"].label == "TONE"
    assert refs["D101"].type == "led" and refs["R100"].value == "6k8"
    assert c.blocks[0].parts == ["D100", "R100", "D101"]
    assert c.renamed["VR1"] == "GAIN" and c.renamed["D100"] == "D1"
    assert any("GAIN → VR1" in n for n in notes)


def test_loose_ground_pads_go_and_ctrl_is_the_sw_lug():
    """The 3PDT array's GND lug and the jack sockets carry ground; Lane deleted the
    sheet's four ground pads. CTRL is the LED's cathode: the footswitch's SW lug."""
    c, notes = house.apply(from_dict(_bulgarian()), PORTS)
    assert [p.name for p in c.ports] == ["IN", "OUT", "9V", "CTRL"]
    assert any("GNDJ2" in n for n in notes)
    assert not any(p.type == "led" and p.value == house.LED_VALUE for p in c.parts)   # it has one


def test_a_board_with_no_status_led_gets_one():
    """The Memory Lapse layout has no status LED; Lane added a red one, 4K7 from the
    protected supply (VCC, past D1) to the SW lug."""
    c, notes = house.apply(load(SAMPLES / "circuits" / "memory-lapse.circuit.json"), PORTS)
    refs = {p.ref: p for p in c.parts}
    led, res = refs["D101"], refs["R100"]
    assert led.value == "RED" and res.value == "4K7"
    assert {pr.net for pr in res.pins.values()} == {"VCC", led.pins["A"].net}
    sw = next(p for p in c.ports if p.name == "SW")
    assert led.pins["K"].net == sw.net
    assert {"D100", "REG100", "C100", "C101"} <= set(refs)
    assert refs["C100"].pins["+"].net == "VCC" and refs["C101"].pins["+"].net == "V5"
    assert set(c.added) == {"D101", "R100"}


def test_no_footswitch_no_status_led():
    """With no true bypass there is no SW lug to hang one from."""
    data = _bulgarian(true_bypass=False)
    data["parts"] = [p for p in data["parts"] if p["ref"] not in ("R8", "LED")]
    data["ports"] = [p for p in data["ports"] if p["name"] != "CTRL"]
    c, _ = house.apply(from_dict(data), PORTS)
    assert not any(p.type == "led" for p in c.parts)
    assert any(p.name == "GNDJ1" for p in c.ports)       # nothing replaces them


def test_the_parts_list_still_finds_a_renamed_part():
    """A parts list says R8 and GAIN, the board says R100 and VR1."""
    parts = {"R100": {"value": "6k8", "type": "resistor", "was": "R8"},
             "VR1": {"value": "B1M", "type": "pot", "label": "GAIN", "was": "GAIN"}}
    rows = [{"ref": "R8", "value": "6k8", "type": "resistor"}, {"ref": "GAIN", "value": "B1M", "type": "pot"}]
    chk = bom.check(parts, rows)
    assert chk.matched == 2 and not chk.problems and not chk.warnings



def test_the_parts_list_finds_an_added_status_led():
    """Sub Octave's list has the LED resistor as R100 4K7 and no LED: house style's
    R100 meets its row, and the added D101 is not a part the list forgot."""
    parts = {"R100": {"value": "4K7", "type": "resistor", "added": True},
             "D101": {"value": "RED", "type": "led", "added": True}}
    chk = bom.check(parts, [{"ref": "R100", "value": "4K7", "type": "Resistor, 1/4W"}])
    assert chk.matched == 1 and not chk.problems and not chk.warnings

def test_power_led_and_jacks_stand_in_the_top_left_corner(tmp_path):
    """Lane's corner: the LED row (rail, R100, D101, SW lug, GND lug) above the power
    row, JIN and JOUT under the POWER socket with JI and JO at their tips, each
    socket's ground lying along its pin 2."""
    prof = load_profile("tht")
    ml = load(SAMPLES / "circuits" / "memory-lapse.circuit.json")
    ml.jacks = True                    # its layout shows none; the sockets come with jacks
    c, hn = house.apply(ml, prof.ports)
    b = Builder(c, prof, "corner", house_notes=hn)
    b.run(tmp_path)
    at = {pl.res.ref: pl for pl in b.layout.placed if not pl.res.ref.startswith("Switch")}
    sock, d100, r100, d101 = at["J1"], at["D100"], at["R100"], at["D101"]
    row_y = sock.pin_pos(sock.pinmap["+"])[1]
    assert d100.pin_pos(d100.pinmap["K"])[1] == row_y
    led_y = d101.pin_pos(d101.pinmap["A"])[1]
    assert led_y < row_y and all(abs(r100.pin_pos(q.number)[1] - led_y) < 1e-3 for q in r100.pins())
    sw = next(pl for pl in b.layout.placed if pl.res.ref.startswith("Switch") and pl.pinmap["1"] == "3")
    assert abs(sw.pin_pos("3")[1] - led_y) < 1e-3 and sw.pin_pos("3")[0] > d101.pin_pos(d101.pinmap["K"])[0]
    jin = next(pl for pl in b.layout.placed if pl.res.value == "JIN")
    jout = next(pl for pl in b.layout.placed if pl.res.value == "JOUT")
    x = sock.pin_pos(sock.pinmap["+"])[0]
    assert jin.pin_pos(jin.pinmap["T"])[0] == x == jout.pin_pos(jout.pinmap["T"])[0]
    assert row_y < jin.pin_pos(jin.pinmap["T"])[1] < jout.pin_pos(jout.pinmap["T"])[1]
    ji = next(pl for pl in b.layout.placed if pl.res.ref.startswith("Switch") and pl.pinmap["1"] == "5")
    assert ji.pin_pos("5")[1] == jin.pin_pos(jin.pinmap["T"])[1]
    # each socket's ground lies along its pin 2, level with it (Lane's sockets)
    for jack in (jin, jout):
        r = jack.pin_pos(jack.pinmap["R"])
        g = [sy for sy in b.w.symbols if sy.lib.lib_id.endswith(":GND")
             and abs(sy.at[1] - r[1]) < 1e-3 and 0 < sy.at[0] - r[0] <= 5.08]
        assert len(g) == 1 and g[0].orient.angle in (90, 270), jack.res.ref
    # everything else below the corner's rows
    rest = [pl for pl in b.layout.placed if pl.res.part.type != "port" and pl.res.ref not in
            ("D100", "D101", "R100", "REG100", "C100", "C101") and not pl.res.is_supply_unit]
    assert min(pl.extent()[1] for pl in rest) > row_y


def test_an_led_pad_fed_through_a_resistor_is_the_status_led_off_the_board():
    """Chuck D. Bones brings the status LED off the board: RLED from the protected
    supply to an LED+ pad. That is the LED's resistor, R100, and no LED is added.
    Memory Lapse's LED+ is fed no such way (and its IN, with a bias resistor to VCC,
    is no LED pad)."""
    c, notes = house.apply(load(SAMPLES / "circuits" / "ripped-speaker-cb-mod.circuit.json"), PORTS)
    assert not any(p.type == "led" for p in c.parts) and not c.added
    assert c.renamed["R100"] == "RLED" and c.renamed["R101"] == "R21"
    assert not any("status LED added" in n for n in notes)
