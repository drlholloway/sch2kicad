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


def _ready(tmp_path, source: str, circuit: str, page: int | None = None, turn: int | None = None):
    """Ingest a source and put the reading of it in the work directory."""
    from sch2kicad.ingest import ingest
    src = SAMPLES / source
    if not src.exists():
        pytest.skip(f"{source} is third-party and not in the repo")
    work = tmp_path / "work"
    ingest(src, work, page=page, turn=turn)
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
    # the status LED and its resistor are on the board by choice, not in Aion's drawing,
    # and the check says so off the page rather than counting them as pins it could not read
    assert [u.split(":")[0] for u in rep.undrawn] == ["LED", "LEDR"], rep.text()
    assert rep.unmatched == [], rep.text()
    assert rep.checked == rep.drawn, rep.text()


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
    import json
    c = _ready(tmp_path, "Higgs-Particle-Phaser-doc.pdf", "higgs-phaser-parasit.circuit.json", page=8)
    # Parasit put the schematic page on its side, and the other half of the rule that
    # keeps the Musket upright is that this one still turns
    assert json.loads((tmp_path / "work" / "ingest.json").read_text())["rotated"] == 90
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


def test_a_closed_box_round_a_parts_pins_is_not_four_wires(tmp_path):
    """The Disorder draws its DPDT as two thick boxes with the terminals *inside* the
    outline, so the four sides chain one to the next round the corners: both poles and
    three separate off-page nets came back as one node — which is the signature a real
    short would give. What the drawing actually joins is the one wire between the
    poles, and that is what has to survive the outline going."""
    c = _ready(tmp_path, "22358.png", "disorder-22358.circuit.json")
    rep = crosscheck_circuit(c)
    assert rep.pins["SW1A.a"][1] == rep.pins["SW1B.com"][1], rep.text()
    # the other four terminals each leave the sheet on their own: three by a letter
    # the tool knows nothing of, the common by the wire to C12
    apart = [rep.pins[p][1] for p in ("SW1A.b", "SW1A.com", "SW1B.a", "SW1B.b")]
    assert None not in apart and len(set(apart)) == 4, rep.text()
    assert rep.merges == [], rep.text()


def test_a_crossing_broken_either_side_is_not_a_junction(tmp_path):
    """Escobedo marks a crossing by breaking the wire that is jumped — one pixel of
    paper each side of the one it steps over. That gap is narrower than the tolerance
    a raster needs for its T-junctions, so both stubs end within reach of the wire
    between them and all three get welded: the Gargler's whole signal bus came out on
    the first filter's own node, eight pins across two nets."""
    c = _ready(tmp_path, "gargler.gif", "gargler-escobedo.circuit.json")
    rep = crosscheck_circuit(c)
    assert rep.merges == [], rep.text()
    assert rep.disagreements == 0, rep.text()
    # the bus and the filter node it crosses are two things, and each of them is one
    bus = {rep.pins[p][1] for p in ("C2.2", "C3.1", "C9.1", "VR1.3")}
    node = {rep.pins[p][1] for p in ("R4.2", "C4.1", "C5.1", "R9.1")}
    assert len(bus) == 1 and len(node) == 1 and bus != node, rep.text()


def _wires(*segs):
    """Conductors from bare geometry, on a page whose smallest unit is 2 px."""
    from sch2kicad.trace import Conductors, Seg
    return Conductors([Seg(*s) for s in segs], [], t_contacts=True, page=2.0)


def test_a_wire_broken_either_side_of_another_hops_over_it():
    """The two stubs are one wire and the wire between them is not on it. A junction
    dot says otherwise, though, and it still wins: a drawing that means both says so
    with the dot."""
    from sch2kicad.trace import Conductors, Dot, Seg
    cond = _wires(((100, 200), (145, 200)), ((149, 200), (200, 200)), ((147, 100), (147, 300)))
    assert cond.find(0) == cond.find(1), "the wire does not carry on across the crossing"
    assert cond.find(0) != cond.find(2), "the crossing was read as a junction"
    dotted = Conductors([Seg((100, 200), (145, 200)), Seg((149, 200), (200, 200)),
                         Seg((147, 100), (147, 300))], [Dot(147, 200, 2)],
                        t_contacts=True, page=2.0)
    assert dotted.find(0) == dotted.find(2), "a dot on the crossing did not join it"


def test_a_capacitors_plate_is_not_a_wire_being_hopped():
    """A plate stands in the gap between its own two leads exactly as a jumped wire
    stands between two stubs — through the middle, and past both sides — and it is
    ink the body rule keeps on purpose, since a run through one pin is a lead. Take
    it for a crossing and the hop rule welds the capacitor end to end, which is a
    dead board. What tells them apart is length: a plate is as long as a capacitor,
    and only a wire runs a twentieth of the page in a straight line."""
    cond = _wires(((100, 200), (147, 200)), ((153, 200), (200, 200)), ((150, 193), (150, 207)))
    assert cond.find(0) != cond.find(1), "the capacitor was shorted across its own plate"


def test_a_wire_that_stops_between_two_stubs_is_a_junction():
    """A hop passes through. One that ends in the gap is a wire meeting a wire, and
    breaking that join would cut a real node in three."""
    cond = _wires(((100, 200), (145, 200)), ((149, 200), (200, 200)), ((147, 200), (147, 300)))
    assert cond.find(0) == cond.find(1) == cond.find(2), "a T-junction was read as a hop"


def test_a_rectangle_of_wires_is_not_an_outline():
    """Two rails with a part standing between them make a rectangle as soon as
    anything else stands between them further along, and every line of it is a wire.
    What tells that from a body outline is where the sides stop: a rail runs on past
    the corner, an outline ends there. Drop it on the strength of the corners alone
    and the cost is two whole rails."""
    from sch2kicad.circuit import from_dict
    from sch2kicad.trace import Seg, drop_boxes

    c = from_dict({"name": "a part between two rails", "source": {"file": "synthetic"},
                   "parts": [{"ref": "R1", "type": "resistor", "value": "10K",
                              "pins": {"1": ["VCC", 100, 100], "2": ["GND", 100, 200]}}]})
    rails = [Seg((50, 100), (250, 100)), Seg((50, 200), (250, 200)),   # run on past both
             Seg((100, 100), (100, 200)), Seg((160, 100), (160, 200))]
    assert drop_boxes(list(rails), c, touch=2.0) == rails
    # the same four lines, each stopping where the next begins, are a box and go
    boxed = [Seg((100, 100), (160, 100)), Seg((100, 200), (160, 200)),
             Seg((100, 100), (100, 200)), Seg((160, 100), (160, 200))]
    assert drop_boxes(list(boxed), c, touch=2.0) == []


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


