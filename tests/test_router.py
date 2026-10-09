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


@pytest.mark.parametrize("name", ["ripped-speaker-cb-mod", "musket-blackout",
                                  pytest.param("colonel-angus", marks=_needs_drawing("colonel-angus"))])
def test_no_wire_steps_aside_between_two_bends(tmp_path, name):
    """A step of a grid or two between two bends reads as a slip of the pen. These
    three each had one or two where a route with longer runs was there for the taking."""
    shape = _shape(SAMPLES / "circuits" / f"{name}.circuit.json", tmp_path)
    assert {net: s for net, s in shape.items() if s[1]} == {}


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
