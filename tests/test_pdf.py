"""The PDF reader gives every caller one page space: points from the top left of the
page as it is shown. These pages are turned, cropped and nested to prove it, each
checked against the picture pdfium itself draws of the same page."""
import numpy as np
import pdfs
import pytest

from sch2kicad import pdf


def _ink(page, dpi=72):
    return np.asarray(page.render(dpi).convert("L")) < 128


def _lines(page):
    return [(it[1], it[2]) for d in page.drawings() for it in d["items"] if it[0] == "l"]


# one thick stroke, off center so that every turn and shift moves it somewhere else
STROKE = "0 0 0 RG 4 w 20 150 m 120 150 l S"


@pytest.mark.parametrize("extra, size", [
    ("", (200, 300)),
    ("/Rotate 90", (300, 200)),
    ("/Rotate 180", (200, 300)),
    ("/Rotate 270", (300, 200)),
    ("/CropBox [10 40 190 280]", (180, 240)),
    ("/CropBox [10 40 190 280] /Rotate 90", (240, 180)),
])
def test_a_stroke_is_read_where_the_page_shows_it(tmp_path, extra, size):
    doc = pdf.open(pdfs.write(tmp_path / "p.pdf", 200, 300, STROKE, page=extra))
    page = doc[0]
    assert (round(page.width), round(page.height)) == size
    ink = _ink(page)
    assert ink.shape == (size[1], size[0])
    [(a, b)] = _lines(page)
    for t in (0.1, 0.5, 0.9):                       # along the stroke: ink
        x, y = a.x + t * (b.x - a.x), a.y + t * (b.y - a.y)
        assert ink[int(y), int(x)], (extra, a, b)
    assert ink.sum() == pytest.approx(100 * 4, rel=0.1)     # and nowhere else
    doc.close()


def test_a_stroke_inside_nested_forms_is_placed_by_every_matrix_on_the_way(tmp_path):
    inner = pdfs.stream("/Type /XObject /Subtype /Form /BBox [0 0 200 300] /Matrix [1 0 0 1 10 0]", STROKE.encode())
    outer = pdfs.stream("/Type /XObject /Subtype /Form /BBox [0 0 400 600] "
                        "/Resources << /XObject << /In 4 0 R >> >>", b"q 0.5 0 0 0.5 0 20 cm /In Do Q")
    doc = pdf.open(pdfs.write(tmp_path / "p.pdf", 200, 300, "q 1 0 0 1 30 0 cm /Out Do Q",
                              xobjects={"In": inner, "Out": outer}))
    page = doc[0]
    [(a, b)] = _lines(page)
    # x: (20 + 10) / 2 + 30 = 45 to (120 + 10) / 2 + 30 = 95; y: 300 - (150 / 2 + 20) = 205
    assert (a.x, a.y, b.x, b.y) == pytest.approx((45, 205, 95, 205))
    assert _ink(page)[205, 50:90].all()
    doc.close()


def test_a_clip_crops_the_render_to_the_part_asked_for(tmp_path):
    page = pdf.open(pdfs.write(tmp_path / "p.pdf", 200, 300, STROKE))[0]
    img = page.render(72, clip=(10, 140, 130, 160))
    assert img.size == (120, 20)
    ink = np.asarray(img.convert("L")) < 128
    assert ink[10, 10:110].all() and not ink[:, :8].any() and not ink[:, 112:].any()


def test_a_page_of_300_dpi_is_not_a_pixel_too_wide(tmp_path):
    """792 pt at 300 dpi is 3300 px; in floats it is a shade more, and pdfium rounds up."""
    page = pdf.open(pdfs.write(tmp_path / "p.pdf", 792, 612, STROKE))[0]
    assert page.render(300).size == (3300, 2550)


def test_shapes_come_back_as_the_tracer_names_them(tmp_path):
    content = ("0 0 0 RG 10 10 50 30 re S "                              # a rectangle
               "100 10 m 150 10 l 160 40 l 110 40 l h S "               # a slanted box
               "10 100 m 60 100 l 60 100 l S "                          # a line, then one that goes nowhere
               "0 0 0 rg 100 100 m 100 105 105 110 110 110 c 115 110 120 105 120 100 c f")
    kinds = [[it[0] for it in d["items"]] for d in pdf.open(pdfs.write(tmp_path / "p.pdf", 200, 200, content))[0].drawings()]
    assert kinds == [["re"], ["qu"], ["l"], ["c", "c"]]


FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
BOLD = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"


def _text_page(tmp_path, content, extra=""):
    path = pdfs.write(tmp_path / "t.pdf", 300, 200, content, page=extra, xobjects={"F1": FONT, "F2": BOLD})
    raw = path.read_bytes().replace(b"/XObject <<", b"/Font <<", 1)       # the two fonts are resources, not XObjects
    path.write_bytes(raw)
    return pdf.open(path)[0]


def test_words_have_their_boxes_and_say_which_are_bold(tmp_path):
    page = _text_page(tmp_path, "BT /F2 12 Tf 20 150 Td (Resistors) Tj ET BT /F1 12 Tf 20 130 Td (R1 10K) Tj ET")
    words = [(w[4], w[5]) for w in page.words()]
    assert words == [("Resistors", True), ("R1", False), ("10K", False)]
    x0, y0, x1, y1 = page.words()[1][:4]
    assert 19 <= x0 <= 21 and 55 <= (y0 + y1) / 2 <= 70 and x1 < 40


def test_text_far_apart_on_one_line_is_two_pieces_and_turned_text_says_which_way(tmp_path):
    page = _text_page(tmp_path, "BT /F1 12 Tf 20 150 Td (R1 10K) Tj 150 0 Td (C3) Tj ET "
                                "BT /F1 12 Tf 0 1 -1 0 250 40 Tm (UP) Tj ET")
    got = {s["text"]: s["dir"] for s in page.spans()}
    assert got == {"R1 10K": (1.0, 0.0), "C3": (1.0, 0.0), "UP": (0.0, -1.0)}
