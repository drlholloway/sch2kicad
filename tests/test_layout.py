"""What the finished sheet looks like, read back out of the file KiCad saved.

Nothing here touches the builder's own idea of where it put things: the checks
parse the ``.kicad_sch``, work out where each visible piece of text actually
lands, and complain if it sits on a part, on other text, or under a wire. That
is the only way to catch a placer that is confidently wrong — the one that put
every turned part's reference and value on the same line for months.
"""

import math
from pathlib import Path

import pytest

from sch2kicad import sexpr
from sch2kicad.build import build
from sch2kicad.geometry import apply, matrix
from sch2kicad.kicad_env import kicad_cli
from sch2kicad.schematic import field_positions, text_box
from sch2kicad.symlib import SymbolResolver, _graphics_bbox, _parse_pins

HERE = Path(__file__).resolve().parent
SAMPLES = sorted((HERE.parent / "samples" / "circuits").glob("*.circuit.json"))


def _have_kicad():
    try:
        kicad_cli()
        return SymbolResolver().exists("2025_PedalDivision_Resistors:TH_Resistor_1/4w")
    except Exception:
        return False


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _box(text: str, at, size: float, justify, turned: bool):
    """Where a piece of text lands. KiCad reads a property's angle in the symbol's
    own frame, so a 90° property on a 90° symbol still reads across the page."""
    lines = str(text).split("\n")
    w = max(len(ln) for ln in lines) * size * 0.72
    h = len(lines) * size * 1.15
    if turned:
        w, h = h, w
    x, y = at
    x0 = x if "left" in justify else (x - w if "right" in justify else x - w / 2)
    y0 = y if "top" in justify else (y - h if "bottom" in justify else y - h / 2)
    return (x0, y0, x0 + w, y0 + h)


def _hidden(node) -> bool:
    eff = sexpr.child(node, "effects") or []
    h = sexpr.child(node, "hide") or sexpr.child(eff, "hide")
    return h is not None and str(sexpr.value(h, 1, "")) == "yes"


def _pin_at(root, ref: str, number: str) -> tuple[float, float]:
    """Where a numbered pin of a placed symbol lands on the sheet, in millimetres."""
    libs = {str(s[1]): s for s in sexpr.children(sexpr.child(root, "lib_symbols") or [""], "symbol")}
    for sym in sexpr.children(root, "symbol"):
        props = {str(p[1]): p for p in sexpr.children(sym, "property")}
        if str(props.get("Reference", ["", "", ""])[2]) != ref:
            continue
        node = libs.get(str(sexpr.value(sexpr.child(sym, "lib_id"), 1, "")))
        if node is None:
            continue
        unit = int(_num(sexpr.value(sexpr.child(sym, "unit"), 1, 1), 1))
        for pin in _parse_pins(node):
            if pin.number != number or pin.unit not in (0, unit):
                continue
            at = sexpr.child(sym, "at")
            mirror = sexpr.child(sym, "mirror")
            m = matrix(int(_num(sexpr.value(at, 3, 0))), str(mirror[1]) if mirror else None)
            dx, dy = apply(m, pin.x, pin.y)      # the library point is the tip itself
            return (round(_num(at[1]) + dx, 2), round(_num(at[2]) + dy, 2))
    raise AssertionError(f"no pin {ref}.{number} on the sheet")


def _effects_of(node):
    eff = sexpr.child(node, "effects") or []
    font = sexpr.child(eff, "font") or []
    size = _num(sexpr.value(sexpr.child(font, "size"), 1, 1.27), 1.27)
    return size, [str(v) for v in (sexpr.child(eff, "justify") or [])[1:]] or ["center"]