def test_the_octave_fuzz_agrees_with_its_drawing(tmp_path):
    """22359 is the sheet the wire-colour change was written for, and the first
    thing it showed was that this reading's own pin positions were a median 18 px
    off the ink. They are read off the green now — the sheet draws symbols in red
    and wires in green, so a pin sits where the green stops — and every pin lands
    on the conductor the reading says it should."""
    c = _ready(tmp_path, "22359.png", "octave-fuzz-22359.circuit.json")
    rep = crosscheck_circuit(c)
    assert rep.unmatched == [], rep.text()
    assert rep.checked == 78, rep.text()
    assert rep.disagreements == 0, rep.text()


def test_a_junction_dot_does_not_swallow_the_wire_through_it(tmp_path):
    """A dot is wider than the wire it sits on, so its own short rows lie squarely
    under the wire's long ones. Stacking runs that merely sit inside each other let
    the dot carry the wire's group down row after row until it was too thick to be
    a line — and 22359's 135 px feed into IC1D pin 12 was thrown away entire."""
    from sch2kicad.trace import raster_segments
    _rgb, page = _source(tmp_path, "22359.png")
    segs, _ = raster_segments(page)
    feed = [s for s in segs if abs(s.a[1] - 211) <= 2 and s.length() > 100]
    assert feed, "the wire under the junction dots was not traced"


def test_a_line_that_falls_between_two_pixels_is_still_ink(tmp_path):
    """Thin lines are drawn with anti-aliasing, so one that lands between two pixel
    centres puts half its ink in each. On 22359 that is the difference between a
    wire reading 165 deep and the same wire, a third of a pixel over, reading 64
    and 76 — under any single bar, though together they carry a whole line."""
    from sch2kicad.trace import raster_segments
    _rgb, page = _source(tmp_path, "22359.png")
    segs, _ = raster_segments(page)
    # R10's foot to IC1D pin 13: a 94 px drop at x=763 that no pixel of makes the bar
    drop = [s for s in segs if abs(s.a[0] - 763.5) <= 2 and s.length() > 80]
    assert drop, "the wire split between two columns was not traced"


def test_a_part_drawn_in_two_places_is_two_bodies(tmp_path):
    """22359 draws the 4040's supply pins over in its power block, a third of a page
    from the chip. Taken as one body those pins span half the drawing and everything
    inside the rectangle reads as the chip's own ink: it cost 30% of the sheet's wire
    and two thirds of its pins."""
    from sch2kicad.trace import drop_bodies, page_unit, raster_segments
    c = _ready(tmp_path, "22359.png", "octave-fuzz-22359.circuit.json")
    page = tmp_path / "work" / "source.png"
    segs, _ = raster_segments(page)
    kept = drop_bodies(list(segs), c, touch=page_unit(page))
    before = sum(s.length() for s in segs)
    assert sum(s.length() for s in kept) >= 0.9 * before, "a part's own span ate the page"


def test_a_short_wire_is_better_than_no_wire_at_all(tmp_path):
    """A sheet that draws its symbols in their own colour leaves nothing of them in
    the wire ink, so what is left between two symbols is the hop alone: 22359's pot
    reaches its resistor in 16 px and its ground flag in 8. Refusing a conductor
    that short loses the pin, and sends the flag's name to whatever passes nearby."""
    c = _ready(tmp_path, "22359.png", "octave-fuzz-22359.circuit.json")
    rep = crosscheck_circuit(c)
    for pin in ("OCTAVE+1.2", "R12.1", "OCTAVE-1.1", "IC1C.8"):
        assert rep.pins[pin][1] is not None, f"{pin} lost its wire\n{rep.text()}"


def test_a_junction_dot_drawn_in_colour_is_found(tmp_path):
    """Ingest looked for dots by brightness, and a saturated green works out at 163
    of 255 — bare paper. 22359 came through with no dots at all, so every crossing
    the sheet marks as a join was read as a crossing."""
    from PIL import Image
    from sch2kicad.ingest import find_dots
    src = SAMPLES / "22359.png"
    if not src.exists():
        pytest.skip("22359.png is third-party and not in the repo")
    with Image.open(src) as im:
        dots = find_dots(im)
    assert dots, "a sheet drawn in colour came through with no junction dots"
    # the one that joins R4's foot to the wire down to C2: without it SQ is two nets
    assert any(abs(d["x"] - 648) <= 3 and abs(d["y"] - 529) <= 3 for d in dots), dots


def test_a_wire_the_pin_stands_on_is_not_behind_it(tmp_path):
    """The outward hint passes over a conductor lying behind a pin, because a
    capacitor's far plate is as near to the pin as its own and belongs to the other
    net. But a wire running *through* a pin has its nearest point at the pin itself,
    so the dot product there is exactly zero — and reading zero as behind charged the
    Gargler's R11.1 the full penalty for standing on its own wire. It took the wire
    of the net 4 px away instead, welding the switch's B pole to the output.

    The other end of that wire used to be a pad, SWB, and is the real switch's common
    now. That makes the check a stronger one than it was: the cross-check only gives
    the outward hint to a part's pins, never to a port, so both ends of this net now
    go through the rule that had the bug."""
    c = _ready(tmp_path, "gargler.gif", "gargler-escobedo.circuit.json")
    rep = crosscheck_circuit(c)
    assert rep.pins["R11.1"][1] == rep.pins["SW1B.com"][1], rep.text()
    assert [m for m in rep.merges if "SW_B" in m] == [], rep.text()


def test_a_run_that_stops_just_past_a_wire_has_met_it(tmp_path):
    """The Theremin draws both of its four-way junctions with a dot, and both dots sit
    inside a part's reach — one a stroke under R3, the other a stroke under the 4013 —
    so the body rule eats the dot's own ink as an outline and leaves a bare crossing.
    What is left says the rest: the wire coming up stops 3.3 px past the one it meets,
    and 3.5 px past the other, because the raster grows its run to the far rim of the
    dot. A tracer that joins within 2 px read both as crossings, and split the
    trimmer's node and the VCO's output each into two."""
    c = _ready(tmp_path, "Theremin-Fuzz-doc.pdf", "theremin-fuzz-parasit.circuit.json", page=8)
    rep = crosscheck_circuit(c, doc=SAMPLES / "Theremin-Fuzz-doc.pdf")
    trimmer = {rep.pins[p][1] for p in ("R3.2", "TRIM1.2", "TRIM1.3", "U1B.4")}
    vco = {rep.pins[p][1] for p in ("R12.2", "U2A.3", "SW1.1", "U3.4")}
    assert None not in trimmer and len(trimmer) == 1, rep.text()
    assert None not in vco and len(vco) == 1, rep.text()
    assert rep.merges == [], rep.text()
    assert rep.disagreements == 0, rep.text()


