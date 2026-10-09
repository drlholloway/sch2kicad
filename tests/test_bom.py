"""The parts-list check: reading a build doc's table, and comparing a drawing with it."""

import json
from pathlib import Path

import pytest

from sch2kicad import bom

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent / "samples"


def test_same_value():
    assert bom.same_value("resistor", "4K7", "4.7k")
    assert bom.same_value("resistor", "390R", "390")
    assert bom.same_value("capacitor", "100n", "100nF")
    assert bom.same_value("capacitor_polarized", "4u7", "4.7uF")
    assert bom.same_value("pot", "B100K", "100kB")
    assert bom.same_value("pot", "A100K", "B100K") is False          # taper is part of the value
    assert bom.same_value("resistor", "47K", "4K7") is False
    assert bom.same_value("opamp_dual", "4558", "RC4558")
    assert bom.same_value("npn", "2N5089", "2N5089")
    assert bom.same_value("npn", "2N5089", "MPS2222") is False
    assert bom.same_value("ic", "CD4007", "4007A")                     # DOD's spelling of a CD4007
    assert bom.same_value("ic", "CD4069UBE", "MC14069")
    assert bom.same_value("ic", "CD4007", "4017") is False
    assert bom.same_value("capacitor", ".1uF", ".10uF")                 # a US sheet's leading point
    assert bom.same_value("resistor", "10K", "10K-22K") is None       # a range: can't tell
    assert bom.same_value("led", "3mm red LED", "3mm LED")            # words of one in the other
    assert bom.same_value("led", "3mm red LED", "5mm") is None


def test_a_lowercase_m_on_a_resistor_is_mega():
    """Deadend Fx list the Ripplet's 2.2M as "2m2". Read as milli it could never
    agree; no pedal has a milliohm part, so on a resistance m is M."""
    assert bom.same_value("resistor", "2.2M", "2m2")
    assert bom.same_value("resistor", "4M7", "4m7")
    assert bom.same_value("pot", "B1M", "1mB")
    assert bom.same_value("resistor", "2.2K", "2m2") is False
    assert bom.same_value("capacitor", "2.2uF", "2m2") is False      # not on a capacitor


def test_a_taper_written_as_a_word():
    """Deadastronaut's parts list writes tapers as words ("1M LOG") and letters
    ("47kB") on one page, and the Abductor II's sheet writes "2M lin". The word
    is the taper; read as nothing, the value could not be compared at all."""
    assert bom.same_value("pot", "A1M", "1M LOG")
    assert bom.same_value("pot", "B1M", "1M LOG") is False
    assert bom.same_value("pot", "B2M", "2M lin")
    assert bom.same_value("pot", "A100k", "100k Linear") is False
    assert bom.same_value("pot", "C10K", "10k rev log")
    assert bom.same_value("pot", "A10K", "10k rev log") is False


PARTS = {
    "R1": {"value": "2M2", "type": "resistor"},
    "C1": {"value": "100n", "type": "capacitor"},
    "Q1": {"value": "2N5089", "type": "npn"},
    "SUSTAIN": {"value": "A100K", "type": "pot", "label": "SUSTAIN"},
    "LED": {"value": "LED", "type": "led"},
}
ROWS = [
    {"ref": "R1", "value": "2M2", "type": "Resistor, 1/4W"},
    {"ref": "C1", "value": "100n", "type": "Film capacitor"},
    {"ref": "Q1", "value": "2N5089", "type": "BJT transistor, NPN TO-92"},
    {"ref": "GAIN", "value": "A100K", "type": "16mm right-angle PCB mount pot"},
    {"ref": "IC1-S", "value": "DIP-8 socket", "type": "IC socket, DIP-8"},
]


def test_check_agrees():
    chk = bom.check(PARTS, ROWS)
    assert chk.rows == 4 and chk.matched == 4           # the socket is hardware
    assert chk.problems == []
    assert any("knobs named differently" in w for w in chk.warnings)
    assert any(w.startswith("LED ") for w in chk.warnings)


def test_untyped_rows_with_a_taper_are_knobs():
    """FuzzDog's Brain Bomb list has no type column: its BLEND 50KB is the layout's
    DEPTH B50K, a knob named differently, not a part missing. An untyped 10K is not
    a knob."""
    parts = {"DEPTH": {"value": "B50K", "type": "pot", "label": "Depth"},
             "R1": {"value": "10K", "type": "resistor"}}
    chk = bom.check(parts, [{"ref": "BLEND", "value": "50KB"}, {"ref": "R1", "value": "10K"}])
    assert chk.problems == [] and chk.matched == 2
    assert any("knobs named differently" in w for w in chk.warnings)
    assert not bom.Row("R9", "10K").is_pot


def test_check_catches_a_misread_and_a_missing_part():
    parts = dict(PARTS, R1={"value": "2K2", "type": "resistor"})
    rows = ROWS + [{"ref": "R2", "value": "470K", "type": "Resistor, 1/4W"}]
    chk = bom.check(parts, rows)
    assert any("R1: drawn 2K2, parts list says 2M2" in p for p in chk.problems)
    assert any(p.startswith("R2 (470K") for p in chk.problems)


def test_accepted_difference_is_a_note():
    parts = dict(PARTS, R1={"value": "2K2", "type": "resistor"})
    chk = bom.check(parts, ROWS, {"R1": "the drawing is right; the list has a typo"})
    assert chk.problems == []
    assert any("accepted: the drawing is right" in n for n in chk.notes)


