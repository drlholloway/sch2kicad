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


def test_a_note_names_the_parts_drawn_touching(tmp_path):
    """Where the drawing packs symbols tighter than KiCad's can go, say which."""
    higgs = next((p for p in SAMPLES if "higgs" in p.name), None)
    if higgs is None:
        pytest.skip("no Higgs sample")
    res = build(higgs, tmp_path / "board")
    touching = [n for n in res.notes if "drawn touching" in n]
    assert touching, res.notes
    assert "Pull them apart in KiCad" in touching[0]


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