class Sheet:
    """Texts, bodies and wires of a built schematic, in sheet millimetres."""

    def __init__(self, path: Path):
        root = sexpr.parse(path.read_text())
        libs = {str(s[1]): s for s in sexpr.children(sexpr.child(root, "lib_symbols") or [""], "symbol")}
        self.texts: list[tuple[tuple, str]] = []
        self.bodies: list[tuple[tuple, str]] = []
        self.wires: list[tuple] = []
        for item in sexpr.children(root, "wire"):
            xy = [c for c in (sexpr.child(item, "pts") or [""])[1:] if sexpr.head(c) == "xy"]
            if len(xy) == 2:
                self.wires.append(((_num(xy[0][1]), _num(xy[0][2])), (_num(xy[1][1]), _num(xy[1][2]))))
        for item in sexpr.children(root, "text"):
            at = sexpr.child(item, "at")
            size, just = _effects_of(item)
            self.texts.append((_box(str(item[1]), (_num(at[1]), _num(at[2])), size, just, False), "text"))
        for sym in sexpr.children(root, "symbol"):
            node = libs.get(str(sexpr.value(sexpr.child(sym, "lib_id"), 1, "")))
            if node is None:
                continue
            at = sexpr.child(sym, "at")
            origin, angle = (_num(at[1]), _num(at[2])), int(_num(sexpr.value(at, 3, 0)))
            mirror = sexpr.child(sym, "mirror")
            m = matrix(angle, str(mirror[1]) if mirror else None)
            unit = int(_num(sexpr.value(sexpr.child(sym, "unit"), 1, 1), 1))
            props = {str(p[1]): p for p in sexpr.children(sym, "property")}
            ref = str(props["Reference"][2]) if "Reference" in props else "?"
            bb = _graphics_bbox(node, str(node[1]).split(":", 1)[-1], unit, 1)
            if bb != (0.0, 0.0, 0.0, 0.0):
                pts = [apply(m, x, y) for x, y in ((bb[0], bb[1]), (bb[0], bb[3]), (bb[2], bb[1]), (bb[2], bb[3]))]
                xs = [origin[0] + p[0] for p in pts]
                ys = [origin[1] + p[1] for p in pts]
                self.bodies.append(((min(xs), min(ys), max(xs), max(ys)), ref))
            for name, p in props.items():
                if _hidden(p) or not str(p[2]).strip():
                    continue
                pat = sexpr.child(p, "at")
                size, just = _effects_of(p)
                turned = (int(_num(sexpr.value(pat, 3, 0))) - angle) % 180 != 0
                self.texts.append((_box(str(p[2]), (_num(pat[1]), _num(pat[2])), size, just, turned),
                                   f"{ref}.{name}"))

    @staticmethod
    def owner(label: str) -> str:
        return label.split(".")[0]


def _overlaps(a, b, pad=0.15) -> bool:
    return (a[0] + pad < b[2] - pad and b[0] + pad < a[2] - pad
            and a[1] + pad < b[3] - pad and b[1] + pad < a[3] - pad)


def _crossed(wire, box, pad=0.3) -> bool:
    (ax, ay), (bx, by) = wire
    x0, y0, x1, y1 = box[0] + pad, box[1] + pad, box[2] - pad, box[3] - pad
    if x1 <= x0 or y1 <= y0:
        return False
    if abs(ay - by) < 1e-6:
        return y0 < ay < y1 and min(ax, bx) < x1 and x0 < max(ax, bx)
    if abs(ax - bx) < 1e-6:
        return x0 < ax < x1 and min(ay, by) < y1 and y0 < max(ay, by)
    return False


