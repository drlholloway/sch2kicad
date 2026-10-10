"""How the router lays a net down: where its trunk runs and where it may bend."""

import math
from collections import defaultdict
from pathlib import Path

import pytest

from sch2kicad.build import Builder
from sch2kicad.circuit import load
from sch2kicad.parts import load_profile
from sch2kicad.router import Router

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent / "samples"


def _shape(circuit: Path, out: Path) -> dict[str, tuple[int, int]]:
    """Each routed net's corners and jogs (a step of two grid cells or less between two bends)."""
    b = Builder(load(circuit), load_profile("tht"), "shape")
    b.run(out)
    wires = {}
    for it in b.w.items:
        if it[0] == "wire":
            (_, (_, ax, ay), (_, bx, by)) = it[1]
            wires[it[-1][1].strip('"')] = ((ax, ay), (bx, by))
    shape = {}
    for net, uu in b.net_items.items():
        ws = [wires[u] for u in uu if u in wires]
        kinds, ends = defaultdict(set), defaultdict(int)
        for a, c in ws:
            for p in (a, c):
                kinds[p].add("h" if abs(a[1] - c[1]) < 1e-3 else "v")
                ends[p] += 1
        corners = {p for p, k in kinds.items() if k == {"h", "v"} and ends[p] == 2}
        jogs = sum(1 for a, c in ws if math.dist(a, c) <= 2.541 and a in corners and c in corners)
        shape[net] = (len(corners), jogs)
    return shape


def _drawn(name: str) -> bool:
    """Is the drawing this reading came from here? The router follows its wires when it is."""
    img = load(SAMPLES / "circuits" / f"{name}.circuit.json").source_image()
    return bool(img and img.exists())


def _needs_drawing(name: str):
    return pytest.mark.skipif(not _drawn(name), reason="third-party drawing not present")


@_needs_drawing("altered-states")
def test_a_trunk_runs_down_the_column_its_pins_share(tmp_path):
    """Chuck D. Bones draws Altered States' Q1 collector as one straight column: R5
    above, the collector below, C2 and R3 teeing in from the side. Grown from R3's
    end, the tree put its trunk in R3's column and the collector and R5 both stepped
    over to it. It takes both rules to keep it straight: growing the tree from every
    pin, so it can start from the collector, and the drawing's own column to say
    which start to keep."""
    shape = _shape(SAMPLES / "circuits" / "altered-states.circuit.json", tmp_path)
    assert shape["Q1_C"] == (0, 0)


def test_no_wire_steps_aside_between_two_bends(tmp_path):
    """A step of a grid or two between two bends reads as a slip of the pen. Ripped
    Speaker had one where a route with longer runs was there for the taking."""
    shape = _shape(SAMPLES / "circuits" / "ripped-speaker-cb-mod.circuit.json", tmp_path)
    assert {net: s for net, s in shape.items() if s[1]} == {}


def test_a_short_step_costs_more_than_its_two_bends(tmp_path, monkeypatch):
    """Not every jog can go: a part placed a grid off forces one somewhere (Musket's C2
    sits on its own tip's column). What the jog cost buys is fewer: Fixd Fuzz has
    four without it and one with."""
    import sch2kicad.router as router
    with_cost = sum(j for _, j in _shape(SAMPLES / "circuits" / "fixd-fuzz-roberts.circuit.json",
                                          tmp_path / "with").values())
    monkeypatch.setattr(router, "JOG", 0.0)
    without = sum(j for _, j in _shape(SAMPLES / "circuits" / "fixd-fuzz-roberts.circuit.json",
                                        tmp_path / "without").values())
    assert with_cost < without


@pytest.mark.parametrize("corner", [(5, 0), (0, 5)])
def test_a_route_follows_the_drawing_where_it_costs_nothing_extra(corner):
    """Two L-shaped routes join (0,0) to (5,5) at the same length and bends; the one
    the drawing draws is the one taken, whichever of the two that is."""
    r = Router((-10.0, -10.0, 20.0, 20.0))
    leg = [(x, corner[1]) for x in range(6)] + [(corner[0], y) for y in range(6)]
    r.guide["N"] = set(leg)
    path, _ = r.search("N", (0, 0), None, {(5, 5)}, 8)
    assert corner in path