def _page(page, *segs):
    """Conductors from bare geometry, on a page of a given smallest unit."""
    from sch2kicad.trace import Conductors, Seg
    return Conductors([Seg(*s) for s in segs], [], t_contacts=True, page=page)


def test_a_run_that_stops_short_of_a_wire_has_not_met_it():
    """The whole rule is that the ink crossed the line it stands on. A run that stops
    the same distance short has not touched the wire, however near it comes."""
    past = _page(6.0, ((100, 200), (300, 200)), ((200, 197), (200, 300)))
    assert past.find(0) == past.find(1), "a run stopping 3 px past the wire did not meet it"
    short = _page(6.0, ((100, 200), (300, 200)), ((200, 203), (200, 300)))
    assert short.find(0) != short.find(1), "a run stopping 3 px short of the wire was joined"
    flat = _page(0.0, ((100, 200), (300, 200)), ((200, 197), (200, 300)))
    assert flat.find(0) != flat.find(1), "a vector page took a crossing for a contact"


def test_a_capacitor_is_not_shorted_across_the_gap_its_body_left():
    """Cutting a part's body out leaves its two leads facing each other on one line,
    and on the RedMuffin that gap is 5 px on a page whose runs may stop 7 past a wire
    they meet. Two runs on one line never cross each other's line, which is what keeps
    this rule out of that gap — and out of the one a hop is drawn with."""
    cap = _page(10.0, ((100, 300), (177, 300)), ((182, 300), (381, 300)))
    assert cap.find(0) != cap.find(1), "the capacitor was shorted across its own body"


def test_a_hop_is_still_a_hop_where_a_run_may_stop_past_a_wire():
    """The two rules read the same shape from opposite sides, so they have to agree:
    a break either side of a crossing is a hop even when the break is narrower than
    the distance a run may stop past a wire it has met."""
    from sch2kicad.trace import Conductors, Dot, Seg
    hop = _page(6.0, ((0, 200), (144, 200)), ((150, 200), (300, 200)), ((147, 50), (147, 350)))
    assert hop.find(0) == hop.find(1), "the wire does not carry on across the crossing"
    assert hop.find(0) != hop.find(2), "the crossing was read as a junction"
    dotted = Conductors([Seg((0, 200), (144, 200)), Seg((150, 200), (300, 200)),
                         Seg((147, 50), (147, 350))], [Dot(147, 200, 2)],
                        t_contacts=True, page=6.0)
    assert dotted.find(0) == dotted.find(2), "a dot on the crossing did not join it"


def _move_part(c, ref: str, dx: float, dy: float):
    """The same reading with one part's pins all shifted, as a misread would have them."""
    bad = copy.deepcopy(c)
    for p in bad.parts:
        if p.ref == ref:
            for pin in p.pins.values():
                if pin.at:
                    pin.at = (pin.at[0] + dx, pin.at[1] + dy)
    return bad


def test_a_part_the_drawing_does_not_draw_is_named_as_such(tmp_path):
    """Parasit's sheet has no status LED on it: the LED and its resistor go on the board
    because the builder wants them, not because the drawing asks. Their pins have no wire
    to find and never will, and counting them as pins the check could not read buries the
    ones that are the reading's own fault. Every other pin of this sheet is now traced."""
    c = _ready(tmp_path, "Theremin-Fuzz-doc.pdf", "theremin-fuzz-parasit.circuit.json", page=8)
    rep = crosscheck_circuit(c, doc=SAMPLES / "Theremin-Fuzz-doc.pdf")
    assert sorted(u.split(":")[0] for u in rep.undrawn) == ["CLR", "LED"], rep.text()
    assert rep.unmatched == [], rep.text()
    assert rep.checked == rep.drawn == len(rep.pins) - 4, rep.text()
    assert rep.disagreements == 0, rep.text()


def test_a_part_read_in_the_wrong_place_is_not_excused_as_undrawn(tmp_path):
    """The whole use of telling the two apart is that a misread part stays on the list.

    R10 is drawn as a vertical resistor at x=1670; shift the reading of it 20 px sideways
    and neither pin has a wire under it any more — the same silence an absent part makes.
    What separates them is the drawing: R10's own ink is still inside the space its pins
    stand in, so it is a reading to fix, not a part that is not there. Excuse it and the
    coverage number would quietly swallow the error it exists to show.
    """
    c = _ready(tmp_path, "Theremin-Fuzz-doc.pdf", "theremin-fuzz-parasit.circuit.json", page=8)
    doc = SAMPLES / "Theremin-Fuzz-doc.pdf"
    rep = crosscheck_circuit(_move_part(c, "R10", 20, 0), doc=doc)
    assert [u.split(":")[0] for u in rep.undrawn] == ["CLR", "LED"], rep.text()
    assert sorted(u.split(" at ")[0] for u in rep.unmatched) == ["R10.1", "R10.2"], rep.text()


def test_the_nearest_ink_says_where_a_misread_pin_belongs(tmp_path):
    """A pin with no wire under it is reported with how far the nearest ink is, because
    that is what says what to do about it: eleven pixels away is a pin to move, and the
    drawing is where the answer comes from. Reading that number off the report is what
    this whole sheet's coordinates were corrected from."""
    c = _ready(tmp_path, "Theremin-Fuzz-doc.pdf", "theremin-fuzz-parasit.circuit.json", page=8)
    rep = crosscheck_circuit(_move_part(c, "R10", 20, 0), doc=SAMPLES / "Theremin-Fuzz-doc.pdf")
    line = next(u for u in rep.unmatched if u.startswith("R10.1"))
    px = float(line.rpartition("nearest ink ")[2].split()[0])
    assert 15 <= px <= 25, line      # the wire it was moved off, and no further