pytestmark = pytest.mark.skipif(not _have_kicad(), reason="needs KiCad 10 and the Pedal Division library")


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_nothing_readable_is_covered_up(tmp_path, circuit):
    sheet = Sheet(build(circuit, tmp_path / "board").sch)
    clashes = []
    for i, (a, an) in enumerate(sheet.texts):
        for b, bn in sheet.texts[i + 1:]:
            if _overlaps(a, b):
                kind = "its own" if sheet.owner(an) == sheet.owner(bn) else "other"
                clashes.append(f"{an} over {kind} text {bn}")
        for body, ref in sheet.bodies:
            if sheet.owner(an) != ref and _overlaps(a, body):
                clashes.append(f"{an} over the body of {ref}")
        if any(_crossed(w, a) for w in sheet.wires):
            clashes.append(f"a wire runs through {an}")
    assert not clashes, f"{circuit.name}:\n  " + "\n  ".join(clashes)


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_nothing_hangs_off_the_page(tmp_path, circuit):
    """Anything past the paper edge prints as nothing, and the netlist never notices.

    The Tri-Vibe used to stand IC2's power unit over the top edge with its VA flag
    off the sheet altogether: a power unit put beside its amplifier was checked
    against the other parts and against nothing else.
    """
    from sch2kicad.geometry import PAPERS

    sch = build(circuit, tmp_path / "board").sch
    root = sexpr.parse(sch.read_text())
    paper = str(sexpr.value(sexpr.child(root, "paper"), 1, "A4"))
    w, h = next((pw, ph) for name, pw, ph in PAPERS if name == paper)
    sheet = Sheet(sch)
    edge = 2.0
    off = [f"{what} at {tuple(round(v, 1) for v in box)}"
           for box, what in sheet.bodies + sheet.texts
           if box[0] < edge or box[1] < edge or box[2] > w - edge or box[3] > h - edge]
    off += [f"a wire at {p}" for wire in sheet.wires for p in wire
            if p[0] < edge or p[1] < edge or p[0] > w - edge or p[1] > h - edge]
    assert not off, f"{circuit.name} on {paper} ({w}x{h}):\n  " + "\n  ".join(off)


def test_a_field_steps_aside_for_what_is_already_there():
    """The whole point of the placer: given somewhere occupied, it finds somewhere else."""
    class FakeSym:
        power = False

    from sch2kicad.geometry import Orient
    from sch2kicad.schematic import SymbolInstance
    inst = SymbolInstance(lib=FakeSym(), ref="R1", unit=1, at=(0, 0), orient=Orient(0, None), value="10K")
    body = (-2.0, -1.0, 2.0, 1.0)
    names = ["Reference", "Value"]

    free = field_positions(inst, body, [(0, -1), (0, 1)], names)
    assert all(p[0] > body[2] for p in free.values()), "with both sides free it uses the right"

    wall = (body[2], -10.0, body[2] + 12.0, 10.0)      # the whole right-hand side taken
    moved = field_positions(inst, body, [(0, -1), (0, 1)], names, blocked=[wall])
    boxes = [text_box(moved[k], "R1" if k == "Reference" else "10K") for k in names]
    assert not any(_overlaps(b, wall, pad=0.0) for b in boxes), moved
    assert all(p[0] < body[0] for p in moved.values()), "the left-hand side was free"


def test_stacked_fields_do_not_sit_on_each_other():
    """A turned part's fields read across the page, so they stack down it, not along it."""
    from sch2kicad.geometry import Orient
    from sch2kicad.schematic import SymbolInstance

    class FakeSym:
        power = False

    for angle in (0, 90, 180, 270):
        inst = SymbolInstance(lib=FakeSym(), ref="C14", unit=1, at=(0, 0), orient=Orient(angle, None),
                              value="100nF")
        pos = field_positions(inst, (-1.0, -2.5, 1.0, 2.5), [(0, -1), (0, 1)], ["Reference", "Value"])
        # measured as KiCad draws it: text_box pads a little, and the stack is tighter
        # than that padding, which is fine so long as the glyphs themselves keep apart
        a = _box("C14", pos["Reference"], 1.27, ["center"], False)
        b = _box("100nF", pos["Value"], 1.27, ["center"], False)
        assert not _overlaps(a, b, pad=0.0), f"at {angle}°: {pos}"


