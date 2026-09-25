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
    # the one thing left is a label the drawing really does write nearer the wrong
    # wire: OCTAVE-1's ground flag is a red stem with 8 px of green at the far end,
    # so its name lands 18 px above R7 and 26 px below its own wire
    assert {m.split(" are one thing")[0] for m in rep.merges} == {"GND, OCT2_TOP"}, rep.text()
    assert rep.splits == [], rep.text()


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