def _flag_pins(c, ink) -> dict[str, str]:
    """Which pins the sheet's ``GND`` flags reach, and what the reading calls each.

    Built from scratch each call: ``name_nets`` joins the conductors that share a
    name, so a second measure asked of the same conductors would be answering a
    question the first one has already changed.
    """
    from sch2kicad.trace import (MIN_NET, Conductors, drop_bodies, drop_boxes, name_nets,
                                 page_unit, pin_points, raster_dots, raster_segments,
                                 read_text)
    work = c.path.parent
    image = work / "source.png"
    dots = raster_dots(work)
    segs, _ = raster_segments(image, dots=dots)
    touch = page_unit(image)
    pin_tol, min_net = 11.0, max(6.0, MIN_NET * touch)
    segs = drop_bodies(drop_boxes(segs, c, touch=touch), c, touch=touch)
    cond = Conductors(segs, dots, t_contacts=True, page=touch)
    on_pins = {r for r in (cond.net_at(at, pin_tol, min_net) for at in pin_points(c))
               if r is not None}
    rails = [n for n in c.all_net_names() if c.net_kind(n) in ("power", "ground")]
    named = name_nets(cond, read_text(work), rails, on_pins, ink)
    roots = {r for r, names in named.items() if "GND" in names}
    return {f"{p.key}.{n}": pin.net for p in c.parts for n, pin in p.pins.items()
            if pin.at and cond.net_at(tuple(pin.at), pin_tol, min_net) in roots}


def test_a_flags_name_reaches_its_wire_across_its_own_symbol(tmp_path):
    """A rail flag's name is written against the flag, not against the wire.

    22359 draws symbols in red and wires in green, so the whole of OCTAVE-1's ground
    flag — the pot's lead, the bar, the stem between them — is red, and the conductor
    the tracer keeps is an 8 px island of green in the middle of it. The sheet writes
    `GND` under the bar, 26 px from the end of that green. Measured by distance the
    name is closer to whatever else passes: 18 px above an octave pot's corner. So
    ground came back welded to the pot's top, and nine more of the sheet's eleven
    flags reached no wire at all and went unchecked.

    Measuring the blank paper between name and wire instead reads the drawing the way
    round it means: 6 px to its own wire across the symbol, against 18 px of bare page
    to the pot. Both measures are asked here, because what makes this a rule and not a
    tuning is that the two give different answers on the same sheet.
    """
    from sch2kicad.trace import page_ink
    c = _ready(tmp_path, "22359.png", "octave-fuzz-22359.circuit.json")
    by_distance = _flag_pins(c, None)
    by_paper = _flag_pins(c, page_ink(c.path.parent / "source.png"))
    assert {k for k, v in by_distance.items() if v != "GND"} == {"OCTAVE-2.3", "R7.2"}
    assert {k for k, v in by_paper.items() if v != "GND"} == set()
    # and the flags the name never reached are the point: 13 ground pins checked, not 3
    assert len(by_paper) == 13 and "OCTAVE-1.1" in by_paper, sorted(by_paper)


def test_a_ground_pin_read_as_the_supply_is_caught_by_its_flags_name(tmp_path):
    """Ten of those thirteen pins sit alone on their own flag stem, so a name is the
    only thing that can check them: there is no second pin on the conductor to disagree
    with. IC1A pin 2 is one — read it as +9V instead of ground, the worst mistake this
    sheet could hold, and with the flags reaching no further than a distance allows the
    check passes it in silence."""
    c = _ready(tmp_path, "22359.png", "octave-fuzz-22359.circuit.json")
    bad = copy.deepcopy(c)
    for p in bad.parts:
        if p.key == "IC1A":
            p.pins["2"].net = "+9V"
    rep = crosscheck_circuit(bad)
    assert any("+9V is where the drawing writes GND" in m for m in rep.misnamed), rep.text()


def _one_part(ref: str, ptype: str, pins: dict):
    """A circuit of one part, for geometry built by hand."""
    from sch2kicad.circuit import from_dict
    return from_dict({"name": "bare geometry", "source": {"file": "synthetic"},
                      "parts": [{"ref": ref, "type": ptype, "value": "x", "pins": pins}]})


def test_a_plate_pair_one_pixel_apart_is_still_one_body():
    """Kelly Roberts draws a capacitor as two 5 px plates a single pixel apart, so the
    run through it comes back in two halves, each through one pin — each "a lead", and
    kept whole — facing each other across a gap the tracer's own join bridges. That
    shorted every series cap on the Fix'd Fuzz. Two halves of one run, between two of
    the part's pins, are the part's body, and are cut at the pins as one run would be."""
    from sch2kicad.trace import Seg, drop_bodies
    c = _one_part("C1", "capacitor", {"1": ["A", 146, 260], "2": ["B", 159, 260]})
    kept = drop_bodies([Seg((120, 260), (151, 260)), Seg((153, 260), (190, 260))], c, touch=5.0)
    cond = _wires(*((s.a, s.b) for s in kept))
    assert len(kept) == 2 and cond.find(0) != cond.find(1), f"shorted across its plates: {kept}"
    # and each pin keeps its own lead: the cut stops at the pin, not short of it
    assert [(s.a[0], s.b[0]) for s in kept] == [(120, 146), (159, 190)], kept


def test_a_lead_with_nothing_facing_it_close_is_kept_whole():
    """The first version of that rule cut *every* lead back to its pin on the side
    facing the part's middle. The Higgs reads its CD4517's pins out along the wires,
    28 px from the box, so it cut real wire — and the end that left lined up with a
    GND written a pixel from another net's wire, which then took the name and welded
    the two. Only a hairline between two leads is body; two leads that stop well
    short of each other are two wires, however far past its pin each one runs."""
    from sch2kicad.trace import Seg, drop_bodies
    c = _one_part("R1", "resistor", {"1": ["A", 100, 50], "2": ["B", 200, 50]})
    leads = [Seg((50, 50), (128, 50)), Seg((180, 50), (250, 50))]     # 52 px apart
    assert drop_bodies(list(leads), c, touch=5.0) == leads
    # nor is a wire meeting a pin square-on, two pixels off it as a hand-read pin will
    # be, beside a half that does face another: it runs across the part, not into it
    c = _one_part("C1", "capacitor", {"1": ["A", 146, 260], "2": ["B", 159, 260]})
    t = Seg((148, 200), (148, 320))
    kept = drop_bodies([t, Seg((120, 260), (151, 260)), Seg((153, 260), (190, 260))], c, touch=5.0)
    assert t in kept, kept


def test_the_fixd_fuzz_agrees_with_its_drawing(tmp_path):
    """Kelly Roberts, 2011: black on white, crossings drawn as arcs, each stage switched
    in and out by a lever pair, and capacitors whose plates stand a pixel apart."""
    c = _ready(tmp_path, "fixd-fuzz.png", "fixd-fuzz-roberts.circuit.json")
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.checked == rep.drawn == 128, rep.text()