def test_a_power_unit_makes_room_for_its_own_flags(tmp_path):
    """The Higgs supply column, which used to be a staircase of touching boxes.

    Parasit draw each chip's supply as a small box; KiCad's symbol of it is far
    bigger, so placing them where the drawing does leaves every ground pin pointing
    into the next box's body with nowhere to hang a flag. A power unit is the one
    part whose position says nothing, so it is eased apart until its rails have room,
    and the grounds come out as flags rather than as labels.
    """
    higgs = next((p for p in SAMPLES if "higgs" in p.name), None)
    if higgs is None:
        pytest.skip("no Higgs sample")
    res = build(higgs, tmp_path / "board")
    assert not [n for n in res.notes if "drawn touching" in n], res.notes
    root = sexpr.parse(res.sch.read_text())
    assert [str(lb[1]) for lb in sexpr.children(root, "label")] == []


def test_parts_left_touching_are_still_named(tmp_path):
    """The note is what is left when nothing can be eased apart, so it has to work."""
    from sch2kicad.circuit import load as load_circuit
    from sch2kicad.geometry import Layout
    from sch2kicad.parts import load_profile, resolve_all

    higgs = next((p for p in SAMPLES if "higgs" in p.name), None)
    if higgs is None:
        pytest.skip("no Higgs sample")
    lay = Layout(resolve_all(load_circuit(higgs), load_profile("tht"), SymbolResolver()))
    lay.place()
    a, b = [p for p in lay.placed if p.res.is_supply_unit][:2]
    b.origin = (a.origin[0] + 1.27, a.origin[1])
    assert lay.touching(b) and lay.touching(a)
    assert tuple(sorted((a.res.part.key, b.res.part.key))) in lay.cramped()


def test_the_notes_block_clears_everything_parked_below(tmp_path):
    """The notes go under the drawing — including under the flags parked under it."""
    higgs = next((p for p in SAMPLES if "higgs" in p.name), None)
    if higgs is None:
        pytest.skip("no Higgs sample")
    sheet = Sheet(build(higgs, tmp_path / "board").sch)
    notes = [b for b, n in sheet.texts if n == "text"]
    assert notes, "the Higgs has notes"
    for box, ref in sheet.bodies:
        assert not any(_overlaps(t, box) for t in notes), f"a note sits on {ref}"


def test_field_placement_is_measured_where_the_text_really_goes():
    """text_box measures across the page, whatever the symbol's rotation."""
    wide = text_box((0.0, 0.0), "100nF")
    assert wide[2] - wide[0] > wide[3] - wide[1]
    assert math.isclose(wide[3] - wide[1], 1.7)


def _wires(root):
    out = []
    for item in sexpr.children(root, "wire"):
        xy = [c for c in (sexpr.child(item, "pts") or [""])[1:] if sexpr.head(c) == "xy"]
        if len(xy) == 2:
            out.append(((round(_num(xy[0][1]), 3), round(_num(xy[0][2]), 3)),
                        (round(_num(xy[1][1]), 3), round(_num(xy[1][2]), 3))))
    return out


def _along(a, b):
    """The two wires lie on one line and share more than a point."""
    for i in (0, 1):
        j = 1 - i
        if a[0][i] == a[1][i] == b[0][i] == b[1][i]:
            lo, hi = sorted((a[0][j], a[1][j]))
            lo2, hi2 = sorted((b[0][j], b[1][j]))
            return min(hi, hi2) - max(lo, lo2) > 1e-6
    return False


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_no_wire_is_drawn_over_another(tmp_path, circuit):
    """One wire on top of another is invisible on the sheet and wrong in the file.

    It happened where a flag's stub stepped aside and came back on the same axis: two
    wires of one net down the same line, the netlist none the wiser, and the drawing
    showing a single stroke. It only surfaced when a rail's wires started going through
    normalise(), which keeps one of the pair and leaves the other's far end dangling.
    """
    root = sexpr.parse(build(circuit, tmp_path / "board").sch.read_text())
    w = _wires(root)
    doubled = [(a, b) for i, a in enumerate(w) for b in w[i + 1:] if _along(a, b)]
    assert doubled == []