@pytest.mark.skipif(not (SAMPLES / "Altered States v0.1.1.sch2kicad" / "source.png").exists(),
                    reason="third-party drawing not present")
def test_the_drawing_gives_each_net_its_own_wires():
    from sch2kicad.trace import drawn_wires
    c = load(SAMPLES / "Altered States v0.1.1.sch2kicad" / "circuit.json")
    drawn = drawn_wires(c)
    # Q1's collector column: R5 down to the collector, one vertical run past C3, C2 and R3
    q1 = next(p for p in c.parts if p.ref == "Q1")
    collector = next(pin.at for name, pin in q1.pins.items() if pin.net == "Q1_C")
    runs = [s for s in drawn["Q1_C"] if abs(s.a[0] - s.b[0]) < 3 and abs(s.a[0] - collector[0]) < 6]
    assert runs and max(abs(s.a[1] - s.b[1]) for s in runs) > 100


@_needs_drawing("altered-states")
def test_grounds_the_drawing_lines_up_stand_on_one_line(tmp_path):
    """Chuck D. Bones runs every ground along the bottom of Altered States to one
    baseline, R1's on the left to C14's on the right, on leads of whatever length
    that takes. A fixed stub under each pin put C13's, C14's and VR3's a grid or
    three off the rest."""
    b = Builder(load(SAMPLES / "circuits" / "altered-states.circuit.json"), load_profile("tht"), "rows")
    b.run(tmp_path)
    grounds = sorted(s.at[1] for s in b.w.symbols if s.lib.lib_id.endswith(":GND"))
    level = max(set(grounds), key=grounds.count)
    assert grounds.count(level) == 11


def test_pins_the_drawing_lines_up_stay_lined_up(tmp_path):
    """Chuck draws C8's end under D1's and D2's (Q2's collector), and C10's under D3's
    and D4's (Q3's drain). KiCad's capacitor is shorter than its diodes, so each part
    centred on its own pins left the caps a grid to one side and their wires jogged."""
    b = Builder(load(SAMPLES / "circuits" / "altered-states.circuit.json"), load_profile("tht"), "line")
    b.run(tmp_path)

    def x(ref, net):
        for pl in b.layout.placed:
            if pl.res.ref == ref:
                for sem, pin in pl.res.part.pins.items():
                    if pin.net == net:
                        return pl.pin_pos(pl.pinmap[sem])[0]
    assert x("C8", "Q2_C") == x("D1", "Q2_C") == x("D2", "Q2_C")
    assert x("C10", "Q3_D") == x("D3", "Q3_D") == x("D4", "Q3_D")


@_needs_drawing("memory-lapse")
def test_a_stripboard_reading_takes_no_guide_from_its_board():
    """Memory Lapse is drawn from a Dirtbox vero layout: its positions are a schematic
    drawn for it, and its image is the board. Traced, the board's copper strips were
    read as wires to follow and ground leads to line up."""
    b = Builder(load(SAMPLES / "circuits" / "memory-lapse.circuit.json"), load_profile("tht"), "vero")
    assert b._drawn() is None and b._flag_rows({}) == {}


def test_the_supply_comes_in_on_the_house_power_socket(tmp_path):
    """Lane's boards take the supply on Conn_01x02_Socket_Power, value POWER, pin 1 to
    the supply ahead of the reverse-polarity diode and pin 2 to ground, rather than
    a lone square pad. Memory Lapse's +9V is such a net: the rail proper, VCC, starts
    past D1. LED+ is power-ish by name but no supply, and stays a pad."""
    b = Builder(load(SAMPLES / "circuits" / "memory-lapse.circuit.json"), load_profile("tht"), "power")
    b.run(tmp_path)
    sockets = [pl for pl in b.layout.placed if pl.res.lib_id.endswith(":Conn_01x02_Socket_Power")]
    assert len(sockets) == 1 and sockets[0].res.value == "POWER"
    j = sockets[0]
    assert b.pin_net(j, j.pinmap["+"]) == "+9V" and b.pin_net(j, j.pinmap["-"]) == "GND"
    pads = {pl.res.value for pl in b.layout.placed if pl.res.ref.startswith("H")}
    assert pads == {"LED+"}


