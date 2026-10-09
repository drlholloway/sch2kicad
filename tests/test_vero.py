"""Stripboard layouts: the nets a board's strips, cuts and links make, the image
read again for the cuts and links, and a schematic checked against the board.

Each check is also handed a planted mistake, because a check that never fails
proves nothing.
"""

import copy
import json
from pathlib import Path

import pytest

from sch2kicad.circuit import from_dict, load
from sch2kicad.vero import (check_circuit, compare_seen, derive, from_dict as board_from_dict,
                            load_board, look, skeleton)

HERE = Path(__file__).resolve().parent
CIRCUITS = HERE.parent / "samples" / "circuits"
MOKA = CIRCUITS / "moka-pot-fuzz.board.json"


def _board(**kw):
    d = {"schema": "sch2kicad/vero@1", "name": "t", "board": {"rows": 4, "cols": 6}}
    d.update(kw)
    return board_from_dict(d)


def _net_of(nets, pin):
    return next(n for n, pins in nets.names.items() if pin in pins)


# ------------------------------------------------------------------ strips

def test_a_strip_joins_its_holes_until_a_cut():
    b = _board(cuts=["A3"], parts=[
        {"ref": "R1", "type": "resistor", "pins": {"1": "A1", "2": "B1"}},
        {"ref": "R2", "type": "resistor", "pins": {"1": "A2", "2": "B2"}},
        {"ref": "R3", "type": "resistor", "pins": {"1": "A4", "2": "B4"}}])
    nets = derive(b)
    assert _net_of(nets, "R1.1") == _net_of(nets, "R2.1")      # same strip, no cut between
    assert _net_of(nets, "R2.1") != _net_of(nets, "R3.1")      # the cut at A3 splits it
    assert _net_of(nets, "R1.2") == _net_of(nets, "R3.2")      # row B is whole


def test_a_cut_between_holes_splits_without_losing_a_hole():
    b = _board(cuts=["A2/A3"], parts=[
        {"ref": "R1", "type": "resistor", "pins": {"1": "A2", "2": "B1"}},
        {"ref": "R2", "type": "resistor", "pins": {"1": "A3", "2": "C1"}}])
    nets = derive(b)
    assert _net_of(nets, "R1.1") != _net_of(nets, "R2.1")
    assert not nets.problems


def test_a_link_joins_two_strips():
    b = _board(links=[["A5", "C5"]], parts=[
        {"ref": "R1", "type": "resistor", "pins": {"1": "A1", "2": "D1"}},
        {"ref": "R2", "type": "resistor", "pins": {"1": "C6", "2": "D6"}}])
    nets = derive(b)
    assert _net_of(nets, "R1.1") == _net_of(nets, "R2.1")


def test_strips_can_run_down_the_columns():
    b = _board(board={"rows": 4, "cols": 6, "strips": "cols"}, parts=[
        {"ref": "R1", "type": "resistor", "pins": {"1": "A1", "2": "A2"}},
        {"ref": "R2", "type": "resistor", "pins": {"1": "D1", "2": "D3"}}])
    nets = derive(b)
    assert _net_of(nets, "R1.1") == _net_of(nets, "R2.1")      # column 1 is one strip
    assert _net_of(nets, "R1.2") != _net_of(nets, "R2.2")


def test_wires_name_their_nets():
    b = _board(wires=[{"name": "Input", "at": "A1"}, {"name": "Ground", "at": "B6"},
                      {"name": "9v", "at": "C1"}, {"name": "Tone 2", "at": "D1"}],
               parts=[{"ref": "R1", "type": "resistor", "pins": {"1": "A3", "2": "B3"}},
                      {"ref": "C1", "type": "capacitor", "pins": {"1": "C3", "2": "D3"}}])
    nets = derive(b)
    assert {"IN", "GND", "+9V", "TONE_2"} <= set(nets.names)


# ------------------------------------------------------------------ what the reading gets wrong