def test_the_musket_agrees_with_its_drawing_the_right_way_up(tmp_path):
    """Blackout Effectors' Musket writes nearly every resistor and pot label up the
    side of its part, so its labels read better turned a quarter than upright — 31
    against 9 — and ingest turned a perfectly upright sheet on its side. Counting its
    words as well as its labels, and asking the turned page to out-read the upright
    one well past that, leaves it where it is."""
    import json
    c = _ready(tmp_path, "musket_schem-corrected.png", "musket-blackout.circuit.json")
    assert json.loads((tmp_path / "work" / "ingest.json").read_text())["rotated"] == 0
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.checked == rep.drawn == 136, rep.text()


def test_two_slanting_wires_that_cross_do_not_meet(tmp_path):
    """A raster keeps a wire as a run along a row or a column, and a slanting wire is a
    staircase of short steps chained end to end. Where two cross, the staircases share
    pixels and the chain runs from one to the other. Read as lines, they meet only where
    one of them ends — which is where each meets the stubs it runs between."""
    from PIL import Image, ImageDraw
    from sch2kicad.trace import Conductors, raster_segments
    img = Image.new("RGB", (400, 200), "white")
    d = ImageDraw.Draw(img)
    d.line([(100, 50), (300, 110)], fill="black")        # one way across
    d.line([(100, 110), (300, 50)], fill="black")        # and the other, through it
    for y in (50, 110):
        d.line([(40, y), (100, y)], fill="black")
        d.line([(300, y), (360, y)], fill="black")
    path = tmp_path / "x.png"
    img.save(path)
    segs, _ = raster_segments(path)
    cond = Conductors(segs, [], t_contacts=True, page=2.0)

    def at(p):
        return cond.find(min(range(len(segs)), key=lambda i: _dist(p, segs[i])))
    assert at((60, 50)) == at((340, 110)), "a slanting wire does not carry its stubs"
    assert at((60, 110)) == at((340, 50)), "a slanting wire does not carry its stubs"
    assert at((60, 50)) != at((60, 110)), "two slanting wires joined where they cross"


def _dist(p, s):
    from sch2kicad.trace import _dist_to_seg
    return _dist_to_seg(p, s)


def test_the_angry_droids_crossovers_are_not_junctions(tmp_path):
    """Talisman Audio cross the Angry Droid's multivibrator over in two Xs of slanting
    wire with no dot — each capacitor and each diode to the far transistor. Read as
    staircases, they made one net of both collectors' loads and another of both bases."""
    c = _ready(tmp_path, "angrydroidDOC.png", "angry-droid-talisman.circuit.json")
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.checked == rep.drawn == 94, rep.text()
    assert rep.pins["C6.2"][1] == rep.pins["D2.A"][1] != rep.pins["C7.2"][1] == rep.pins["D1.A"][1], rep.text()


@pytest.mark.parametrize("source, circuit, pins", [
    ("TL431PFV4DOC.png", "tl431-precision-fuzz.circuit.json", 31),
    ("hullfuzzdpdtDOC.png", "hull-fuzz-talisman.circuit.json", 36),
    ("djinnDOC.png", "djinns-flame-talisman.circuit.json", 76),
])
def test_the_talisman_sheets_agree_with_their_drawings(tmp_path, source, circuit, pins):
    """Talisman Audio's doc sheets: KiCad-drawn, black on white, pin numbers printed on
    every transistor, pot and IC, and a DPDT drawn as two boxes with its terminals
    inside — the Disorder's style, so the outline rule reads it without help."""
    c = _ready(tmp_path, source, circuit)
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.checked == rep.drawn == pins, rep.text()


def test_a_sheet_drawn_white_on_black_is_read_as_ink_on_paper(tmp_path):
    """The Christine is drawn in white on black. Every step reads dark pixels as ink,
    so the page itself was one wire and the black insides of the inverters' bubbles
    were its junction dots: ingest inverts a page whose paper is dark."""
    import json
    c = _ready(tmp_path, "Christine-Build-PDF2.pdf", "christine-tonegod.circuit.json", page=2)
    assert json.loads((tmp_path / "work" / "ingest.json").read_text())["inverted"] is True
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.checked == rep.drawn == 74, rep.text()


def test_a_run_into_a_big_dot_stops_a_dots_radius_past_the_wire(tmp_path):
    """The Christine draws 3 px wires with dots only a little fatter, too thin for the
    dot finder, and each T into the oscillator's feedback wire runs on to the dot's far
    rim, 3 px past it — past the 2.8 px (0.75 of a page unit) the tracer allowed,
    so the oscillator's output came apart in four."""
    c = _ready(tmp_path, "Christine-Build-PDF2.pdf", "christine-tonegod.circuit.json", page=2)
    rep = crosscheck_circuit(c)
    osc = {rep.pins[p][1] for p in ("U1F.15", "OSC.cw", "OSC.wiper", "R6.2")}
    assert None not in osc and len(osc) == 1, rep.text()


def test_a_circuit_drawn_sideways_on_an_upright_page_is_traced_turned(tmp_path):
    """PCB Guitar Mania set the Acapulco Tone on its side on an upright page whose
    header reads the right way, and draw its lettering as outlines, so nothing on the
    page says it is turned; the reader says so with ``turn``. The vector tracer then
    has to turn its segments the same way, or nothing lands where source.png has it.
    The page also has a white X from corner to corner, which put BASS and the tone
    stack's middle on one wire, and ends every pad in a drawn X its lead starts from."""
    c = _ready(tmp_path, "Acapulco-Tone-1v2-Building-Docs-1.pdf",
               "acapulco-tone-pcbguitarmania.circuit.json", page=5, turn=270)
    rep = crosscheck_circuit(c, doc=SAMPLES / "Acapulco-Tone-1v2-Building-Docs-1.pdf")
    assert rep.disagreements == 0, rep.text()
    assert rep.checked == rep.drawn == 85, rep.text()
    assert rep.pins["R4.1"][1] != rep.pins["R5.2"][1], rep.text()


def _vector_page(tmp_path, draw):
    import pdfs
    page = pdfs.Canvas(200)
    draw(page)
    return pdfs.write(tmp_path / "page.pdf", 200, 200, page.content())


def test_a_white_stroke_is_not_a_wire(tmp_path):
    from sch2kicad.trace import vector_segments

    def draw(page):
        page.draw_line((20, 50), (180, 50), color=(0, 0, 0))
        page.draw_line((20, 20), (180, 180), color=(1, 1, 1))
    segs, _ = vector_segments(_vector_page(tmp_path, draw), 1, 1.0)
    assert [(s.a, s.b) for s in segs] == [((20, 50), (180, 50))]


