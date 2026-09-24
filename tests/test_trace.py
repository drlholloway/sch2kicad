"""The reading checked against the drawing it came from.

Each test ingests the source the way a conversion does, drops the finished reading
beside it, and asks whether the two agree — then plants a mistake and asks again,
because a check that never fails proves nothing.
"""

import copy
import shutil
from pathlib import Path

import pytest

from sch2kicad.circuit import load
from sch2kicad.trace import crosscheck_circuit

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent / "samples"
CIRCUITS = SAMPLES / "circuits"


def _ready(tmp_path, source: str, circuit: str, page: int | None = None):
    """Ingest a source and put the reading of it in the work directory."""
    from sch2kicad.ingest import ingest
    src = SAMPLES / source
    if not src.exists():
        pytest.skip(f"{source} is third-party and not in the repo")
    work = tmp_path / "work"
    ingest(src, work, page=page)
    shutil.copy(CIRCUITS / circuit, work / "circuit.json")
    return load(work / "circuit.json")


def _wrong_net(c, ref: str, pin: str, net: str):
    bad = copy.deepcopy(c)
    for p in bad.parts:
        if p.key == ref:
            p.pins[pin].net = net
    return bad


def test_vector_page_agrees_with_its_reading(tmp_path):
    """Aion FX draw in vectors: the page's own strokes, joined as a schematic joins
    them, give the same nets as the reading — the LED added to the board aside."""
    c = _ready(tmp_path, "tri-vibe_documentation.pdf", "trivibe-aionfx.circuit.json", page=6)
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.checked >= 155, rep.text()
    # the status LED and its resistor are on the board by choice, not in Aion's drawing
    assert {u.split(" at ")[0] for u in rep.unmatched} == {"LED.A", "LED.K", "LEDR.1", "LEDR.2"}


def test_vector_page_catches_a_wrong_net(tmp_path):
    c = _ready(tmp_path, "tri-vibe_documentation.pdf", "trivibe-aionfx.circuit.json", page=6)
    rep = crosscheck_circuit(_wrong_net(c, "R5", "2", "GND"))
    assert rep.disagreements, "a pin moved to another net went unnoticed"
    assert any("OTA1_BIAS" in m and "GND" in m for m in rep.merges), rep.text()


def test_vector_page_catches_two_rails_mixed_up(tmp_path):
    """VA and VB are drawn as flags, so only their names keep them apart."""
    c = _ready(tmp_path, "tri-vibe_documentation.pdf", "trivibe-aionfx.circuit.json", page=6)
    bad = copy.deepcopy(c)
    for p in bad.parts:
        for pin in p.pins.values():
            if pin.net == "VB":
                pin.net = "VA"
    rep = crosscheck_circuit(bad)
    assert any("writes VB" in m for m in rep.misnamed), rep.text()


def test_raster_page_agrees_with_its_reading(tmp_path):
    """Parasit draw in KiCad and the page arrives as pixels: wires are the long thin
    runs of ink, and the blue a symbol is drawn in is not ink at all."""
    c = _ready(tmp_path, "Higgs-Particle-Phaser-doc.pdf", "higgs-phaser-parasit.circuit.json", page=8)
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.checked >= 120, rep.text()


def test_raster_page_catches_a_crossing_read_as_a_junction(tmp_path):
    """The clock crosses the modulator's input; joining them is the classic misread."""
    c = _ready(tmp_path, "Higgs-Particle-Phaser-doc.pdf", "higgs-phaser-parasit.circuit.json", page=8)
    rep = crosscheck_circuit(_wrong_net(c, "C1", "1", "CLK"))
    assert rep.disagreements, "a crossing read as a junction went unnoticed"
    assert any("MOD_IN" in m for m in rep.splits + rep.merges), rep.text()


def test_hand_drawn_png_agrees_with_its_reading(tmp_path):
    """Chuck's Boneyard export a PNG whose wires come out grey once scaled."""
    c = _ready(tmp_path, "EHX Ripped Speaker Fuzz - cb mod v1.0.png",
               "ripped-speaker-cb-mod.circuit.json")
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.checked >= 85, rep.text()


