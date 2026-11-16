"""What the rest of the program asks of a PDF page, read with pdfium (pypdfium2).

Everything here is in page points with the origin at the top left of the page as
it is shown: x to the right, y down, the crop box's corner at (0, 0) and the
page's own /Rotate already applied. pdfium itself works from the bottom left of
the unrotated page; `Page._shown` is the one place that turns one into the other.

pdfium hands a path back as the bare moves, lines and curves the file wrote.
`Page.drawings` gives them the shape the tracer was written against: a path is a
list of items — ``("l", a, b)`` a line, ``("c", a, c1, c2, b)`` a curve,
``("re", [four corners])`` a rectangle, ``("qu", [four corners])`` any other
four-sided closed figure — with the strokes that go nowhere left out.
"""
from __future__ import annotations

import ctypes
import math
from pathlib import Path
from typing import Iterator, NamedTuple

import pypdfium2 as pdfium
import pypdfium2.raw as C
from PIL import Image


class Pt(NamedTuple):
    x: float
    y: float


Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_GAP = 0.8          # of the font size: a wider gap along a line starts a new piece of text


def _then(m: Matrix, n: Matrix) -> Matrix:
    """``m`` applied first, then ``n``."""
    a, b, c, d, e, f = m
    A, B, C_, D, E, F = n
    return (a * A + b * C_, a * B + b * D, c * A + d * C_, c * B + d * D,
            e * A + f * C_ + E, e * B + f * D + F)


def _matrix(obj) -> Matrix:
    m = C.FS_MATRIX()
    C.FPDFPageObj_GetMatrix(obj, m)
    return (m.a, m.b, m.c, m.d, m.e, m.f)


def _near(p: Pt, q: Pt) -> bool:
    return abs(p.x - q.x) < 1e-3 and abs(p.y - q.y) < 1e-3


def _square(q: list[Pt]) -> bool:
    """Four corners whose every side runs along an axis."""
    return all(abs(q[i].x - q[(i + 1) % 4].x) < 1e-3 or abs(q[i].y - q[(i + 1) % 4].y) < 1e-3
               for i in range(4))


class Document:
    def __init__(self, path: Path | str):
        self._doc = pdfium.PdfDocument(str(path))

    def __len__(self) -> int:
        return len(self._doc)

    def __getitem__(self, i: int) -> "Page":
        return Page(self._doc[i])

    def __iter__(self) -> Iterator["Page"]:
        return (self[i] for i in range(len(self)))

    def close(self) -> None:
        self._doc.close()

    def __enter__(self) -> "Document":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open(path: Path | str) -> Document:          # noqa: A001 - reads as pdf.open(...)
    return Document(path)