def test_an_x_marking_a_pad_is_not_a_wire(tmp_path):
    """The Acapulco Tone's pads: an X, the lead starting where its strokes cross.
    Two slanting wires that merely cross somewhere are long, and stay."""
    from sch2kicad.trace import vector_segments

    def draw(page):
        page.draw_line((40, 50), (100, 50), color=(0, 0, 0))            # the lead
        page.draw_line((36, 46), (44, 54), color=(0, 0, 0))             # the X
        page.draw_line((36, 54), (44, 46), color=(0, 0, 0))
        page.draw_line((20, 100), (180, 180), color=(0, 0, 0))          # a real crossing
        page.draw_line((20, 180), (180, 100), color=(0, 0, 0))
    segs, _ = vector_segments(_vector_page(tmp_path, draw), 1, 1.0)
    assert len(segs) == 3, segs
    assert ((40, 50), (100, 50)) in [(s.a, s.b) for s in segs]


def test_turned_vector_segments_land_where_the_turned_page_has_them(tmp_path):
    from sch2kicad.trace import vector_segments

    def draw(page):
        page.draw_line((10, 20), (60, 20), color=(0, 0, 0))
    segs, _ = vector_segments(_vector_page(tmp_path, draw), 1, 1.0, rot=270)
    # turned 270 clockwise (90 anticlockwise), (x, y) goes to (y, 200 - x)
    assert [(s.a, s.b) for s in segs] == [((20, 190), (20, 140))]


def test_a_box_drawn_as_a_quad_is_an_outline():
    """The Halsted St OD's PDF (KiCad printed through macOS) draws boxes as quad items,
    which the vector reader took for a curve's two ends and failed on: a quad has no
    x. A quad is four-sided and closed, an outline."""
    from sch2kicad.pdf import Pt
    from sch2kicad.trace import _closed
    q = [Pt(60, 80), Pt(120, 80), Pt(60, 140), Pt(120, 140)]
    assert _closed([("qu", q)])


def test_the_halsted_st_od_agrees_but_for_its_labelled_pads(tmp_path):
    """Refresh Audio FX's Halsted St OD: KiCad 10 printed to PDF through macOS, so its
    lettering is outlines and its boxes quads. Every pin traces; the one disagreement is
    the sheet's own: each jack line runs between two pads joined only by a label, and
    only a rail is joined by name."""
    c = _ready(tmp_path, "halsted-st-od.pdf", "halsted-st-od-refresh.circuit.json", page=1)
    rep = crosscheck_circuit(c, doc=SAMPLES / "halsted-st-od.pdf")
    assert rep.checked == rep.drawn == 101, rep.text()
    assert rep.disagreements == 2, rep.text()
    assert "J_IN" in rep.text() and "J_OUT" in rep.text()


def test_a_rail_name_in_the_wrong_case_is_not_the_rail():
    """OCR read the side of the Pocket Comb's rotated "BAT41" as "Vr", beside a diode
    that sits on VB; matched case-blind, VR was hung on that diode and welded VR to VB.
    A rail's name is written as the reading spells it, or in capitals."""
    from sch2kicad.trace import Conductors, Seg, name_nets
    cond = Conductors([Seg((100, 100), (100, 160)), Seg((300, 100), (300, 160))], [],
                      t_contacts=True, page=2.0)
    texts = [{"text": "VR", "box": [90, 70, 110, 90]},       # the flag over the first wire
             {"text": "Vr", "box": [290, 70, 310, 90]}]      # an OCR misread over the second
    named = name_nets(cond, texts, ["VR"], {cond.find(0), cond.find(1)})
    assert cond.find(0) != cond.find(1)                     # not joined by the name
    assert named == {cond.find(0): {"VR"}}
    # a sheet that writes the reading's "Vref" in capitals still names it
    named = name_nets(cond, [{"text": "VREF", "box": [290, 70, 310, 90]}], ["Vref"],
                      {cond.find(0), cond.find(1)})
    assert named == {cond.find(1): {"Vref"}}


def test_the_pocket_comb_agrees_but_for_its_battery(tmp_path):
    """Effects Layouts' Pocket Comb, a raster KiCad sheet: every pin traces, VR and VB
    stay two rails, and the one disagreement is the sheet's own: its battery symbol has
    the wire drawn straight through the cells, so in ink 9V and GND are one."""
    c = _ready(tmp_path, "Pocket-Comb-build-doc.pdf", "pocket-comb-effectslayouts.circuit.json", page=2)
    rep = crosscheck_circuit(c)
    assert rep.checked == rep.drawn == 268, rep.text()
    assert rep.disagreements == 2, rep.text()
    assert not any("VR" in m and "VB" in m for m in rep.merges), rep.text()
    assert all("9V_IN" in m for m in rep.merges + rep.misnamed), rep.text()


def test_the_junction_dots_say_which_colour_the_wires_are():
    """An Eagle sheet draws parts red and wires green, and a big IC's red box and
    leads can outrun the green wires on long straight runs — the Hot Mett's did, and
    the tracer followed the symbols. The dots are drawn in the wires' colour."""
    import numpy as np
    from sch2kicad.trace import Dot, wire_ink
    rgb = np.full((400, 600, 3), 255, np.int16)
    red, green = (212, 61, 61), (61, 188, 145)
    rgb[40:360, 100:103] = red                    # a tall IC box, long red runs
    rgb[40:360, 300:303] = red
    rgb[40:43, 100:303] = red
    rgb[357:360, 100:303] = red
    rgb[200:202, 320:420] = green                 # shorter green wires, with a dot
    rgb[260:262, 320:420] = green
    rgb[195:207, 364:376] = green
    assert wire_ink(rgb, 5)[50, 101], "without dots the long red runs win"
    ink = wire_ink(rgb, 5, dots=[Dot(370, 201, 6)])
    assert ink[200, 330] and ink[260, 330] and not ink[50, 101]


def test_the_hot_mett_agrees_with_its_drawing(tmp_path):
    """Moonn Electronics' Hot Mett, an Eagle sheet in red and green: with the wire
    colour read off its dots, every pin traces and the reading agrees outright."""
    c = _ready(tmp_path, "Hot_Mett_Docs.pdf", "hot-mett-moonn.circuit.json", page=3)
    rep = crosscheck_circuit(c)
    assert rep.checked == rep.drawn == 117, rep.text()
    assert rep.disagreements == 0, rep.text()