def test_a_sheet_drawn_in_outlines_agrees_with_its_reading(tmp_path):
    """Tim Escobedo draws 460 px wide, and every part is a hollow shape whose black
    boundary touches both its leads. Walk that boundary and you join the two nets
    the part is there to keep apart — so a body has to be known for a body."""
    c = _ready(tmp_path, "wobbletron_1.gif", "wobbletron-escobedo.circuit.json")
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.unmatched == [], rep.text()


def test_an_outline_does_not_carry_current(tmp_path):
    """R7's box is 6 px across and its corners sit 4 px from its own pins. If the
    tracer walks round it, the 2.2M's two ends are one thing in the drawing and
    reading them as one net passes unnoticed — which is the whole fault."""
    c = _ready(tmp_path, "wobbletron_1.gif", "wobbletron-escobedo.circuit.json")
    rep = crosscheck_circuit(_wrong_net(c, "R7", "2", "OSC_C"))
    assert rep.disagreements, "a resistor shorted end to end went unnoticed"
    assert any("OSC_C" in m and "Q3_B" in m for m in rep.merges), rep.text()


def test_a_filled_body_does_not_carry_current(tmp_path):
    """The LED is a solid triangle sitting on a solid bar, so one column of pixels
    runs from the resistor above it, straight through the part, to the rail below.
    Read as a conductor it makes anode and cathode the same node — and no planted
    fault shows that, because the reading it agrees with is the wrong one."""
    c = _ready(tmp_path, "wobbletron_1.gif", "wobbletron-escobedo.circuit.json")
    rep = crosscheck_circuit(c)
    anode, cathode = rep.pins["D1.A"][1], rep.pins["D1.K"][1]
    assert anode is not None and cathode is not None, rep.text()
    assert anode != cathode, "the LED's own body was traced as a wire across it"


def test_a_rail_name_goes_to_the_wire_it_is_written_against(tmp_path):
    """\"Power\" is written along the wire from D1 to the chip's V+, 5 px above it.
    The V- wire's corner happens to sit 14 px below the same word, and the V+ wire
    ends off to one side — so scoring a name by where its wire *ends* hands Power
    to ground, and since names are what join a rail's pins, welds the two rails."""
    c = _ready(tmp_path, "22358.png", "disorder-22358.circuit.json")
    rep = crosscheck_circuit(c)
    assert not any("GND" in m and "Power" in m for m in rep.merges), rep.text()
    assert rep.misnamed == [], rep.text()


def _source(tmp_path, name: str):
    """Ingest a sample and hand back its upright page, as pixels and as a path."""
    from sch2kicad.ingest import ingest
    src = SAMPLES / name
    if not src.exists():
        pytest.skip(f"{name} is third-party and not in the repo")
    ingest(src, tmp_path / "work")
    import numpy
    from PIL import Image
    page = tmp_path / "work" / "source.png"
    with Image.open(page) as im:
        return numpy.asarray(im.convert("RGB")).astype(numpy.int16), page


def test_the_wire_colour_is_read_from_the_drawing(tmp_path):
    """22359 draws its wires green, its symbols red and its text black. The older
    test — dark and colourless — kept the text, dropped the wires it needed and
    traced 4 of its 78 pins, because a saturated green's darkest channel is bright."""
    from sch2kicad.trace import raster_segments, wire_ink
    rgb, page = _source(tmp_path, "22359.png")
    ink = wire_ink(rgb, max(5, round(rgb.shape[1] / 320)))
    assert ink[118, 340], "the green rail is not ink"       # (68, 193, 151)
    assert not ink[611, 1069], "a red symbol is ink"        # (215, 66, 66)
    assert not ink[358, 622], "black text is ink"           # (0, 0, 0)
    # and the sheet's own output bus comes back whole, which is what tells the
    # difference: with the wires invisible the longest run left is a letter
    segs, _ = raster_segments(page)
    assert max(s.length() for s in segs) >= 500, "the output bus was not traced"


def test_a_page_drawn_in_one_colour_keeps_all_of_it(tmp_path):
    """Escobedo draws in black on white. Picking a colour has to pick that one, or
    every sheet that worked before this stops working."""
    from sch2kicad.trace import wire_ink
    rgb, _page = _source(tmp_path, "wobbletron_1.gif")
    ink = wire_ink(rgb, 5)
    was = (rgb.max(axis=2) <= 170) & (rgb.max(axis=2) - rgb.min(axis=2) <= 60)
    assert ink.sum() == was.sum() and (ink == was).all()
