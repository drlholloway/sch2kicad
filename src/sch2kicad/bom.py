"""The parts list in a build doc, and the check of a schematic against it.

PedalPCB and Aion FX print the parts list as a text table (LOCATION or PART,
VALUE, TYPE, NOTES), so its rows can be read column by column from the words'
positions. Ingest puts the rows into circuit.json as ``bom``; verify checks every
part in KiCad's netlist against them. A value that disagrees, or a listed part
the drawing never showed, is a problem until the reading is fixed or the user
accepts the difference in ``bom_accept``.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .circuit import parse_action, parse_value
from .parts import _CMOS4000

_REF_HEADS = {"LOCATION", "PART"}
_HEADS = {"VALUE": "value", "TYPE": "type", "NOTES": "notes"}
_REF = re.compile(r"^[A-Z][A-Z0-9_+\-]*$")
# rows for the enclosure and wiring rather than the board
_HARDWARE = re.compile(r"socket|jack|enclosure|stomp|footswitch|knob|bezel|standoff|spacer|washer|"
                       r"hook-?up wire|header", re.I)
_TAPER = re.compile(r"^([ABCWG])(?=\d)|(?<=[\dkKmM])([ABCW])$")
# a parts list laid out as columns under section headings (Parasit Studio)
_GRID_HEADS = {"CAPACITORS": "Capacitor", "RESISTORS": "Resistor", "IC'S": "IC", "ICS": "IC",
               "POTENTIOMETERS": "Potentiometer", "TRIMMERS": "Trimmer", "SWITCHES": "Switch",
               "DIODES": "Diode", "LEDS": "LED", "TRANSISTORS": "Transistor",
               "CONNECTORS": "Connector", "HARDWARE": "Hardware", "MISC": "Misc"}
_GRID_REF = re.compile(r"^[A-Za-z]{1,8}\d{0,3}$")
# what a column of references looks like, strictly enough that a paragraph's left
# margin ("The", "the", "a") is not mistaken for one
_GRID_COL_REF = re.compile(r"^([A-Za-z]{1,4}\d{1,3}|[A-Z]{2,10})$")
VALUE_TYPES = {"resistor", "capacitor", "capacitor_polarized", "inductor", "ldr", "pot", "trimmer"}
PART_TYPES = {"npn", "pnp", "njfet", "pjfet", "nmos", "pmos", "diode", "zener", "schottky",
              "opamp_single", "opamp_dual", "opamp_quad", "lm386", "ic", "transformer"}


@dataclass
class Row:
    ref: str
    value: str
    type: str = ""
    notes: str = ""
    page: int = 0

    @property
    def hardware(self) -> bool:
        return bool(_HARDWARE.search(self.type))

    @property
    def is_pot(self) -> bool:
        return bool(re.search(r"\bpot\b|potentiometer|trimmer|trimpot", self.type, re.I))


def _lines(words: list, tol: float = 2.5) -> list[tuple[float, list]]:
    """Words grouped into text lines by vertical centre, each line left to right."""
    lines: list[list] = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        yc = (w[1] + w[3]) / 2
        if lines and abs(lines[-1][0] - yc) <= tol:
            lines[-1][1].append(w)
        else:
            lines.append([yc, [w]])
    return [(yc, sorted(ws, key=lambda w: w[0])) for yc, ws in lines]


# a cell may open with a footnote mark or an aside — "R20 * 10K-22K", "MAX (trimpot) 50K"
_GRID_ASIDE = re.compile(r"^(\*{1,3}|\(.*\))$")
_GRID_MARK = re.compile(r"^\*{1,3}$")


def _next_ref(ref_cols: set, x: float) -> float:
    after = [c for c in ref_cols if c > x]
    return min(after) if after else float("inf")


def _value_start(cw: list):
    """The first word of a cell that is the value itself, past any mark or aside."""
    for w in cw[1:]:
        if not _GRID_ASIDE.match(w[4]):
            return w
    return cw[-1]


def _grid_rows(page, pno: int) -> list[Row]:
    """A parts list laid out as columns of ref/value pairs under bold section headings.

    Parasit Studio prints it that way: pairs of columns side by side, each pair
    headed "Capacitors", "Resistors", "IC's"..., which says what its rows are. The
    columns line up exactly, so a column is a set of words sharing a left edge, and
    a ref column is one whose words look like references.
    """
    words = [w for w in page.get_text("words") if w[4].strip()]
    bold = [sp["bbox"] for b in page.get_text("dict")["blocks"] for line in b.get("lines", [])
            for sp in line.get("spans", []) if sp["flags"] & 16]

    def is_bold(w) -> bool:
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        return any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in bold)

    heads = [(w, _GRID_HEADS[w[4].upper().strip(":")]) for w in words
             if w[4].upper().strip(":") in _GRID_HEADS and is_bold(w)]
    if len(heads) < 2:
        return []
    top = min(w[1] for w, _ in heads)
    body = [w for w in words if w[1] >= top and not is_bold(w)]

    bins: dict[float, list] = {}
    for w in body:
        bins.setdefault(round(w[0] * 2) / 2, []).append(w)
    cols = {x: ws for x, ws in bins.items() if len(ws) >= 3}
    ref_cols = {x for x, ws in cols.items()
                if sum(bool(_GRID_COL_REF.match(w[4])) for w in ws) >= 0.6 * len(ws)}
    if not ref_cols:
        return []

    def col_of(w) -> float | None:
        x = round(w[0] * 2) / 2
        return x if x in cols else None

    # each heading belongs to the ref column nearest to the left of its middle
    head_col: list[tuple[float, float, str]] = []          # (y, ref column x, type)
    for w, kind in heads:
        left = [x for x in ref_cols if x <= (w[0] + w[2]) / 2]
        if left:
            head_col.append((w[1], max(left), kind))

    rows: list[Row] = []
    steps: list[float] = []
    last_y = top
    for y, ws in _lines(body):
        cells: list[tuple[float, list]] = []
        for w in ws:
            if col_of(w) in ref_cols and _GRID_REF.match(w[4]):
                cells.append((col_of(w), [w]))
            elif cells and w[0] < _next_ref(ref_cols, cells[-1][0]):
                cells[-1][1].append(w)     # a cell ends where the next pair of columns starts
        # a real row: the value starts in a column of its own, not mid-sentence
        cells = [(x, cw) for x, cw in cells if len(cw) > 1
                 and (col_of(_value_start(cw)) in cols and col_of(_value_start(cw)) not in ref_cols)]
        if not cells:
            continue
        limit = 3 * statistics.median(steps) if steps else 60.0
        if rows and y - last_y > limit:
            break                                          # past the list: prose, another section
        for x, cw in cells:
            body_words = cw[1:]
            lead = 0                                       # marks and asides before the value
            while lead < len(body_words) - 1 and _GRID_ASIDE.match(body_words[lead][4]):
                lead += 1
            aside = [w[4] for w in body_words[:lead]]
            rest = body_words[lead:]
            aside += [w[4] for w in rest if _GRID_MARK.match(w[4])]
            value = " ".join(w[4] for w in rest if not _GRID_MARK.match(w[4]))
            mark = re.search(r"\**$", value).group(0)      # 47K*, 4.7K-22K**: a footnote
            kind = [k for hy, hx, k in head_col if hx == x and hy < y]
            rows.append(Row(cw[0][4], value[:len(value) - len(mark)] or value,
                            kind[-1] if kind else "", " ".join(aside + ([mark] if mark else [])), pno))
        steps.append(y - last_y)
        last_y = y
    return rows


def extract(pdf: Path) -> list[Row]:
    """Rows of every parts-list table (a LOCATION/PART, VALUE, TYPE header) in a PDF."""
    import pymupdf
    rows: list[Row] = []
    with pymupdf.open(pdf) as doc:
        for pno, page in enumerate(doc, 1):
            cols: list[tuple[str, float]] | None = None
            last_y = 0.0
            steps: list[float] = []
            for y, ws in _lines(page.get_text("words")):
                upper = {w[4].upper() for w in ws}
                if cols is None:
                    if {"VALUE", "TYPE"} <= upper and upper & _REF_HEADS:
                        cols = sorted([("ref" if w[4].upper() in _REF_HEADS else _HEADS[w[4].upper()], w[0])
                                       for w in ws if w[4].upper() in _REF_HEADS | set(_HEADS)],
                                      key=lambda c: c[1])
                        last_y = y
                    continue
                limit = 3 * statistics.median(steps) if steps else 40.0
                if y - last_y > limit:
                    break                              # the table has ended (footer, next section)
                cells: dict[str, list[str]] = {}
                for w in ws:
                    key = cols[0][0]
                    for k, x in cols:
                        if w[0] >= x - 2:
                            key = k
                    cells.setdefault(key, []).append(w[4])
                text = {k: " ".join(v) for k, v in cells.items()}
                ref = text.get("ref", "")
                if ref and _REF.match(ref) and text.get("value") and text.get("type"):
                    rows.append(Row(ref, text["value"], text.get("type", ""), text.get("notes", ""), pno))
                elif not ref and rows and rows[-1].page == pno:
                    for k in ("value", "type", "notes"):   # a cell wrapped onto a second line
                        if text.get(k):
                            setattr(rows[-1], k, f"{getattr(rows[-1], k)} {text[k]}".strip())
                else:
                    continue
                steps.append(y - last_y)
                last_y = y
            if cols is None:                   # no table on this page: try a column grid
                rows += _grid_rows(page, pno)
    return rows


def pages(rows: list[Row]) -> str:
    ps = sorted({r.page for r in rows})
    return ", ".join(str(p) for p in ps)


# ------------------------------------------------------------------- check

@dataclass
class Check:
    rows: int = 0                  # board parts in the list
    matched: int = 0
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return re.sub(r"[\s\-_.,/]", "", s).upper()


_POLES_RE = re.compile(r"\b(SPST|SPDT|DPDT|DPST|[1-9]P[1-9]T)\b", re.I)


def _poles(s: str) -> str:
    m = _POLES_RE.search(s)
    return m.group(1).upper() if m else ""


def _taper(v: str) -> str:
    m = _TAPER.search(v.strip())
    return (m.group(1) or m.group(2)) if m else ""


def same_value(ptype: str, drawn: str, listed: str, listed_type: str = "") -> bool | None:
    """Whether a drawn value and a listed one agree; None when they can't be compared."""
    if _norm(drawn) == _norm(listed):
        return True
    if ptype.startswith("switch"):
        # poles and action are the whole of a switch's value, and a publisher may
        # write either in either column: Aion's row for the Tri-Vibe's MODE says
        # "SPDT cntr off" under value and "Toggle switch, SPDT on-off-on" under type.
        a, b = parse_action(drawn), parse_action(listed) or parse_action(listed_type)
        pa, pb = _poles(drawn), _poles(listed) or _poles(listed_type)
        if pa and pb and pa != pb:
            return False
        if not a or not b:
            return None
        return a == b
    if ptype in VALUE_TYPES:
        a, b = parse_value(drawn), parse_value(listed)
        if a is None or b is None:
            return None
        if abs(a - b) > 1e-6 * max(abs(a), abs(b)):
            return False
        ta, tb = _taper(drawn), _taper(listed)
        return not (ta and tb and ta != tb)
    if ptype in PART_TYPES:
        a, b = _norm(drawn), _norm(listed)
        if a and b and (a in b or b in a):             # 4558 / RC4558, TL062P / TL062
            return True
        # a 4000-series chip under any maker's prefix and grade: DOD list the
        # CD4007 as 4007A
        ma, mb = _CMOS4000.match(a), _CMOS4000.match(b)
        return bool(ma and mb and ma.group(1) == mb.group(1))
    if ptype == "led":                                 # "3mm red LED" / "3mm LED"
        a, b = set(re.findall(r"[A-Z0-9]+", drawn.upper())), set(re.findall(r"[A-Z0-9]+", listed.upper()))
        return True if a and b and (a <= b or b <= a) else None
    return None


