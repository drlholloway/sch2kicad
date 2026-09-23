"""Fast tests: no KiCad needed."""

import pytest

from sch2kicad import sexpr
from sch2kicad.circuit import parse_value
from sch2kicad.geometry import Orient, apply, matrix
from sch2kicad.parts import canonical_pin, lookup_pinout, pinout_assumption
from sch2kicad.router import normalise


def test_sexpr_round_trip():
    text = '(kicad_sch (version 20260306) (paper "A4") (text "a \\"quoted\\" word\\nnext" (at 1.27 -2.54 0)))'
    node = sexpr.parse(text)
    assert node[0] == "kicad_sch"
    t = sexpr.child(node, "text")
    assert t[1] == 'a "quoted" word\nnext'
    again = sexpr.parse(sexpr.dumps(node))
    assert again == node


def test_number_format():
    assert sexpr.fmt_num(1.27) == "1.27"
    assert sexpr.fmt_num(-0.0) == "0"
    assert sexpr.fmt_num(59.690000001) == "59.69"


def test_orientation_matches_kicad():
    # library Y is up, sheet Y is down
    assert apply(matrix(0), 3.81, 0) == (3.81, 0)
    assert apply(matrix(0), 0, 3.81) == (0, -3.81)
    # 90 rotates counter-clockwise on screen: a pin on the right ends up on top
    assert apply(matrix(90), 3.81, 0) == (0, -3.81)
    assert apply(matrix(270), 3.81, 0) == (0, 3.81)
    # mirror y flips left/right, mirror x flips up/down
    assert apply(matrix(0, "y"), 3.81, 0) == (-3.81, 0)
    assert apply(matrix(0, "x"), 0, 3.81) == (0, 3.81)
    assert len({Orient(a, m).m for a in (0, 90, 180, 270) for m in (None, "x", "y")}) == 8


def test_values():
    approx = pytest.approx
    assert parse_value("4k7") == approx(4700)
    assert parse_value("2M2") == approx(2.2e6)
    assert parse_value("3.3nF") == approx(3.3e-9)
    assert parse_value("100uF") == approx(100e-6)
    assert parse_value("A100K") == approx(100e3)
    assert parse_value("100kB") == approx(100e3)
    assert parse_value("10") == approx(10)
    assert parse_value("4n7") == approx(4.7e-9)
    assert parse_value("TL072") is None


def test_pinout_table():
    assert lookup_pinout("npn", "2N5089") == ("EBC", None)
    assert lookup_pinout("npn", "BC549C") == ("CBE", None)
    assert lookup_pinout("nmos", "BS170") == ("DGS", None)
    # 2N2222 ships both ways; through-hole builds assume E-B-C, and say so
    assert lookup_pinout("npn", "2N2222") == ("EBC", None)
    assert "C-B-E" in pinout_assumption("npn", "2N2222A")
    assert lookup_pinout("npn", "P2N2222A") == ("CBE", None)
    assert lookup_pinout("npn", "MPS2222") == ("EBC", None)
    assert lookup_pinout("npn", "MPS2222A") == ("EBC", None)
    assert pinout_assumption("npn", "2N5089") is None
    order, why = lookup_pinout("npn", "MP38A")
    assert order is None and why
    assert lookup_pinout("npn", "XYZ123") == (None, None)


def test_pin_synonyms():
    assert canonical_pin("pot", "1") == "ccw"
    assert canonical_pin("pot", "CW") == "cw"
    assert canonical_pin("capacitor_polarized", "pos") == "+"
    assert canonical_pin("diode", "k") == "K"


def test_the_ota_bias_pin_leaves_from_the_bottom():
    """Why the tool ships its own LM13700 at all.

    KiCad's brings the amplifier bias current (pins 1 and 16) out on the right, a
    step below the output, so the wire from the LFO has to cross the signal it is
    meant to control. Ours takes it out of the base of the triangle, which is where
    the datasheet and every pedal schematic put it.
    """
    from pathlib import Path

    from sch2kicad.symlib import _parse_pins

    lib = Path(__file__).resolve().parent.parent / "src" / "sch2kicad" / "data" / "symbols" / "sch2kicad.kicad_sym"
    node = next(s for s in sexpr.children(sexpr.parse(lib.read_text()), "symbol") if str(s[1]) == "LM13700")
    pins = {p.number: p for p in _parse_pins(node)}
    for bias, out, plus, minus in (("1", "5", "3", "4"), ("16", "12", "14", "13")):
        assert pins[bias].outward() == (0, -1), f"pin {bias} should leave from the bottom"
        assert pins[out].outward() == (1, 0), f"pin {out} should leave to the right"
        assert pins[plus].outward() == pins[minus].outward() == (-1, 0)
    # the drawing's IC4A is pins 1-5, and on this symbol that really is unit A
    assert pins["1"].unit == 1 and pins["12"].unit == 2
    assert pins["6"].name == "V-" and pins["11"].name == "V+"


def test_normalise_splits_t_joins():
    # a horizontal run with a wire ending on its middle: split + one junction
    segs = [((0.0, 0.0), (5.08, 0.0)), ((2.54, 0.0), (2.54, 2.54))]
    out, juncs = normalise(segs, pins=[(0.0, 0.0), (5.08, 0.0), (2.54, 2.54)])
    assert len(out) == 3
    assert juncs == [(2.54, 0.0)]
