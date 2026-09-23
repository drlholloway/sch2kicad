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