def test_a_lead_in_a_cut_hole_is_a_problem():
    b = _board(cuts=["A3"], parts=[{"ref": "R1", "type": "resistor", "pins": {"1": "A3", "2": "B3"}}])
    assert any("R1.1 is in A3, which is cut" in p for p in derive(b).problems)


def test_counts_the_layout_prints_are_checked():
    b = _board(stated={"cuts": 2, "links": 1}, cuts=["A3"])
    probs = derive(b).problems
    assert any("says 2 cuts" in p for p in probs)
    assert any("says 1 links" in p for p in probs)


def test_two_jacks_on_one_strip_are_a_short():
    b = _board(wires=[{"name": "Input", "at": "A1"}, {"name": "Ground", "at": "A6"}])
    assert any("IN and GND" in p or "GND and IN" in p for p in derive(b).problems)


def test_a_lone_lead_is_flagged():
    b = _board(parts=[{"ref": "R1", "type": "resistor", "pins": {"1": "A1", "2": "B1"}},
                      {"ref": "R2", "type": "resistor", "pins": {"1": "B2", "2": "C2"}}])
    warns = derive(b).warnings
    assert any("R1.1 is alone" in w for w in warns)
    assert not any("R1.2" in w for w in warns)                 # B1 shares its strip with R2


def test_a_hole_off_the_board_is_a_problem():
    b = _board(parts=[{"ref": "R1", "type": "resistor", "pins": {"1": "A7", "2": "E1"}}])
    probs = derive(b).problems
    assert any("A7 is off the 6x4 board" in p for p in probs)
    assert any("E1 is off the 6x4 board" in p for p in probs)


# ------------------------------------------------------------------ the schematic against the board

def _moka():
    return load(CIRCUITS / "moka-pot-fuzz.circuit.json")


def test_the_moka_pot_schematic_matches_its_board():
    c = _moka()
    chk = check_circuit(c, c.board_path())
    assert chk.ok, chk.problems + chk.diffs
    assert chk.pins == 93


def _part(c, ref):
    return next(p for p in c.parts if p.ref == ref)


@pytest.mark.parametrize("plant", [
    "transistor leads swapped", "resistor on the wrong rail", "pot lugs swapped",
    "electrolytic reversed", "value changed", "part missing"])
def test_a_schematic_that_differs_from_its_board_is_caught(plant):
    c = copy.deepcopy(_moka())
    if plant == "transistor leads swapped":
        q = _part(c, "Q2")
        q.pins["B"].net, q.pins["C"].net = q.pins["C"].net, q.pins["B"].net
    elif plant == "resistor on the wrong rail":
        _part(c, "R4").pins["2"].net = "VCC"
    elif plant == "pot lugs swapped":
        p = _part(c, "RV5")
        p.pins["1"].net, p.pins["3"].net = p.pins["3"].net, p.pins["1"].net
    elif plant == "electrolytic reversed":
        p = _part(c, "C6")
        p.pins["+"].net, p.pins["-"].net = p.pins["-"].net, p.pins["+"].net
    elif plant == "value changed":
        _part(c, "R10").value = "100K"
    elif plant == "part missing":
        c.parts = [p for p in c.parts if p.ref != "C10"]
    assert not check_circuit(c, c.board_path()).ok


def test_a_resistor_may_be_drawn_either_way_round():
    c = copy.deepcopy(_moka())
    r = _part(c, "R4")
    r.pins["1"].net, r.pins["2"].net = r.pins["2"].net, r.pins["1"].net
    assert check_circuit(c, c.board_path()).ok


def test_the_jacks_must_keep_their_names():
    c = copy.deepcopy(_moka())
    for p in c.parts:
        for pin in p.pins.values():
            if pin.net == "IN":
                pin.net = "INPUT_JACK"
    for port in c.ports:
        if port.net == "IN":
            port.net, port.name = "INPUT_JACK", "INPUT_JACK"
    assert not check_circuit(c, c.board_path()).ok