# The places a label is not a regression. Escobedo draws the Phuncgnosis 569 px
# wide and stacks three LEDs in 43 px of it, which is less room than three KiCad LED
# symbols take however they are placed — the build says so itself ("drawn touching").
# Spreading the pins until the wire fits was tried: it clears the label and costs two
# false disagreements in the drawing cross-check, because the pins no longer sit on
# the ink. A reading that is true about where the parts are is worth more than a wire.
# The Abductor II is the same trade on another part: Eagle's PT2399 runs its pins
# 1 5 6 2 7 8 3 4 down one side and KiCad's runs them in order, so each chip's VCO
# pin stands behind REF, AGND and DGND, whose wires drop straight to their caps and
# ground where the drawing puts the 2N3904 that pin 6 runs to. IC4's pins 10 and 9
# lose their cap the same way, to C41 and C42 crowding in where KiCad spaces its pins.
LABEL_IS_FAIR = {"phuncgnosis-escobedo": {"LED_M2"},
                 "abductor-ii-deadastronaut": {"IC1_VCO", "IC2_VCO", "IC3_VCO", "IC4_VCO", "IC4_OP1I"}}


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_every_pin_of_a_rail_reaches_a_flag(tmp_path, circuit):
    """No pin on these sheets settles for a net label.

    A label is always correct and always a retreat: the reader has to go hunting for
    the other end. Every sample gets a wire or a flag everywhere bar the one named
    above, so a build that falls back again is a regression and not a fact of life.
    """
    res = build(circuit, tmp_path / "board")
    root = sexpr.parse(res.sch.read_text())
    fair = LABEL_IS_FAIR.get(circuit.name.split(".")[0], set())
    assert set(res.labelled_nets) <= fair, res.labelled_nets
    assert {str(lb[1]) for lb in sexpr.children(root, "label")} <= fair


def test_two_pins_with_no_room_share_one_flag(tmp_path):
    """The Theremin's spare inverter and the 3PDT ground pad, 5 mm apart facing each other.

    Neither has anywhere to stand a symbol of its own. Joined, they are one node with
    one ground on it — which is what the drawing means and what a hand would draw.
    """
    theremin = next((p for p in SAMPLES if "theremin" in p.name), None)
    if theremin is None:
        pytest.skip("no Theremin sample")
    root = sexpr.parse(build(theremin, tmp_path / "board").sch.read_text())
    w = _wires(root)
    # where the two sit is the placer's business and moves when a pin is read afresh,
    # so the sheet is asked where they are rather than told
    u1_11 = _pin_at(root, "U1", "11")
    pad = min((p for s in w for p in s), key=lambda p: math.dist(p, u1_11) or 99)
    assert [s for s in w if u1_11 in s], "the spare inverter's ground pin is wired to nothing"
    assert [s for s in w if pad in s], "the pad's ground pin is wired to nothing"
    grounds = [s for s in sexpr.children(root, "symbol")
               if any(str(p[1]) == "Value" and str(p[2]) == "GND" for p in sexpr.children(s, "property"))]
    on_node = [g for g in grounds
               if any(math.dist((_num(sexpr.child(g, "at")[1]), _num(sexpr.child(g, "at")[2])), p) < 5.1
                      for s in w if u1_11 in s or pad in s for p in s)]
    assert len(on_node) == 1, f"{len(on_node)} ground symbols where one should serve the node"