def test_a_sheet_stored_mirrored_in_its_pdf_is_flipped_back(tmp_path):
    """A PDF can store an image upside down in a mirror and flip it back with its
    placement matrix (Pedal Parts' ICL): the pixels as stored read 6 for 9 and put a
    DIP's notch at the bottom, so ingest undoes a mirroring placement."""
    import numpy as np
    import pdfs
    from PIL import Image
    from sch2kicad.ingest import pdf_schematic
    stored = Image.new("L", (800, 600), 255)
    stored.paste(0, (0, 0, 800, 100))                 # a black band along the top as stored
    picture = pdfs.stream("/Type /XObject /Subtype /Image /Width 800 /Height 600 "
                          "/ColorSpace /DeviceGray /BitsPerComponent 8", stored.tobytes())
    # draw it mirrored top to bottom: the band shows along the bottom of the page
    pdf = pdfs.write(tmp_path / "mirrored.pdf", 800, 600, "q 800 0 0 -600 0 600 cm /Im0 Do Q",
                     xobjects={"Im0": picture})
    img, meta, _ = pdf_schematic(pdf, 1, tmp_path)
    ink = np.array(img.convert("L")) < 128
    assert meta["mirrored"] is True
    assert ink[-50:].all() and not ink[:50].any()


def test_the_icl_agrees_with_its_drawing(tmp_path):
    """Pedal Parts' Invading Cannibal Ladybirds, the Hot Mett's circuit drawn again:
    flipped back out of its mirror, every pin traces and the reading agrees outright."""
    c = _ready(tmp_path, "ICL.pdf", "icl-pedalparts.circuit.json", page=4)
    rep = crosscheck_circuit(c)
    assert rep.checked == rep.drawn == 119, rep.text()
    assert rep.disagreements == 0, rep.text()


def test_the_fx96_agrees_with_its_drawing(tmp_path):
    """DOD's FX96 service sheet, OrCAD in vectors: every pin traces and the reading
    agrees outright, with the MN3005's and MN3101's GND pins on their own +9V rails."""
    c = _ready(tmp_path, "DOD-FX96.pdf", "fx96-dod.circuit.json", page=1)
    rep = crosscheck_circuit(c)
    assert rep.checked == rep.drawn == 293, rep.text()
    assert rep.disagreements == 0, rep.text()


def test_a_pin_name_inside_a_chip_is_not_a_rail_flag(tmp_path, monkeypatch):
    """The FX96 writes GND inside the NE570 and both BBD chips as pin names. Read as
    ground flags, they hang ground on the wire leaving each pin, and the MN3005's
    GND pin is on the filtered +9V, so the check welds that supply to ground."""
    from sch2kicad import trace
    c = _ready(tmp_path, "DOD-FX96.pdf", "fx96-dod.circuit.json", page=1)
    texts = trace.read_text(c.path.parent)
    kept = trace._not_pin_names(texts, c)
    dropped = [t["text"] for t in texts if t not in kept]
    assert dropped.count("GND") == 3
    assert [t["text"] for t in kept].count("+9V") == [t["text"] for t in texts].count("+9V")
    # the guard's witness: without it the same reading disagrees with the drawing
    monkeypatch.setattr(trace, "_not_pin_names", lambda texts, circuit: texts)
    rep = crosscheck_circuit(c)
    assert rep.disagreements > 0
    assert "V_BBD" in rep.text() and "GND" in rep.text()


def _arc_page(tmp_path, draw, size=(400, 200)):
    """A drawing made with ``draw(d)``, and what the tracer reads off it."""
    from PIL import Image, ImageDraw
    from sch2kicad.trace import page_ink, raster_segments
    img = Image.new("RGB", size, "white")
    draw(ImageDraw.Draw(img))
    path = tmp_path / "arc.png"
    img.save(path)
    segs, _ = raster_segments(path)
    return segs, page_ink(path)


def _hop(d, turn=False):
    """A wire along y=100 stepping over the one at x=200 with a semicircle; with
    ``turn`` it heads straight down after the jump, as the Gnomeratron's SW2A does."""
    d.line([(40, 100), (186, 100)], fill="black", width=2)
    d.arc([186, 86, 214, 114], 180, 360, fill="black", width=2)
    if turn:
        d.line([(214, 100), (214, 190)], fill="black", width=2)
    else:
        d.line([(214, 100), (360, 100)], fill="black", width=2)
    d.line([(200, 10), (200, 190)], fill="black", width=2)


def _joined(segs, arcs, *pts, crossings_join=False):
    from sch2kicad.trace import Conductors
    cond = Conductors(segs, [], t_contacts=True, page=8.0, arcs=arcs, crossings_join=crossings_join)
    return [cond.find(min(range(len(segs)), key=lambda i: _dist(p, segs[i]))) for p in pts]


@pytest.mark.parametrize("turn", [False, True])
def test_a_wire_that_arcs_over_another_hops_it(tmp_path, turn):
    """The Gnomeratron VTF steps every wire that does not connect over the other with a
    semicircle whose top touches the wire it jumps. A raster sees a wire stopping a
    stroke short, another starting as far past, and a scatter of short runs where the
    curve went; the arc is what says the two sides are one wire, and not on the other."""
    from sch2kicad.trace import arc_hops
    segs, ink = _arc_page(tmp_path, lambda d: _hop(d, turn))
    far = (214, 180) if turn else (340, 100)
    arcs = arc_hops(segs, ink, 8.0)
    assert len({a.at for a in arcs}) == 1, arcs
    near_side, far_side, jumped = _joined(segs, arcs, (60, 100), far, (200, 20))
    assert near_side == far_side, "the wire does not carry on over its arc"
    assert near_side != jumped, "the arc was read as a junction with the wire it jumps"
    # the rule's witness: without it the wire comes apart at every hop
    near_side, far_side = _joined(segs, [], (60, 100), far)
    assert near_side != far_side


def test_a_page_that_draws_its_hops_joins_where_it_crosses():
    """A sheet that steps over every wire it does not connect has said what a plain
    crossing is: a join. Most sheets say the opposite, with a dot, so the rule waits
    for the page to show its hops before it believes a crossing."""
    from sch2kicad.trace import Seg
    plus = [Seg((100, 200), (300, 200)), Seg((200, 100), (200, 300))]
    assert len(set(_joined(plus, [], (120, 200), (200, 120), crossings_join=True))) == 1
    assert len(set(_joined(plus, [], (120, 200), (200, 120)))) == 2
    # a T is a join either way, and two wires that only come near do not cross
    apart = [Seg((100, 200), (300, 200)), Seg((200, 206), (200, 300))]
    assert len(set(_joined(apart, [], (120, 200), (200, 280), crossings_join=True))) == 2