def test_a_run_lies_straight_along_the_pin_it_feeds(tmp_path):
    """Lane laid Memory Lapse's PT2399 pin 2 run out on the pin's own row: R3, then
    STABILITY turned to lie along the row, then C14, with C11 hung below it and its
    ground under it. Drawn part by part, the run stepped up and down at every part."""
    b = Builder(load(SAMPLES / "circuits" / "memory-lapse.circuit.json"), load_profile("tht"), "runs")
    b.run(tmp_path)
    placed = {pl.res.ref: pl for pl in b.layout.placed}

    def ys(ref, *sems):
        pl = placed[ref]
        return {round(pl.pin_pos(pl.pinmap[s])[1], 3) for s in sems}
    ic2 = placed["IC2"]
    row = {round(ic2.pin_pos("2")[1], 3)}
    assert ys("R3", *placed["R3"].pinmap) == row
    assert ys("STABILITY", "ccw", "cw") == row
    assert ys("C14", *placed["C14"].pinmap) == row
    c11 = placed["C11"]
    plus, minus = (c11.pin_pos(c11.pinmap[s]) for s in ("+", "-"))
    assert round(plus[1], 3) in row and minus[1] > plus[1]         # hung below, ground under


def test_the_supply_is_one_row_in_the_top_left(tmp_path):
    """Lane draws the supply as a row across the top left corner: POWER socket, D1, the
    78L05, with C4 and C1 hanging under the nodes either side of the regulator, their
    grounds level, and VCC flagged once at its node rather than at the regulator's
    input as well."""
    b = Builder(load(SAMPLES / "circuits" / "memory-lapse.circuit.json"), load_profile("tht"), "supply")
    b.run(tmp_path)
    placed = {pl.res.ref: pl for pl in b.layout.placed}

    def at(ref, sem):
        pl = placed[ref]
        return pl.pin_pos(pl.pinmap[sem])
    row = {round(at(*p)[1], 3) for p in (("J1", "+"), ("D1", "A"), ("D1", "K"), ("REG1", "VI"), ("REG1", "VO"),
                                         ("C4", "+"), ("C1", "+"))}
    assert len(row) == 1
    assert at("J1", "+")[0] < at("D1", "A")[0] < at("D1", "K")[0] < at("C4", "+")[0] < at("REG1", "VI")[0] \
        < at("REG1", "VO")[0] < at("C1", "+")[0]
    assert at("C4", "-")[1] > at("C4", "+")[1] and at("C1", "-")[1] > at("C1", "+")[1]
    grounds = {b.flag_rows[f"{r}.{placed[r].pinmap[s]}"] for r, s in (("C4", "-"), ("C1", "-"), ("REG1", "GND"))}
    assert len(grounds) == 1
    supply = [placed[r].extent() for r in ("J1", "D1", "C4", "REG1", "C1")]
    rest = [pl.extent() for pl in b.layout.placed if pl.res.ref not in ("J1", "D1", "C4", "REG1", "C1")]
    x0, x1 = min(e[0] for e in supply), max(e[2] for e in supply)
    under = [e for e in rest if e[0] < x1 and e[2] > x0]                       # in the row's columns
    assert under and max(e[3] for e in supply) < min(e[1] for e in under)       # nothing above it there
    parts = [pl.extent() for pl in b.layout.placed
             if pl.res.ref not in ("J1", "D1", "C4", "REG1", "C1") and pl.res.part.type != "port"]
    assert x0 <= min(e[0] for e in parts) + 2 * 2.54                            # left of every part
    assert b.power_nodes["VCC"][f"C4.{placed['C4'].pinmap['+']}"] == "lead"


def test_a_designer_copy_is_laid_out_as_ours_and_never_traced(tmp_path):
    """A stripboard reading's final/ copy leaves its board off, since it no longer
    matches it, but its positions are still the schematic drawn for the board and
    its image still the board. Marked "arrangement": "ours", it takes no guide from
    that image and is laid out like the board reading it came from."""
    c = load(SAMPLES / "circuits" / "memory-lapse.circuit.json")
    c.board, c.arrangement = "", "ours"
    b = Builder(c, load_profile("tht"), "final")
    assert b._drawn() is None and b._flag_rows({}) == {}
    b.run(tmp_path)
    assert "VCC" in b.power_nodes                      # the supply row, as for the board