def _flags(root):
    """Every rail symbol on the sheet as (net, x, y, angle); PWR_FLAG markers aside."""
    out = []
    for s in sexpr.children(root, "symbol"):
        lib = str(sexpr.child(s, "lib_id")[1])
        if ("Flags:" not in lib and not lib.startswith("power:")) or "PWR_FLAG" in lib:
            continue
        at = sexpr.child(s, "at")
        net = next(str(p[2]) for p in sexpr.children(s, "property") if str(p[1]) == "Value")
        out.append((net, _num(at[1]), _num(at[2]), _num(at[3]) if len(at) > 3 else 0.0))
    return out


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_every_flag_stands_upright(tmp_path, circuit):
    """A ground symbol on its head reads as anything but ground.

    Turning a flag to face its pin was the first pass's last resort, and it took it
    six times across the samples — the Tri-Vibe's VB arrow pointing down beside D3,
    two grounds apiece standing upside down on the Higgs and the Theremin. Every one
    of those pins had a flag of its own rail close enough to wire to instead. The
    one ground that lies down is a jack socket's, along its pin 2.
    """
    root = sexpr.parse(build(circuit, tmp_path / "board").sch.read_text())
    wires = []
    for item in sexpr.children(root, "wire"):
        xy = [c for c in (sexpr.child(item, "pts") or [""])[1:] if sexpr.head(c) == "xy"]
        wires.append(((_num(xy[0][1]), _num(xy[0][2])), (_num(xy[1][1]), _num(xy[1][2]))))

    def short_lead(f) -> bool:
        """A ground upside down at the end of a short sideways lead: what Lane draws
        where the spot under the lead is taken (LM386 pin 3 over pin 2's lead), and
        allowed for that alone."""
        at = (f[1], f[2])
        return f[0] == "GND" and abs(f[3]) == 180 and any(
            abs(a[1] - b[1]) < 1e-3 and math.dist(a, b) <= 3 * 2.54 + 1e-3 and
            (math.dist(a, at) < 1e-3 or math.dist(b, at) < 1e-3) for a, b in wires)
    jacks = [(_num(at[1]), _num(at[2])) for sym in sexpr.children(root, "symbol")
             if "Socket_Jack" in str((sexpr.child(sym, "lib_id") or ["", ""])[1])
             for at in [sexpr.child(sym, "at")]]

    def jack_ground(f) -> bool:
        """A jack socket's ground lying along its pin 2 at the end of a short lead: how
        Lane draws JIN and JOUT (Bulgarian Fuzz, Memory Lapse), and allowed for that alone."""
        at = (f[1], f[2])
        return f[0] == "GND" and abs(f[3]) in (90, 270) and any(
            abs(a[1] - b[1]) < 1e-3 and math.dist(a, b) <= 2 * 2.54 + 1e-3 and
            any(math.dist(p, at) < 1e-3 for p in (a, b)) and
            any(math.dist(p, j) <= 8 for p in (a, b) for j in jacks) for a, b in wires)
    assert [f for f in _flags(root) if f[3] != 0 and not short_lead(f) and not jack_ground(f)] == []


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_no_rail_is_flagged_twice_in_one_place(tmp_path, circuit):
    """Two symbols of one rail a few millimetres apart say one node twice.

    Pins that close already share a flag; this is the case that grouping by pin
    missed, where the pins are farther apart and a dog-leg brings their flags
    together — twelve pairs across the samples, the Gargler's VR pair 2.54 mm apart.
    """
    from sch2kicad.build import BASELINE_MM, SHARE_MM
    fl = _flags(sexpr.parse(build(circuit, tmp_path / "board").sch.read_text()))

    def baseline(a, b) -> bool:
        """Level and two flag widths apart: a row of grounds, as Chuck D. Bones and
        Lane both draw them (Lane's C3 and C2 under PT2399, ~5 mm apart)."""
        return abs(a[2] - b[2]) < 1e-3 and math.dist(a[1:3], b[1:3]) >= BASELINE_MM - 1e-3
    close = [(a, b) for i, a in enumerate(fl) for b in fl[i + 1:]
             if a[0] == b[0] and math.dist(a[1:3], b[1:3]) < SHARE_MM - 1e-3 and not baseline(a, b)]
    assert close == []