def _as_row(r: dict | Row) -> Row:
    if isinstance(r, Row):
        return r
    return Row(ref=str(r.get("ref", "")), value=str(r.get("value", "")), type=str(r.get("type", "")),
               notes=str(r.get("notes", "")), page=int(r.get("page", 0) or 0))


def check(parts: dict[str, dict], rows: list, accept: dict[str, str] | None = None) -> Check:
    """Compare board parts (ref → {"value", "type", "label"}) with parts-list rows."""
    accept = {k.upper(): v for k, v in (accept or {}).items()}
    board = [r for r in map(_as_row, rows) if not r.hardware]
    res = Check(rows=len(board))
    by_ref = {ref.upper(): ref for ref in parts}
    by_label = {p.get("label", "").upper(): ref for ref, p in parts.items() if p.get("label")}
    seen: set[str] = set()
    missing: list[Row] = []

    def report(keys: list[str], text: str, severity: str) -> None:
        why = next((accept[k.upper()] for k in keys if k.upper() in accept), None)
        if why is not None:
            res.notes.append(f"{text} — accepted: {why}")
        else:
            (res.problems if severity == "problem" else res.warnings).append(text)

    for r in board:
        ref = by_ref.get(r.ref.upper())
        if ref is None:                        # named by its knob or switch function
            ref = by_label.get(r.ref.upper())
        if ref is None or ref in seen:
            missing.append(r)
            continue
        seen.add(ref)
        p = parts[ref]
        ok = same_value(p.get("type", ""), p.get("value", ""), r.value, r.type)
        name = ref if ref.upper() == r.ref.upper() else f"{ref} ({r.ref} in the list)"
        if ok:
            res.matched += 1
        elif ok is False:
            report([ref, r.ref], f"{name}: drawn {p.get('value')}, parts list says {r.value}", "problem")
        else:
            report([ref, r.ref], f"{name}: drawn {p.get('value')!r}, parts list says {r.value!r} "
                                 f"(can't compare these)", "warning")
    extra = [ref for ref in parts if ref not in seen]

    # knobs the drawing and the list name differently (SUSTAIN in one, GAIN in the other)
    pots_missing = [r for r in missing if r.is_pot]
    pots_extra = [ref for ref in extra if parts[ref].get("type") in ("pot", "trimmer")]
    if pots_missing and pots_extra and len(pots_missing) == len(pots_extra):
        def key(v: str) -> tuple:
            return (parse_value(v), _taper(v)) if parse_value(v) is not None else (_norm(v), "")
        if sorted(key(parts[ref].get("value", "")) for ref in pots_extra) == sorted(key(r.value) for r in pots_missing):
            drawn = ", ".join(f"{ref} {parts[ref].get('value')}" for ref in pots_extra)
            listed = ", ".join(f"{r.ref} {r.value}" for r in pots_missing)
            res.warnings.append(f"knobs named differently: the drawing has {drawn}; the parts list "
                                f"has {listed} (values agree)")
            res.matched += len(pots_extra)
            missing = [r for r in missing if r not in pots_missing]
            extra = [ref for ref in extra if ref not in pots_extra]

    for r in missing:
        report([r.ref], f"{r.ref} ({r.value}, {r.type}) is in the parts list but not in the drawing", "problem")
    for ref in extra:
        report([ref], f"{ref} ({parts[ref].get('value')}) is in the drawing but not in the parts list", "warning")
    return res


def rows_json(rows: list[Row]) -> list[dict]:
    return [{k: v for k, v in asdict(r).items() if v not in ("", 0)} for r in rows]