def test_an_arc_among_the_words_is_lettering(tmp_path):
    """The bowl of a letter sitting on a title-block rule is a semicircle over a line.
    Where the page was read for words, an arc inside one is part of it."""
    from sch2kicad.trace import arc_hops
    segs, ink = _arc_page(tmp_path, _hop)
    assert arc_hops(segs, ink, 8.0, words=[{"text": "o", "box": [190, 80, 212, 104]}]) == []


def test_the_gnomeratron_agrees_with_its_drawing(tmp_path, monkeypatch):
    """McSpunckle's Gnomeratron VTF: a hop for every crossing that does not join, so a
    plain crossing does. Every pin with a wire traces and the reading agrees; the two
    left over are rheostat wipers the sheet ties inside the part and never draws."""
    from sch2kicad import trace
    c = _ready(tmp_path, "Gnomeratron-VTF.png", "gnomeratron-vtf.circuit.json")
    work, hops = c.path.parent, {(598, 778), (718, 1078), (838, 820), (894, 988), (1048, 732),
                                 (1348, 762), (1386, 448), (1528, 238), (1632, 778), (1752, 1078)}
    image = work / "source.png"
    unit = trace.page_unit(image)
    segs, _ = trace.raster_segments(image, dots=trace.raster_dots(work))
    segs = trace.drop_bodies(trace.drop_boxes(segs, c, touch=unit), c, touch=unit)
    ink = trace.page_ink(image)
    # its ten hops and nothing else: not a wiper's lead, not "share!" when OCR is missing
    for words in (trace.read_text(work), []):
        found = {tuple(round(v) for v in a.at) for a in trace.arc_hops(segs, ink, unit, words=words)}
        assert found == hops, found ^ hops
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.checked == 247 and rep.drawn == 249, rep.text()
    # the witness: read with the rule of every other sheet, its crossings come apart
    monkeypatch.setattr(trace, "SPEAKS_IN_HOPS", 10_000)
    assert crosscheck_circuit(c).disagreements > 5


def _eagle_page(tmp_path, wire="green", part="red", lone_dot="green"):
    """Parts joined the way Eagle joins them: a dot between two leads, and a stub of
    wire a few pixels long between two leads; with a letter's stroke and a dot in
    the parts' own ink beside them that join nothing."""
    from PIL import Image, ImageDraw
    from sch2kicad.trace import Dot
    img = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(img)
    for y in (20, 280):                                   # what makes the wire colour a wire
        d.line([(20, y), (380, y)], fill=wire, width=2)
    d.line([(142, 150), (145, 150)], fill=part, width=2)  # lead, dot, lead: pin to pin
    d.ellipse([146, 146, 154, 154], fill=wire)
    d.line([(150, 155), (150, 158)], fill=part, width=2)
    d.line([(250, 100), (250, 147)], fill=part, width=2)  # lead, 4 px of wire, lead
    d.line([(250, 148), (250, 151)], fill=wire, width=2)
    d.line([(250, 152), (250, 200)], fill=part, width=2)
    d.line([(320, 148), (320, 151)], fill=wire, width=2)  # a letter's stroke on its own
    d.line([(350, 100), (350, 147)], fill=part, width=2)  # one that meets a part at one end
    d.line([(350, 148), (350, 151)], fill=wire, width=2)
    d.line([(160, 240), (196, 240)], fill=part, width=2)  # a dot found in a part's ink
    d.ellipse([196, 236, 204, 244], fill=lone_dot)
    d.line([(204, 240), (240, 240)], fill=part, width=2)
    path = tmp_path / "eagle.png"
    img.save(path)
    dots = [Dot(150, 150, 4), Dot(200, 240, 4), Dot(60, 20, 1), Dot(300, 280, 1)]
    return path, dots


def _pin_to_pin(path, dots):
    from sch2kicad.trace import pin_to_pin, raster_segments
    segs, _ = raster_segments(path, dots=dots)
    return pin_to_pin(path, dots, segs, 3.0)


def test_parts_joined_pin_to_pin_have_a_conductor(tmp_path):
    """Eagle joins two parts with no wire: the leads meet at a dot, or reach each
    other through a stub of wire shorter than any the tracer takes. The Abductor II
    joins sixteen pins so, and in red and green the tracer sees nothing of it but a
    dot or a stub on its own. Each is a conductor, and nothing else is: not a
    letter's stroke in the wire colour, even one touching a part at one end, and not
    a dot ingest found in a part's own ink."""
    path, dots = _eagle_page(tmp_path, lone_dot="blue")
    joins = _pin_to_pin(path, dots)
    at = sorted((round((s.a[0] + s.b[0]) / 2), round((s.a[1] + s.b[1]) / 2)) for s in joins)
    assert len(at) == 2 and _near(at[0], (150, 150)) and _near(at[1], (250, 149)), joins


def test_a_sheet_in_one_colour_is_not_joined_pin_to_pin(tmp_path):
    """Drawn in one colour, a part's leads are wire ink the tracer already follows,
    so a dot with no wire at it joins nothing: the Gnomeratron's six are the heavy
    strokes of its title, where ink of the wire colour runs on out of the "dot"."""
    from PIL import Image, ImageDraw
    from sch2kicad.trace import Dot
    img = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(img)
    for y in (20, 280):
        d.line([(20, y), (380, y)], fill="black", width=2)
    d.ellipse([146, 146, 154, 154], fill="black")
    d.line([(150, 150), (175, 130)], fill="black", width=5)
    path = tmp_path / "title.png"
    img.save(path)
    assert _pin_to_pin(path, [Dot(150, 150, 4), Dot(60, 20, 1), Dot(300, 280, 1)]) == []


def _near(p, q, tol=2):
    return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol


def test_the_abductor_agrees_with_its_drawing(tmp_path, monkeypatch):
    """Deadastronaut's Abductor II, Eagle red and green. The pins it joins pin to pin
    trace, the PT2399s' tied grounds among them; what is left is R10's lead, a wire
    that runs inside the TIME pot's reach and touches none of its pins."""
    from sch2kicad import trace
    c = _ready(tmp_path, "Abductor-II-Deadastronaut.pdf", "abductor-ii-deadastronaut.circuit.json", page=4)
    rep = crosscheck_circuit(c)
    assert rep.disagreements == 0, rep.text()
    assert rep.checked == 359 and len(rep.unmatched) == 1 and rep.misplaced == [], rep.text()
    # the witness: without the rule, the pins joined pin to pin trace to nothing
    monkeypatch.setattr(trace, "pin_to_pin", lambda *a, **k: [])
    assert crosscheck_circuit(c).checked < 345
