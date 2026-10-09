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


def test_a_lug_wired_to_ground_is_on_the_boards_ground():
    """'Level 1 -> Ground': the lug goes to the ground wire's net, not a second ground."""
    b = _board(wires=[{"name": "Ground", "at": "B6"}, {"name": "Level 3", "at": "A1"}],
               parts=[{"ref": "R1", "type": "resistor", "pins": {"1": "A3", "2": "B3"}},
                      {"ref": "LEVEL", "type": "pot", "value": "A10K",
                       "pins": {"1": {"net": "GND"}, "2": {"net": "OUT"}, "3": "Level 3"}}])
    nets = derive(b)
    assert _net_of(nets, "LEVEL.1") == _net_of(nets, "R1.2") == "GND"


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
    # A3 shows as a small red square under the 47nF's lettering; the only cut not
    # seen is K3, under the 4.7K's translucent body
    assert compare_seen(b, seen) == [
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


# ------------------------------------------------------------------ a board with a chip on it

SUB = CIRCUITS / "sub-octave-synth.board.json"


def test_the_sub_octave_schematic_matches_its_board():
    c = load(CIRCUITS / "sub-octave-synth.circuit.json")
    chk = check_circuit(c, c.board_path())
    assert chk.ok, chk.problems + chk.diffs
    assert chk.pins == 83


def test_a_chips_spare_pins_are_one_note_and_nc_in_the_skeleton():
    b = load_board(SUB)
    nets = derive(b)
    assert not any("IC1" in w for w in nets.warnings)
    assert nets.notes == ["IC1 pins 1, 2, 3, 10, 11, 13, 14, 15 are alone on their strips: unused "
                          "(check the datasheet lets them float, and the schematic marks them NC)"]
    ic = next(p for p in skeleton(b, nets)["parts"] if p["ref"] == "IC1")
    assert [k for k, n in ic["pins"].items() if n == "NC"] == ["1", "2", "3", "10", "11", "13", "14", "15"]


def test_a_spare_chip_pin_is_a_note_but_a_lone_resistor_lead_still_warns():
    b = _board(board={"rows": 4, "cols": 8}, cuts=["A4", "B4", "C4", "D4"], parts=[
        {"ref": "IC1", "type": "opamp_dual", "pins": {"1": "A3", "2": "B3", "3": "C3", "4": "D3",
                                                     "5": "D5", "6": "C5", "7": "B5", "8": "A5"}},
        {"ref": "R1", "type": "resistor", "pins": {"1": "A1", "2": "C1"}},
        {"ref": "R2", "type": "resistor", "pins": {"1": "A8", "2": "D8"}}])
    nets = derive(b)
    # B3 (pin 2) has nothing else on its strip: a spare pin, said once
    assert any(n.startswith("IC1 pins 2, 4, 6, 7 are") for n in nets.notes), nets.notes
    assert not any("IC1" in w for w in nets.warnings)
    assert not any("R" in w for w in nets.warnings)
    b = _board(board={"rows": 4, "cols": 8}, parts=[
        {"ref": "R1", "type": "resistor", "pins": {"1": "A1", "2": "C1"}},
        {"ref": "R2", "type": "resistor", "pins": {"1": "A8", "2": "D8"}}])
    assert [w.split(" is alone")[0] for w in derive(b).warnings] == ["R1.2", "R2.2"]


needs_sub_image = pytest.mark.skipif(not (CIRCUITS.parent / "Seppuku Sub Octave Synth.png").exists(),
                                     reason="the layout image is third-party and not in the repo")


@needs_sub_image
def test_cuts_under_a_chip_are_seen_and_a_capacitor_is_not_a_cut():
    b = load_board(SUB)
    seen = look(b)
    # the seven cuts down the middle of the CD4046, dark red under its body
    assert {f"{r}9" for r in "GHIJKLM"} <= {h.name for h in seen.cuts}
    # O5 is under the 470nF's red body and its white lettering, not a cut
    assert "O5" not in {h.name for h in seen.cuts}
    assert compare_seen(b, seen) == [
        "cut at B6 is in the reading, but no cut mark shows there (hidden under a part, or a misread hole)",
        "cut at H6 is in the reading, but no cut mark shows there (hidden under a part, or a misread hole)"]


# ------------------------------------------------------------------ lettered columns, red-dot cuts

LAPSE = CIRCUITS / "memory-lapse.board.json"


def _lettered(**kw):
    d = {"schema": "sch2kicad/vero@1", "name": "t",
         "board": {"rows": 14, "cols": 6, "strips": "rows", "columns": "letters"}}
    d.update(kw)
    return board_from_dict(d)


def test_lettered_columns_read_row_then_column():
    b = _lettered(cuts=["AC"], parts=[
        {"ref": "R1", "type": "resistor", "pins": {"1": "AB", "2": "BB"}},
        {"ref": "R2", "type": "resistor", "pins": {"1": "AD", "2": "BD"}}])
    nets = derive(b)
    assert _net_of(nets, "R1.1") != _net_of(nets, "R2.1")      # cut at row A, column C
    assert _net_of(nets, "R1.2") == _net_of(nets, "R2.2")
    assert any("AB" in nets.holes[n] for n in nets.holes)
    assert any("off the 6x14 board" in p for p in _lettered(parts=[
        {"ref": "R1", "type": "resistor", "pins": {"1": "AG", "2": "BA"}}]).problems)


def test_nc_is_a_hole_when_the_columns_are_lettered():
    # row N, column C — not "no connect"
    b = _lettered(parts=[{"ref": "R1", "type": "resistor", "pins": {"1": "NC", "2": "AA"}},
                         {"ref": "R2", "type": "resistor", "pins": {"1": "NA", "2": "BA"}}])
    nets = derive(b)
    assert _net_of(nets, "R1.1") == _net_of(nets, "R2.1")


def test_a_chip_pin_in_a_cut_hole_is_left_open_but_a_resistor_lead_is_a_problem():
    b = _lettered(cuts=["AB", "BB"], parts=[
        {"ref": "IC1", "type": "lm386", "pins": {"1": "AB", "2": "BC", "3": "CB", "4": "DB",
                                                 "5": "DE", "6": "CE", "7": "BE", "8": "AE"}},
        {"ref": "R1", "type": "resistor", "pins": {"1": "BB", "2": "CA"}}])
    nets = derive(b)
    assert "IC1 pin 1 sits in a cut hole: left open, as the layout means" in nets.notes
    assert any("R1.1 is in BB, which is cut" in p for p in nets.problems)
    assert not any("IC1" in p for p in nets.problems)


def test_the_memory_lapse_schematic_matches_its_board():
    c = load(CIRCUITS / "memory-lapse.circuit.json")
    chk = check_circuit(c, c.board_path())
    assert chk.ok, chk.problems + chk.diffs
    assert chk.pins == 109


needs_lapse_image = pytest.mark.skipif(not (CIRCUITS.parent / "Seppuku Fx Memory Lapse.png").exists(),
                                       reason="the layout image is third-party and not in the repo")


@needs_lapse_image
def test_red_dot_cuts_and_blue_links_are_seen():
    b = load_board(LAPSE)
    seen = look(b)
    names = {h.name for h in seen.cuts}
    # plain red dots, a dot under a lead (KN), dots under the PT2399's grey body (column G)
    assert {"AK", "KN", "MO", "EF"} | {f"{r}G" for r in "JKLMOPQ"} <= names
    read = {h for h, o in b.cuts if o is None}
    assert not (seen.cuts - read), sorted(h.name for h in seen.cuts - read)     # nothing invented
    assert {(x.name, y.name) for x, y in seen.links} >= {("ED", "FD"), ("CJ", "DJ"), ("EG", "HG"), ("LD", "MD")}


def test_cut_colours_of_the_2019_layouts():
    """The 2019 Dirtbox layouts (Octave Drone, Fuzz Overloader, Space Fuzz) draw cuts
    pink, and the pink dims under a translucent chip, diode or resistor. Their magenta
    film caps, the copper, a translucent red body over copper and a pink resistor are
    not cuts."""
    from sch2kicad.vero import _red
    for cut in [(255, 0, 102), (188, 68, 116), (173, 28, 86), (213, 70, 153), (206, 81, 99),
                (204, 0, 0), (190, 66, 66)]:
        assert _red(cut), cut
    for other in [(204, 0, 102), (255, 153, 51), (230, 75, 60), (255, 107, 71), (243, 102, 123)]:
        assert not _red(other), other


def test_the_sabrotone_style_finds_its_grid_cuts_and_links(tmp_path):
    """DIY Layout Creator boards (SabroTone): a cut is a red dot in a white square in a
    red outline, a link a blue bar two pixels wide. The strips' ends put the columns a
    few pixels out and a dithered label beside a strip has a copper-coloured pixel, so
    the grid comes from the holes."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (300, 120), "white")
    d = ImageDraw.Draw(im)
    X, Y = (lambda c: 30 + 20 * (c - 1)), (lambda r: 30 + 20 * r)
    for r in range(4):
        d.rectangle((22, Y(r) - 7, 219, Y(r) + 6), fill=(255, 170, 51))
        for c in range(1, 11):
            d.rectangle((X(c) - 1, Y(r) - 1, X(c) + 1, Y(r) + 1), fill="white")
    im.putpixel((290, Y(1)), (255, 170, 51))                  # the label's stray pixel
    x, y = X(4), Y(1)                                          # the cut at B4...
    d.rectangle((x - 7, y - 7, x + 7, y + 7), fill=(255, 0, 0))
    d.rectangle((x - 5, y - 5, x + 5, y + 5), fill="white")
    d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(255, 0, 0))
    d.rectangle((x - 1, Y(0), x + 1, Y(2)), fill=(102, 85, 102))   # ...with a lead across it
    d.rectangle((X(7) - 1, Y(0), X(7), Y(3)), fill=(0, 0, 255))     # the link A7-D7
    d.rectangle((X(2) - 7, Y(2) - 7, X(2) + 7, Y(2) + 7), fill=(255, 0, 0))   # a red body, not a cut
    im.save(tmp_path / "t.png")
    b = _board(board={"rows": 4, "cols": 10}, source={"image": "t.png"})
    b.path = tmp_path / "b.board.json"
    seen = look(b)
    g = seen.grid
    assert g and abs(g.x0 - 30) < 1 and abs(g.dx - 20) < 0.6, g
    assert {h.name for h in seen.cuts} == {"B4"}
    assert [(x.name, y.name) for x, y in seen.links] == [("A7", "D7")]
