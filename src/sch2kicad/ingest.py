"""Prepare a schematic image or build-doc PDF for reading.

Writes a work directory next to the input::

    source.png      the schematic, upright, flattened onto white
    grid.png        whole image with a pixel grid (every 50 px, labelled per 100)
    tiles/*.png     overlapping zoomed crops with the same grid — read these
    ocr.json        words with pixel boxes (tesseract, both orientations)
    text.json       exact text with boxes, when the PDF page is vector
    dots.json       junction dots found by shape (filled circles)
    bom.json        the build doc's parts list, when it has one as a text table
    pages/          thumbnails of every PDF page (with --list or no --page)
    circuit.json    a skeleton with the source filled in (never overwritten)

All coordinates are pixels of source.png, origin top-left, y down — the same
frame circuit.json uses.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import bom
from .circuit import SCHEMA


def _font(size: int):
    for name in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
                 "/Library/Fonts/Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def flatten(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg
    return img.convert("RGB")


# --------------------------------------------------------------------- PDFs

def pdf_pages(pdf: Path, out: Path) -> list[dict]:
    import pymupdf
    doc = pymupdf.open(pdf)
    rows = []
    (out / "pages").mkdir(parents=True, exist_ok=True)
    for i, page in enumerate(doc, start=1):
        text = page.get_text()
        words = text.split()
        refs = sum(1 for w in words if len(w) <= 5 and w[:1] in "RCQDU" and w[1:].isdigit())
        imgs = []
        for im in page.get_images(full=True):
            rects = page.get_image_rects(im[0])
            if rects:
                r = rects[0]
                imgs.append({"xref": im[0], "w": im[2], "h": im[3], "area": round(r.width * r.height / (page.rect.width * page.rect.height), 2)})
        drawings = len(page.get_drawings())
        thumb = out / "pages" / f"page-{i:02d}.png"
        page.get_pixmap(dpi=40).save(thumb)
        big = [im for im in imgs if im["w"] * im["h"] >= 600_000]
        score = 0.0
        if big:
            top = max(big, key=lambda im: im["area"])
            score += 100 * top["area"] + 5 * max(0.0, __import__("math").log2(top["w"] * top["h"] / 1e6))
        if drawings >= 300:                       # a vector drawing, not a table
            score += min(drawings, 3000) / 30 + refs
        if "schematic" in text.lower():
            score += 30
        rows.append({"page": i, "words": len(words), "refs": refs, "drawings": drawings, "images": imgs,
                     "thumb": str(thumb), "score": round(score, 1)})
    return rows


def pdf_schematic(pdf: Path, page_no: int, out: Path) -> tuple[Image.Image, dict, list[dict]]:
    """The schematic on a page: the embedded image at native size if one dominates, else a render."""
    import pymupdf
    doc = pymupdf.open(pdf)
    page = doc[page_no - 1]
    best = None
    for im in page.get_images(full=True):
        for rect, m in page.get_image_rects(im[0], transform=True):
            area = rect.width * rect.height / (page.rect.width * page.rect.height)
            if im[2] * im[3] > 400_000 and (best is None or area > best[0]):
                best = (area, im[0], m)
    meta = {"pdf": str(pdf), "page": page_no}
    text: list[dict] = []
    if best and best[0] > 0.25:
        _, xref, m = best
        info = doc.extract_image(xref)
        img = flatten(Image.open(__import__("io").BytesIO(info["image"])))
        # the placement matrix only says how the image sits on the page (often turned
        # to fit a portrait page); the pixels themselves are what the author drew
        meta.update({"kind": "embedded-image", "xref": xref,
                     "matrix": [m.a, m.b, m.c, m.d, m.e, m.f]})
        return img, meta, text
    # vector page: render it, and keep its text with exact boxes
    dpi = 300
    pix = page.get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    k = dpi / 72
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for span in line.get("spans", []):
                if span["text"].strip():
                    x0, y0, x1, y1 = span["bbox"]
                    text.append({"text": span["text"].strip(), "box": [round(x0 * k), round(y0 * k), round(x1 * k), round(y1 * k)],
                                 "dir": [round(v, 2) for v in line.get("dir", (1, 0))]})
    meta.update({"kind": "vector", "dpi": dpi})
    return img, meta, text


# ---------------------------------------------------------------- features

def page_ink(img: Image.Image) -> np.ndarray:
    """The page as depth of ink: 0 where the drawing is darkest, 255 where it is bare.

    Not the same as its brightness. How far a pixel stands off the page is how far
    it goes in whichever channel goes furthest, so a saturated green wire counts as
    deeply as a black one; by brightness it works out at 163 of 255 and reads as
    bare paper, which is how a sheet drawn in colour came to have no junction dots
    at all. On anything grey this is the brightness it always was.
    """
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)
    return (255 - (255 - rgb).max(axis=2)).astype(np.uint8)


def line_width(gray: np.ndarray) -> float:
    """Typical stroke width in pixels (median horizontal run of ink)."""
    ink = gray < 128
    runs = []
    for row in ink[:: max(1, ink.shape[0] // 200)]:
        r = 0
        for v in row:
            if v:
                r += 1
            elif r:
                runs.append(r)
                r = 0
    runs = [r for r in runs if r < 12]
    return float(np.median(runs)) if runs else 1.0


def find_dots(img: Image.Image) -> list[dict]:
    """Filled circles noticeably thicker than the strokes: junction dots.

    A dot's inscribed radius is ~3× the line width; text and wires never get
    there, so thresholding the distance transform isolates dots cleanly.
    """
    import cv2
    gray = page_ink(img)
    lw = line_width(gray)
    ink = (gray < 150).astype(np.uint8)
    dist = cv2.distanceTransform(ink, cv2.DIST_L2, 5)
    r_min = max(2.0, 1.6 * lw)
    core = (dist >= r_min).astype(np.uint8)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(core, 8)
    dots = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w > 4 * r_min or h > 4 * r_min:
            continue            # a thick bar or filled shape, not a dot
        cx, cy = cents[i]
        r = float(dist[labels == i].max())
        # the blob around the core must be round: sample the ink radius in 8 directions
        radii = []
        for ang in range(0, 360, 45):
            dx, dy = np.cos(np.radians(ang)), np.sin(np.radians(ang))
            k = 0.0
            while k < 6 * r_min:
                px, py = int(round(cx + dx * k)), int(round(cy + dy * k))
                if not (0 <= px < ink.shape[1] and 0 <= py < ink.shape[0]) or not ink[py, px]:
                    break
                k += 0.5
            radii.append(k)
        radii.sort()
        # wires leave through up to four directions; the other half must be the dot's edge
        edge = radii[:4]
        if max(edge) - min(edge) > max(1.5, 0.45 * r) or min(edge) < r_min:
            continue
        dots.append({"x": round(float(cx), 1), "y": round(float(cy), 1), "d": round(2 * float(np.median(edge)), 1)})
    if dots:
        # a drawing uses one dot size; smaller round blobs are bold lettering and arrowheads
        ds = sorted(d["d"] for d in dots)
        big = ds[int(0.9 * (len(ds) - 1))]
        dots = [d for d in dots if d["d"] >= 0.8 * big]
    return dots


_TOKEN = __import__("re").compile(r"^(?:[RCQDULPJ]\d{1,3}|VR\d{1,2}|IC\d|\d+(?:\.\d+)?[pnuµmkKMR]\d*[FH]?)$")


def detect_rotation(img: Image.Image, work: Path) -> int:
    """Clockwise degrees to turn the image so its text reads left to right."""
    exe = shutil.which("tesseract")
    if not exe:
        return 0
    scale = 2 if img.width < 2500 and img.height < 2500 else 1
    g = img.convert("L")
    g = g.resize((g.width * scale, g.height * scale), Image.Resampling.LANCZOS) if scale > 1 else g
    best, best_n = 0, -1
    for rot in (0, 90, 270, 180):
        im = g if rot == 0 else g.rotate(-rot, expand=True)
        p = work / "_orient.png"
        im.save(p)
        r = subprocess.run([exe, str(p), "stdout", "--psm", "11", "tsv"], capture_output=True, text=True)
        p.unlink(missing_ok=True)
        n = 0
        for ln in r.stdout.splitlines()[1:]:
            f = ln.split("\t")
            if len(f) >= 12 and f[11].strip() and float(f[10]) >= 70 and _TOKEN.match(f[11].strip()):
                n += 1
        if n > best_n * 1.5 + 2:   # only leave 0° for a clear win
            best, best_n = rot, n
        if rot == 0 and n >= 25:
            break                  # plenty of upright text: no need to try the rest
    return best


def ocr(img: Image.Image, work: Path) -> list[dict]:
    exe = shutil.which("tesseract")
    if not exe:
        return []
    words: list[dict] = []
    scale = 3 if img.width < 2200 else 2 if img.width < 3500 else 1
    big = img.convert("L").resize((img.width * scale, img.height * scale), Image.Resampling.LANCZOS)
    for rot in (0, 90):
        im = big if rot == 0 else big.rotate(-90, expand=True)   # vertical text reads upward
        p = work / f"_ocr{rot}.png"
        im.save(p)
        r = subprocess.run([exe, str(p), "stdout", "--psm", "11", "tsv"], capture_output=True, text=True)
        p.unlink(missing_ok=True)
        lines = r.stdout.splitlines()[1:]
        for ln in lines:
            f = ln.split("\t")
            if len(f) < 12 or not f[11].strip():
                continue
            conf = float(f[10])
            if conf < 55:
                continue
            x, y, w, h = (int(v) for v in f[6:10])
            if rot == 90:
                # rotated -90 (clockwise): (x, y) in rotated → (y', W - x') in original
                ox0, oy0 = y, big.height - (x + w)
                ow, oh = h, w
                x, y, w, h = ox0, oy0, ow, oh
            words.append({"text": f[11].strip(), "conf": round(conf), "rot": rot,
                          "box": [round(x / scale), round(y / scale), round((x + w) / scale), round((y + h) / scale)]})
    return words


# ------------------------------------------------------------------- grids

def draw_grid(img: Image.Image, step: int = 50, label_every: int = 100, zoom: float = 1.0,
              offset: tuple[int, int] = (0, 0), minor: int = 0) -> Image.Image:
    """Grid lines at source-pixel multiples; labels show source pixel coordinates."""
    base = img.convert("RGBA")
    over = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    ox, oy = offset
    f = _font(max(10, int(11 * max(zoom, 1) * 0.9)))
    W, H = base.size

    def xs(s):
        first = (ox // s + 1) * s if ox % s else ox
        v = first
        while (v - ox) * zoom <= W:
            yield v
            v += s

    def ys(s):
        first = (oy // s + 1) * s if oy % s else oy
        v = first
        while (v - oy) * zoom <= H:
            yield v
            v += s

    if minor:
        for v in xs(minor):
            X = (v - ox) * zoom
            d.line([(X, 0), (X, H)], fill=(0, 170, 255, 38), width=1)
        for v in ys(minor):
            Y = (v - oy) * zoom
            d.line([(0, Y), (W, Y)], fill=(0, 170, 255, 38), width=1)
    for v in xs(step):
        X = (v - ox) * zoom
        strong = v % label_every == 0
        d.line([(X, 0), (X, H)], fill=(0, 140, 255, 110 if strong else 60), width=1)
        if strong:
            d.text((X, 2), str(v), fill=(0, 90, 220, 255), font=f, anchor="mt")
            d.text((X, H - 2), str(v), fill=(0, 90, 220, 255), font=f, anchor="mb")
    for v in ys(step):
        Y = (v - oy) * zoom
        strong = v % label_every == 0
        d.line([(0, Y), (W, Y)], fill=(255, 60, 120, 110 if strong else 60), width=1)
        if strong:
            d.text((2, Y), str(v), fill=(200, 0, 90, 255), font=f, anchor="lm")
            d.text((W - 2, Y), str(v), fill=(200, 0, 90, 255), font=f, anchor="rm")
    return Image.alpha_composite(base, over).convert("RGB")


def make_tiles(img: Image.Image, tiles_dir: Path) -> list[dict]:
    tiles_dir.mkdir(parents=True, exist_ok=True)
    zoom = 3 if img.width <= 1000 else 2 if img.width <= 2200 else 1
    tw, th = int(1100 / zoom), int(820 / zoom)
    ovx, ovy = int(tw * 0.18), int(th * 0.18)
    out = []
    ys_ = list(range(0, max(1, img.height - ovy), th - ovy))
    xs_ = list(range(0, max(1, img.width - ovx), tw - ovx))
    for r, y in enumerate(ys_):
        for c, x in enumerate(xs_):
            box = (x, y, min(x + tw, img.width), min(y + th, img.height))
            crop = img.crop(box).resize(((box[2] - box[0]) * zoom, (box[3] - box[1]) * zoom), Image.Resampling.LANCZOS)
            crop = draw_grid(crop, step=25, label_every=50, zoom=zoom, offset=(x, y), minor=5 if zoom >= 2 else 0)
            p = tiles_dir / f"tile_r{r}_c{c}.png"
            crop.save(p)
            out.append({"file": str(p), "box": list(box), "zoom": zoom})
    return out


# -------------------------------------------------------------------- main

def ingest(inp: Path, out: Path | None = None, page: int | None = None, list_only: bool = False) -> int:
    inp = inp.resolve()
    out = (out or inp.with_suffix("").parent / (inp.stem + ".sch2kicad")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    meta: dict = {"input": str(inp)}
    text: list[dict] = []
    parts_list: list = []
    if inp.suffix.lower() == ".pdf":
        rows = pdf_pages(inp, out)
        parts_list = bom.extract(inp)
        if list_only or page is None:
            print(f"{inp.name}: {len(rows)} pages (thumbnails in {out / 'pages'})")
            for r in rows:
                ims = ", ".join(f"{i['w']}x{i['h']} ({int(i['area'] * 100)}% of page)" for i in r["images"]) or "-"
                print(f"  page {r['page']:2}: score {r['score']:5}  refs~{r['refs']:3}  drawings {r['drawings']:5}  images {ims}")
            best = max(rows, key=lambda r: r["score"])
            print(f"likely schematic: page {best['page']} — re-run with --page N after checking the thumbnail")
            if parts_list:
                print(f"parts list: {len(parts_list)} rows on page {bom.pages(parts_list)}")
            return 0
        img, pmeta, text = pdf_schematic(inp, page, out)
        meta.update(pmeta)
    else:
        img = flatten(Image.open(inp))
        meta["kind"] = "image"
    rot = detect_rotation(img, out)
    if rot:
        img = img.rotate(-rot, expand=True)
        print(f"  turned {rot}° clockwise so the text reads upright")
    meta["rotated"] = rot
    src = out / "source.png"
    img.save(src)
    meta.update({"width": img.width, "height": img.height})
    draw_grid(img, step=50, label_every=100).save(out / "grid.png")
    tiles = make_tiles(img, out / "tiles")
    dots = find_dots(img)
    words = ocr(img, out)
    (out / "dots.json").write_text(json.dumps(dots, indent=1) + "\n")
    (out / "ocr.json").write_text(json.dumps(words, indent=1) + "\n")
    if text:
        (out / "text.json").write_text(json.dumps(text, indent=1) + "\n")
    meta["tiles"] = tiles
    (out / "ingest.json").write_text(json.dumps(meta, indent=2) + "\n")
    if parts_list:
        (out / "bom.json").write_text(json.dumps(bom.rows_json(parts_list), indent=1) + "\n")
    skel = out / "circuit.json"
    had_skel = skel.exists()
    if not had_skel:
        skel.write_text(json.dumps({
            "schema": SCHEMA,
            "name": inp.stem,
            "source": {"file": inp.name, "image": "source.png", "width": img.width, "height": img.height,
                       **({"page": page} if page else {})},
            "title_block": {},
            "nets": {"GND": {"kind": "ground"}},
            "parts": [], "ports": [], "texts": [], "notes": [], "blocks": [], "uncertain": [],
            **({"bom": bom.rows_json(parts_list)} if parts_list else {}),
        }, indent=2) + "\n")
    print(f"{inp.name} → {out}")
    print(f"  source.png {img.width}x{img.height}  ({meta['kind']})")
    print(f"  {len(tiles)} tiles (zoom {tiles[0]['zoom'] if tiles else '-'}x), {len(dots)} junction dots, "
          f"{len(words)} OCR words" + (f", {len(text)} exact text spans" if text else ""))
    if parts_list:
        print(f"  parts list: {len(parts_list)} rows from page {bom.pages(parts_list)} → bom.json"
              + ("" if not had_skel else " (circuit.json already exists: copy them into its \"bom\")"))
    print(f"  circuit.json skeleton: {skel}")
    return 0