@pytest.mark.skipif(not (SAMPLES / "ThunderPipe-PedalPCB.pdf").exists(), reason="third-party PDF not present")
def test_extract_pedalpcb_parts_list():
    rows = bom.extract(SAMPLES / "ThunderPipe-PedalPCB.pdf")
    assert len(rows) == 63 and bom.pages(rows) == "2, 3"
    d3 = next(r for r in rows if r.ref == "D3")
    assert (d3.value, d3.type) == ("GE", "Germanium Diode") and "1N34A" in d3.notes
    # the reading in samples/circuits carries the same list
    reading = json.loads((SAMPLES / "circuits" / "thunderpipe-pedalpcb.circuit.json").read_text())
    assert reading["bom"] == bom.rows_json(rows)


@pytest.mark.skipif(not (SAMPLES / "Theremin-Fuzz-doc.pdf").exists(), reason="third-party PDF not present")
def test_extract_parasit_parts_list():
    """Parasit Studio prints the list as columns of ref/value pairs under bold headings."""
    rows = bom.extract(SAMPLES / "Theremin-Fuzz-doc.pdf")
    assert len(rows) == 34 and bom.pages(rows) == "3"      # the prose around it is not a row
    by_ref = {r.ref: r for r in rows}
    assert (by_ref["U1"].value, by_ref["U1"].type) == ("CD4069UBE", "IC")
    assert (by_ref["BLEND"].value, by_ref["BLEND"].is_pot) == ("B50K", True)
    assert (by_ref["Octave"].value, by_ref["Octave"].type) == ("SPDT on/on", "Switch")
    assert (by_ref["D1"].value, by_ref["D1"].type) == ("1N4001", "Diode")
    assert (by_ref["R14"].value, by_ref["R14"].notes) == ("47K", "*")     # footnote kept apart
    assert by_ref["CLR"].value == "4.7K-22K"
    reading = json.loads((SAMPLES / "circuits" / "theremin-fuzz-parasit.circuit.json").read_text())
    assert reading["bom"] == bom.rows_json(rows)


@pytest.mark.skipif(not (SAMPLES / "tri-vibe_documentation.pdf").exists(), reason="third-party PDF not present")
def test_extract_aion_parts_list():
    rows = bom.extract(SAMPLES / "tri-vibe_documentation.pdf")
    assert len(rows) == 75 and bom.pages(rows) == "3, 4, 5"       # page footers are not rows
    assert {r.ref for r in rows if r.hardware} == {"IC1-S", "IC2-S", "IC3-S", "IC4-S", "IN", "OUT", "DC", "FSW", "ENC"}
    assert next(r for r in rows if r.ref == "IC4").type == "Transconductance amplifier, dual, DIP16"
    reading = json.loads((SAMPLES / "circuits" / "trivibe-aionfx.circuit.json").read_text())
    assert reading["bom"] == bom.rows_json(rows)


@pytest.mark.skipif(not (SAMPLES / "Higgs-Particle-Phaser-doc.pdf").exists(), reason="third-party PDF not present")
def test_extract_parasit_grid_with_notes_and_marks():
    """The Higgs list puts footnote marks and an aside inside the cell, and prose right
    under the list: neither may swallow a row or invent a column."""
    rows = bom.extract(SAMPLES / "Higgs-Particle-Phaser-doc.pdf")
    assert len(rows) == 50 and bom.pages(rows) == "4"
    by_ref = {r.ref: r for r in rows}
    assert by_ref["R1"].type == "Resistor"              # the heading sits over the ref column
    assert (by_ref["U4"].value, by_ref["U4"].type) == ("CD4517", "IC")
    assert (by_ref["C14"].value, by_ref["C14"].notes) == ("100nF", "**")     # mark before the value
    assert (by_ref["MAX"].value, by_ref["MAX"].notes) == ("50K", "(trimpot) ***")
    assert by_ref["R20"].value == "10K–22K" and by_ref["R21"].value == "10K–22K"
    assert by_ref["D1"].value == "1N4001"               # its own column, on R20's line
    reading = json.loads((SAMPLES / "circuits" / "higgs-phaser-parasit.circuit.json").read_text())
    assert reading["bom"] == bom.rows_json(rows)


def test_a_switch_agrees_when_its_action_does():
    """A publisher may put the action in either column, so both are read.

    Aion's row for the Tri-Vibe's MODE says "SPDT cntr off" under VALUE and "Toggle
    switch, SPDT on-off-on" under TYPE. The sheet states the action canonically, so
    without folding the spellings the one switch on a 66-part list would stop
    matching — an own goal, since the two say the same thing.
    """
    assert bom.same_value("switch_spdt", "SPDT on/off/on", "SPDT cntr off",
                          "Toggle switch, SPDT on-off-on")
    assert bom.same_value("switch_spdt", "SPDT on/on", "SPDT on/on", "Switch")
    assert bom.same_value("switch_dpdt", "DPDT on/on/on", "DPDT on-on-on", "Switch")
    # a real disagreement, either half of the value
    assert bom.same_value("switch_dpdt", "DPDT on/on", "SPDT on/on", "Switch") is False
    assert bom.same_value("switch_dpdt", "DPDT on/on", "DPDT on-on-on", "Switch") is False
    # neither column says: nothing to compare, so a warning rather than a verdict
    assert bom.same_value("switch_spdt", "SPDT", "Switch", "Switch") is None