class Page:
    def __init__(self, page):
        self._page = page
        self._x0, self._y0, self._x1, self._y1 = page.get_cropbox()
        self._rot = page.get_rotation()
        self.width, self.height = page.get_width(), page.get_height()     # as shown
        self._chars: list[dict] | None = None

    # ------------------------------------------------------------- geometry

    def _shown(self, x: float, y: float) -> Pt:
        """A point of pdfium's page space, where it is on the page as shown."""
        w, h = self._x1 - self._x0, self._y1 - self._y0
        x, y = x - self._x0, self._y1 - y
        if self._rot == 90:
            return Pt(h - y, x)
        if self._rot == 180:
            return Pt(w - x, h - y)
        if self._rot == 270:
            return Pt(y, w - x)
        return Pt(x, y)

    def _box(self, left: float, bottom: float, right: float, top: float) -> tuple[float, float, float, float]:
        a, b = self._shown(left, bottom), self._shown(right, top)
        return (min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))

    def _objects(self, kind: int) -> Iterator[tuple[object, Matrix]]:
        """Every page object of one kind with the matrix that places it on the page,
        the ones inside form XObjects included."""
        def walk(parent, count, get, ctm, depth):
            for i in range(count(parent)):
                obj = get(parent, i)
                t = C.FPDFPageObj_GetType(obj)
                if t == C.FPDF_PAGEOBJ_FORM and depth < 15:
                    yield from walk(obj, C.FPDFFormObj_CountObjects, C.FPDFFormObj_GetObject,
                                    _then(_matrix(obj), ctm), depth + 1)
                elif t == kind:
                    yield obj, _then(_matrix(obj), ctm)
        yield from walk(self._page, C.FPDFPage_CountObjects, C.FPDFPage_GetObject, _IDENTITY, 0)

    # --------------------------------------------------------------- paths

    def drawings(self) -> list[dict]:
        """Each filled or stroked path: ``type`` ("f", "s" or "fs"), ``items``,
        ``rect`` (x0, y0, x1, y1 round every point of it) and ``color`` (the stroke's
        r, g, b from 0 to 1, None when it is not stroked)."""
        out = []
        for obj, m in self._objects(C.FPDF_PAGEOBJ_PATH):
            fill, stroke = ctypes.c_int(), ctypes.c_int()
            C.FPDFPath_GetDrawMode(obj, fill, stroke)
            kind = ("f" if fill.value else "") + ("s" if stroke.value else "")
            if not kind:
                continue
            items, seen = self._items(obj, m)
            if not seen:
                continue
            color = None
            if stroke.value:
                r, g, b, a = (ctypes.c_uint() for _ in range(4))
                if C.FPDFPageObj_GetStrokeColor(obj, r, g, b, a):
                    color = (r.value / 255, g.value / 255, b.value / 255)
            xs, ys = [p.x for p in seen], [p.y for p in seen]
            out.append({"type": kind, "items": items, "color": color,
                        "rect": (min(xs), min(ys), max(xs), max(ys))})
        return out

    def _items(self, obj, m: Matrix) -> tuple[list[tuple], list[Pt]]:
        a, b, c, d, e, f = m
        items: list[tuple] = []
        seen: list[Pt] = []
        first = last = None
        lines = 0                   # lines in a row since the last move, curve or box
        moved = False               # the stroke before this one was a move
        handles: list[Pt] = []
        x, y = ctypes.c_float(), ctypes.c_float()

        def box() -> None:
            nonlocal lines
            q = [it[1] for it in items[-4:]]
            items[-4:] = [("re" if _square(q) else "qu", q)]
            lines = 0

        for k in range(C.FPDFPath_CountSegments(obj)):
            seg = C.FPDFPath_GetPathSegment(obj, k)
            C.FPDFPathSegment_GetPoint(seg, x, y)
            p = self._shown(a * x.value + c * y.value + e, b * x.value + d * y.value + f)
            seen.append(p)
            t = C.FPDFPathSegment_GetType(seg)
            if t == C.FPDF_SEGMENT_MOVETO:
                first = last = p
                lines, moved = 0, True
            elif t == C.FPDF_SEGMENT_LINETO:
                if p != last or moved:          # a line that goes nowhere is not drawn
                    items.append(("l", last, p))
                    last = p
                    lines += 1
                    if lines == 4 and _near(p, items[-4][1]):
                        box()                   # four lines back to where they started
                moved = False
            elif t == C.FPDF_SEGMENT_BEZIERTO:
                handles.append(p)
                if len(handles) < 3:
                    continue
                c1, c2, end = handles
                handles = []
                # a curve whose handles sit on its ends is a straight line
                if (last == c1 and (c2 == end or c1 == c2)) or (last != c1 and c1 == c2 == end):
                    if end != last or moved:
                        items.append(("l", last, end))
                        lines += 1
                else:
                    items.append(("c", last, c1, c2, end))
                    lines = 0
                last, moved = end, False
            if C.FPDFPathSegment_GetClose(seg) and first is not None:
                if lines == 3:
                    q = [items[-3][1], items[-2][1], items[-1][1], items[-1][2]]
                    if _square(q):              # three sides and a close: a rectangle
                        items[-3:] = [("re", q)]
                        lines, last = 0, first
                        continue
                if not _near(last, first):
                    items.append(("l", last, first))
                last, lines = first, 0
        return items, seen

    # --------------------------------------------------------------- images

    def images(self) -> list[dict]:
        """Each image placed on the page: its pixel size ``w`` x ``h``, the ``rect``
        it covers, and ``matrix`` — where the picture's unit square (top-left origin,
        like the page) lands. A negative determinant is a mirrored picture."""
        out = []
        for n, (obj, m) in enumerate(self._objects(C.FPDF_PAGEOBJ_IMAGE)):
            w, h = ctypes.c_uint(), ctypes.c_uint()
            if not C.FPDFImageObj_GetImagePixelSize(obj, w, h):
                continue
            a, b, c, d, e, f = m
            corners = [self._shown(a * u + c * v + e, b * u + d * v + f) for u, v in ((0, 1), (1, 1), (0, 0), (1, 0))]
            o, ux, vy = corners[0], corners[1], corners[2]       # the picture's top left, top right, bottom left
            xs, ys = [p.x for p in corners], [p.y for p in corners]
            out.append({"index": n, "w": w.value, "h": h.value,
                        "rect": (min(xs), min(ys), max(xs), max(ys)),
                        "matrix": (ux.x - o.x, ux.y - o.y, vy.x - o.x, vy.y - o.y, o.x, o.y)})
        return out

    def image(self, index: int) -> Image.Image:
        """The pixels of one placed image as the file stores them, not as the page
        turns, scales or masks them."""
        for n, (obj, _) in enumerate(self._objects(C.FPDF_PAGEOBJ_IMAGE)):
            if n != index:
                continue
            im = pdfium.PdfImage(obj, page=self._page, pdf=self._page.pdf)
            if im.get_filters() in (["DCTDecode"], ["JPXDecode"]):
                import io                       # a JPEG is read by PIL itself, as it always was
                try:
                    pic = Image.open(io.BytesIO(bytes(im.get_data(decode_simple=True))))
                    pic.load()
                    return pic
                except Exception:               # noqa: BLE001 - fall back on pdfium's decoder
                    pass
            return im.get_bitmap(render=False).to_pil()
        raise IndexError(index)

    # ---------------------------------------------------------------- render

    def render(self, dpi: float, clip: tuple[float, float, float, float] | None = None) -> Image.Image:
        """The page as an RGB picture on white; ``clip`` (x0, y0, x1, y1) is the part wanted."""
        crop = (0, 0, 0, 0)
        if clip:
            x0, y0, x1, y1 = (max(0.0, clip[0]), max(0.0, clip[1]),
                              min(self.width, clip[2]), min(self.height, clip[3]))
            crop = (x0, self.height - y1, self.width - x1, y0)       # left, bottom, right, top
        scale = dpi / 72
        # pdfium rounds a size up, so 792 pt at 300 dpi (3300.00003 in floats) would
        # come out 3301 px wide: ask for a hair less when the size is that close
        for side in (self.width - crop[0] - crop[2], self.height - crop[1] - crop[3]):
            if math.ceil(side * scale) > round(side * scale) and side * scale - round(side * scale) < 1e-3:
                scale = min(scale, (round(side * dpi / 72) - 1e-4) / side)
        return self._page.render(scale=scale, crop=crop).to_pil().convert("RGB")

    # ------------------------------------------------------------------ text

    def text(self) -> str:
        tp = self._page.get_textpage()
        try:
            return tp.get_text_range()
        finally:
            tp.close()

    def _letters(self) -> list[dict]:
        """Every character pdfium reads off the page, in reading order, with its box."""
        if self._chars is not None:
            return self._chars
        tp = self._page.get_textpage()
        out = []
        try:
            for i in range(tp.count_chars()):
                ch = chr(C.FPDFText_GetUnicode(tp, i))
                made = bool(C.FPDFText_IsGenerated(tp, i))       # a space or break pdfium put in
                rec = {"ch": ch, "made": made, "box": None, "size": 0.0, "dir": (1.0, 0.0), "bold": False}
                if not made:
                    left, bottom, right, top = tp.get_charbox(i, loose=True)
                    rec["box"] = self._box(left, bottom, right, top)
                    m = C.FS_MATRIX()               # the font size is in the text's own units
                    grow = math.hypot(m.c, m.d) if C.FPDFText_GetMatrix(tp, i, m) else 1.0
                    rec["size"] = C.FPDFText_GetFontSize(tp, i) * (grow or 1.0)
                    ang = C.FPDFText_GetCharAngle(tp, i)        # clockwise as the page is seen
                    if ang >= 0:
                        rec["dir"] = self._turned(math.cos(ang), math.sin(ang))
                    rec["bold"] = self._bold(tp, i)
                out.append(rec)
        finally:
            tp.close()
        self._chars = out
        return out

    def _turned(self, dx: float, dy: float) -> tuple[float, float]:
        """A direction on the unrotated page, as it runs on the page as shown."""
        for _ in range(self._rot // 90):
            dx, dy = -dy, dx
        return (round(dx, 6) + 0.0, round(dy, 6) + 0.0)

    @staticmethod
    def _bold(tp, i: int) -> bool:
        """Whether a character is set in a bold face: by the font's weight, its
        force-bold flag or, as most files leave both unset, its name."""
        if C.FPDFText_GetFontWeight(tp, i) >= 600:
            return True
        flags = ctypes.c_int()
        n = C.FPDFText_GetFontInfo(tp, i, None, 0, flags)
        if flags.value & 0x40000:
            return True
        if n <= 0:
            return False
        buf = ctypes.create_string_buffer(n)
        C.FPDFText_GetFontInfo(tp, i, buf, n, flags)
        return "bold" in buf.value.decode("utf-8", "replace").lower()

    def words(self) -> list[tuple[float, float, float, float, str, bool]]:
        """(x0, y0, x1, y1, word, bold) for each run of characters between blanks."""
        out, run = [], []

        def flush() -> None:
            if run:
                out.append((min(c["box"][0] for c in run), min(c["box"][1] for c in run),
                            max(c["box"][2] for c in run), max(c["box"][3] for c in run),
                            "".join(c["ch"] for c in run), all(c["bold"] for c in run)))
                run.clear()
        for c in self._letters():
            if c["made"] or c["ch"].isspace() or c["box"] is None:
                flush()
            else:
                run.append(c)
        flush()
        return out

    def spans(self) -> list[dict]:
        """Pieces of text that read as one: ``text``, ``bbox`` (x0, y0, x1, y1) and
        ``dir``, the way the writing runs ((1, 0) left to right, (0, -1) up the page).
        A line breaks one, and so does a gap wider than a word space."""
        out, run = [], []

        def flush() -> None:
            while run and run[-1]["box"] is None:
                run.pop()
            if run:
                boxes = [c["box"] for c in run if c["box"]]
                out.append({"text": "".join(c["ch"] for c in run),
                            "bbox": (min(b[0] for b in boxes), min(b[1] for b in boxes),
                                     max(b[2] for b in boxes), max(b[3] for b in boxes)),
                            "dir": next(c["dir"] for c in run if c["box"])})
            run.clear()
        prev = None
        for c in self._letters():
            if c["ch"] in "\r\n":
                flush()
                prev = None
                continue
            if c["box"] is None:                # a space pdfium made up for a gap
                if run:
                    run.append({**c, "ch": " "})
                continue
            if prev is not None and run:
                turn = abs(c["dir"][0] - prev["dir"][0]) + abs(c["dir"][1] - prev["dir"][1])
                size = max(c["size"], prev["size"], 1e-6)
                gap, off = self._gap(prev, c)
                # the next letter follows on the same line, or it is something else:
                # too far on, back before the last one, or off to one side
                if turn > 0.02 or gap > _GAP * size or gap < -1.5 * size or off > 0.7 * size:
                    flush()
            run.append(c)
            prev = c
        flush()
        return self._joined(out)

    @staticmethod
    def _joined(spans: list[dict]) -> list[dict]:
        """Pieces that touch end to start on one line are one piece. A file can
        write the letters of its names and then their digits (Aion FX's Tri-Vibe:
        "D", "R", then "4", "32"), and pdfium reads them in the order written."""
        size = lambda sp: min(sp["bbox"][2] - sp["bbox"][0], sp["bbox"][3] - sp["bbox"][1]) if abs(sp["dir"][0]) < abs(sp["dir"][1]) \
            else sp["bbox"][3] - sp["bbox"][1]
        out = [dict(sp, size=sp.pop("_size", None) or size(sp)) for sp in spans]
        merged = True
        while merged:
            merged = False
            for i, a in enumerate(out):
                for j, b in enumerate(out):
                    if i == j or a["dir"] != b["dir"] or abs(a["size"] - b["size"]) > 0.15 * a["size"]:
                        continue
                    gap, off = Page._gap({"dir": a["dir"], "box": a["bbox"]}, {"dir": b["dir"], "box": b["bbox"]})
                    if -0.15 * a["size"] <= gap <= 0.12 * a["size"] and off <= 0.15 * a["size"]:
                        x0, y0, x1, y1 = a["bbox"]
                        u0, v0, u1, v1 = b["bbox"]
                        a.update(text=a["text"] + b["text"], bbox=(min(x0, u0), min(y0, v0), max(x1, u1), max(y1, v1)))
                        del out[j]
                        merged = True
                        break
                if merged:
                    break
        for sp in out:
            del sp["size"]
        return out

    @staticmethod
    def _gap(a: dict, b: dict) -> tuple[float, float]:
        """From one character to the next: the clear space along the writing, and
        how far the second sits to one side of the first."""
        dx, dy = a["dir"]
        (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = a["box"], b["box"]
        if abs(dx) >= abs(dy):
            side = abs((ay0 + ay1) - (by0 + by1)) / 2
            return ((bx0 - ax1) if dx > 0 else (ax0 - bx1)), side
        side = abs((ax0 + ax1) - (bx0 + bx1)) / 2
        return ((by0 - ay1) if dy > 0 else (ay0 - by1)), side