def test_the_skeleton_connects_what_the_board_does(tmp_path):
    b = load_board(MOKA)
    sk = skeleton(b, derive(b))
    (tmp_path / MOKA.name).write_text(MOKA.read_text())
    c = from_dict(sk)
    chk = check_circuit(c, tmp_path / MOKA.name)
    assert chk.ok, chk.diffs
    assert {p["ref"] for p in sk["parts"]} == {p.ref for p in b.parts}


def test_an_op_amp_is_split_into_units_and_checked_by_package_pin(tmp_path):
    data = {"schema": "sch2kicad/vero@1", "name": "buffer", "board": {"rows": 8, "cols": 8},
            "cuts": ["A5", "B5", "C5", "D5"],
            "wires": [{"name": "Input", "at": "C1"}, {"name": "Output", "at": "A1"},
                      {"name": "Ground", "at": "D1"}, {"name": "9v", "at": "A8"}],
            "parts": [{"ref": "IC1", "type": "opamp_dual", "part": "TL072",
                       "pins": {"1": "A4", "2": "B4", "3": "C4", "4": "D4",
                                "5": "D6", "6": "C6", "7": "B6", "8": "A6"}},
                      {"ref": "R1", "type": "resistor", "value": "1K", "pins": {"1": "A2", "2": "B2"}}]}
    path = tmp_path / "b.board.json"
    path.write_text(json.dumps(data))
    b = load_board(path)
    sk = skeleton(b, derive(b))
    a = next(p for p in sk["parts"] if p["ref"] == "IC1" and p.get("unit") == "A")
    assert a["supply"] == {"V+": "+9V", "V-": "GND"}
    assert a["pins"]["+"] == "IN"
    c = from_dict(sk)
    assert check_circuit(c, path).ok
    # the inputs of unit A swapped in the drawing: not what the board connects
    for p in c.parts:
        if p.ref == "IC1" and p.unit == "A":
            p.pins["+"].net, p.pins["-"].net = p.pins["-"].net, p.pins["+"].net
    assert not check_circuit(c, path).ok


# ------------------------------------------------------------------ the image read again

needs_image = pytest.mark.skipif(not (CIRCUITS.parent / "ALEPH NULL MOKA POT FUZZ.png").exists(),
                                 reason="the layout image is third-party and not in the repo")


@needs_image
def test_the_image_finds_the_grid_cuts_and_links():
    b = load_board(MOKA)
    seen = look(b)
    g = seen.grid
    assert g and abs(g.x0 - 241) < 2 and abs(g.y0 - 240) < 2 and abs(g.dx - 30) < 0.5
    # M4 is under D1's translucent body, and is the 19th cut the layout counts
    assert {h.name for h in seen.cuts} >= {"M4", "B8", "C8", "D8", "J8", "E14", "O15"}
    # the only cuts not seen are the two drawn under a part's text or body
    assert compare_seen(b, seen) == [
        "cut at A3 is in the reading, but no cut mark shows there (hidden under a part, or a misread hole)",
        "cut at K3 is in the reading, but no cut mark shows there (hidden under a part, or a misread hole)"]


@needs_image
@pytest.mark.parametrize("plant, says", [
    ("drop a cut", "shows a cut at M4"),
    ("drop a link", "shows a link I4-K4"),
    ("misread a link end", "carries on past"),
    ("cut the wrong hole", "cut at M5 is in the reading"),
])
def test_the_image_catches_a_misread_cut_or_link(plant, says):
    data = json.loads(MOKA.read_text())
    if plant == "drop a cut":
        data["cuts"].remove("M4")
    elif plant == "drop a link":
        data["links"].remove(["I4", "K4"])
    elif plant == "misread a link end":
        data["links"][data["links"].index(["I11", "O11"])] = ["I11", "M11"]
    elif plant == "cut the wrong hole":
        data["cuts"][data["cuts"].index("M4")] = "M5"
    b = board_from_dict(data)
    b.path = MOKA
    warns = compare_seen(b, look(b))
    assert any(says in w for w in warns), warns