def _memory_lapse(tmp_path, name):
    b = Builder(load(SAMPLES / "circuits" / "memory-lapse.circuit.json"), load_profile("tht"), name)
    b.run(tmp_path)
    return b, {pl.res.ref: pl for pl in b.layout.placed}


def _at(placed, ref, sem):
    pl = placed[ref]
    return pl.pin_pos(pl.pinmap[sem])


def test_grounds_in_one_block_end_level_with_no_drawing(tmp_path):
    """Lane hangs C3's and C2's grounds, under PT2399's CC1 and CC0, on one line.
    Memory Lapse has no drawing to say so (it is drawn from a board), so grounds in
    one block within a few grid of each other are levelled instead."""
    b, placed = _memory_lapse(tmp_path, "grounds")
    owners = [f"{r}.{placed[r].pinmap[s]}" for r, s in (("C3", "2"), ("C2", "2"))]
    assert all(o in b.flag_rows for o in owners)
    assert len({b.flag_rows[o] for o in owners}) == 1


def test_a_cap_across_two_chip_pins_stands_beside_them(tmp_path):
    """Lane stands PT2399's C10 just off pins 9 and 10, between them and parallel to
    the chip's side; placed where it was drawn it hung four grid below."""
    b, placed = _memory_lapse(tmp_path, "pincaps")
    ic2 = placed["IC2"]
    p9, p10 = ic2.pin_pos("9"), ic2.pin_pos("10")
    ends = [placed["C10"].pin_pos(q.number) for q in placed["C10"].pins()]
    assert abs(ends[0][0] - ends[1][0]) < 1e-3                       # stands up, like the side
    assert 0 < ends[0][0] - p9[0] <= 4 * 1.27                        # just off the pins
    mid = (p9[1] + p10[1]) / 2
    assert abs((ends[0][1] + ends[1][1]) / 2 - mid) <= 1.27            # centred between them


def test_a_lead_points_straight_at_what_it_joins(tmp_path):
    """Lane puts R13 straight under the R12/VOLUME node and C16 straight under MIX."""
    b, placed = _memory_lapse(tmp_path, "point")
    r13 = placed["R13"]
    top = min((r13.pin_pos(q.number) for q in r13.pins()), key=lambda p: p[1])
    node = [placed[r].pin_pos(n) for r in ("R12", "VOLUME") for n in placed[r].pinmap.values()
            if b.pin_net(placed[r], n) == b.pin_net(r13, next(q.number for q in r13.pins()
                                                                if r13.pin_pos(q.number) == top))]
    assert any(abs(p[0] - top[0]) < 1e-3 for p in node)
    c16 = placed["C16"]
    tops = min((c16.pin_pos(q.number) for q in c16.pins()), key=lambda p: p[1])
    mix = [placed["MIX"].pin_pos(n) for n in placed["MIX"].pinmap.values()]
    assert any(abs(p[0] - tops[0]) < 1e-3 for p in mix)


def test_the_input_runs_straight_into_the_pin_it_feeds(tmp_path):
    """Lane's IN comes straight across into LM386 pin 2, R2 hanging up off the line
    to VCC, the way the output runs straight out to OUT. The run had stopped before
    it began: pin 2's net holds only R2, which hangs across the run, and the pad."""
    b, placed = _memory_lapse(tmp_path, "input")
    row = round(placed["IC1"].pin_pos("2")[1], 3)
    pad = next(pl for pl in b.layout.placed if pl.res.part.type == "port"
               and b.pin_net(pl, next(iter(pl.pinmap.values()))) == "IN")
    assert round(pad.pin_pos(next(iter(pad.pinmap.values())))[1], 3) == row
    r2 = placed["R2"]
    ends = sorted((r2.pin_pos(q.number) for q in r2.pins()), key=lambda p: p[1])
    assert round(ends[1][1], 3) == row and ends[0][1] < row          # on the line, hanging up


