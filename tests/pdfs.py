"""Small PDFs written by hand, for tests that need a page with known contents."""
from __future__ import annotations

import zlib
from pathlib import Path


def write(path: Path, width: float, height: float, content: str, *, page: str = "",
          xobjects: dict[str, bytes] | None = None) -> Path:
    """A one-page PDF: ``content`` is the page's content stream, ``page`` extra page
    dictionary entries (``/Rotate 90``), ``xobjects`` whole stream objects by name."""
    names = " ".join(f"/{n} {4 + i} 0 R" for i, n in enumerate(xobjects or {}))
    body = content.encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] {page} "
             f"/Resources << /XObject << {names} >> >> /Contents {4 + len(xobjects or {})} 0 R >>").encode()]
    objs += list((xobjects or {}).values())
    objs.append(b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream")
    out, at = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        at.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % a for a in at)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def stream(dictionary: str, data: bytes) -> bytes:
    """A stream object, its data deflated."""
    packed = zlib.compress(data)
    return (f"<< {dictionary} /Filter /FlateDecode /Length {len(packed)} >>\nstream\n".encode()
            + packed + b"\nendstream")


class Canvas:
    """Strokes drawn from the top left of the page, as the program sees a page."""

    def __init__(self, height: float):
        self.height = height
        self.ops: list[str] = []

    def draw_line(self, a, b, color=(0, 0, 0), width: float = 1) -> None:
        r, g, bl = color
        self.ops.append(f"{r} {g} {bl} RG {width} w {a[0]} {self.height - a[1]} m "
                        f"{b[0]} {self.height - b[1]} l S")

    def content(self) -> str:
        return "\n".join(self.ops)
