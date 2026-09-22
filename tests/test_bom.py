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
    assert bom.same_value("resistor", "10K", "10K-22K") is None       # a range: can't tell


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


@pytest.mark.skipif(not (SAMPLES / "tri-vibe_documentation.pdf").exists(), reason="third-party PDF not present")
def test_extract_aion_parts_list():
    rows = bom.extract(SAMPLES / "tri-vibe_documentation.pdf")
    assert len(rows) == 75 and bom.pages(rows) == "3, 4, 5"       # page footers are not rows
    assert {r.ref for r in rows if r.hardware} == {"IC1-S", "IC2-S", "IC3-S", "IC4-S", "IN", "OUT", "DC", "FSW", "ENC"}
    assert next(r for r in rows if r.ref == "IC4").type == "Transconductance amplifier, dual, DIP16"