def test_side_by_side_parts_leave_room_for_their_names(tmp_path):
    """Lined up with PT2399 pins 14, 13 and 12, R6, R8 and R11 stood a grid apart and
    R8's name was pushed down beside R11's. Lane gave the stack room."""
    b, placed = _memory_lapse(tmp_path, "room")
    ys = sorted((placed[r].box()[1] + placed[r].box()[3]) / 2 for r in ("R6", "R8", "R11"))
    assert ys[1] - ys[0] >= 5 * 1.27 - 1e-3 and ys[2] - ys[1] >= 5 * 1.27 - 1e-3


def test_a_row_of_level_grounds_keeps_a_flag_each(tmp_path):
    """C3's and C2's grounds, levelled under PT2399, stand 6.35 mm apart: inside the
    distance at which two flags of a rail are taken for one node drawn twice, so C2's
    was wired across to C3's in a loop, and then hung a grid lower. A row is a
    baseline on purpose: each pin on it keeps its own flag, on the line."""
    b, placed = _memory_lapse(tmp_path, "baseline")
    tips = {}
    for r in ("C3", "C2"):
        pl = placed[r]
        n = pl.pinmap["2"]
        tips[r] = pl.pin_pos(n)
        assert f"{r}.{n}" in b.flag_rows
    line = b.flag_rows[f"C3.{placed['C3'].pinmap['2']}"]
    flags = [s.at for s in b.w.symbols if s.lib.lib_id.endswith(":GND")]
    for r, tip in tips.items():
        assert any(abs(f[0] - tip[0]) < 1e-3 and abs(f[1] - line) < 1e-3 for f in flags), r


@pytest.mark.parametrize("name", ["memory-lapse", "altered-states", "hellbender-cb-mod"])
def test_every_name_stands_nearest_its_own_part(tmp_path, name):
    """A name pushed out of the clear round its own part used to land wherever was
    clear, beside a neighbour: Memory Lapse's R8 read as R11's. Every reference and
    value now stands nearer its own body than any other's, and on nothing."""
    b = Builder(load(SAMPLES / "circuits" / f"{name}.circuit.json"), load_profile("tht"), "names")
    b.run(tmp_path)
    assert b.text_clashes() == 0


def test_a_wire_may_turn_at_a_pin_tip(tmp_path, monkeypatch):
    """Lane runs a column straight down through a row of pin tips (R10's and R7's
    left ends on Memory Lapse) where our wires had to set off along each lead a
    grid and then turn, two corners a pin. Turning at the tip, at a bend's cost,
    takes Memory Lapse from 53 corners to 47 (and the drawn samples from 53 jogs
    to 25)."""
    import sch2kicad.router as router
    path = SAMPLES / "circuits" / "memory-lapse.circuit.json"
    turning = sum(c for c, _ in _shape(path, tmp_path / "turn").values())
    monkeypatch.setattr(router, "TIP_TURN", None)
    stepping = sum(c for c, _ in _shape(path, tmp_path / "step").values())
    assert turning < stepping


def test_a_pot_a_run_ends_at_stands_with_its_ground_down(tmp_path):
    """The run from PT2399's VCO pin goes through R1 to DELAY and ends in ground. Lane
    stands DELAY upright with that end down to its flag; turned on its side to lie
    along the run, its grounded end pointed sideways and needed a dog-leg."""
    b, placed = _memory_lapse(tmp_path, "delay")
    pl = placed["DELAY"]
    ground_end = next(n for s, n in pl.pinmap.items() if s in ("ccw", "cw") and b.pin_net(pl, n) == "GND")
    assert pl.pin_dir(ground_end) == (0, 1)


def test_a_sideways_ground_takes_its_flag_at_the_end_of_a_short_lead(tmp_path):
    """A ground pin pointing sideways took its flag out a grid, then down a stub: a
    corner in open wire. Lane runs a short lead straight to the ground symbol, the
    lead's end its pin. The POWER socket's ground pin points sideways; where the spot
    under the lead is free (LM386 pin 3's is not: pin 2's lead runs there) the flag
    stands right at the end of it."""
    b, placed = _memory_lapse(tmp_path, "lead")
    j1 = placed["J1"]
    pin = j1.pin_pos(j1.pinmap["-"])
    flags = [s.at for s in b.w.symbols if s.lib.lib_id.endswith(":GND")]
    assert any(abs(f[1] - pin[1]) < 1e-3 and 0 < abs(f[0] - pin[0]) <= 3 * 2.54 for f in flags)
