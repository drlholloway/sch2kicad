"""Check a built schematic independently of the code that wrote it.

Everything here reads KiCad's own output (``kicad-cli``), never the builder's
in-memory state: the netlist KiCad derives from the file is compared pin for
pin with circuit.json, ERC runs with KiCad's rules, and every footprint is
looked up in your fp-lib-table.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import bom, sexpr
from .kicad_env import footprint_exists, kicad_cli, lib_table


@dataclass
class Report:
    sch: Path
    netlist_ok: bool = False
    netlist_diffs: list[str] = field(default_factory=list)
    erc_errors: list[str] = field(default_factory=list)
    erc_warnings: list[str] = field(default_factory=list)
    footprint_problems: list[str] = field(default_factory=list)
    build_problems: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    bom_rows: int = 0                  # board parts in the doc's parts list (0 = no list)
    bom_matched: int = 0
    bom_problems: list[str] = field(default_factory=list)
    bom_warnings: list[str] = field(default_factory=list)
    components: int = 0
    nets: int = 0
    drawing: object | None = None      # trace.Crosscheck, when the source can be read again
    board: object | None = None        # vero.BoardCheck, when the schematic was drawn from a stripboard
    render: Path | None = None
    compare: Path | None = None

    @property
    def ok(self) -> bool:
        return (self.netlist_ok and not self.erc_errors and not self.footprint_problems
                and not self.build_problems and not self.unconfirmed and not self.uncertain
                and not self.bom_problems and (self.board is None or self.board.ok))

    def text(self) -> str:
        def sect(title, items, mark):
            if not items:
                return []
            return [f"{title}:"] + [f"  {mark} {i}" for i in items]
        lines = [f"{self.sch.name}: {self.components} parts, {self.nets} nets"]
        lines.append(f"  netlist:    {'matches circuit.json pin for pin  OK' if self.netlist_ok else 'DIFFERS'}")
        lines.append(f"  ERC:        {len(self.erc_errors)} errors, {len(self.erc_warnings)} warnings")
        lines.append(f"  footprints: {'all resolve  OK' if not self.footprint_problems else f'{len(self.footprint_problems)} problems'}")
        if self.bom_rows:
            state = "OK" if not self.bom_problems else f"{len(self.bom_problems)} differ"
            lines.append(f"  parts list: {self.bom_matched} of {self.bom_rows} agree with the drawing  {state}")
        d = self.drawing
        if d is not None:
            state = "the reading agrees  OK" if not d.disagreements else f"{d.disagreements} disagree"
            lines.append(f"  drawing:    {d.checked} of {d.drawn} pins traced through "
                         f"{d.traced} conductors, {state}")
        bd = self.board
        if bd is not None:
            state = "the schematic agrees  OK" if bd.ok else f"{len(bd.diffs) + len(bd.problems)} problems"
            lines.append(f"  board:      {bd.pins} pins on the {bd.board} layout, {state}")
        lines += sect("netlist differences", self.netlist_diffs, "problem:")
        lines += sect("ERC errors", self.erc_errors, "problem:")
        lines += sect("footprints", self.footprint_problems, "problem:")
        lines += sect("build", self.build_problems, "problem:")
        lines += sect("pinouts to confirm", self.unconfirmed, "problem:")
        lines += sect("needs your eyes (marked CHECK on the sheet)", self.uncertain, "problem:")
        lines += sect("parts list (re-read the drawing; accept a real difference in bom_accept)",
                      self.bom_problems, "problem:")
        lines += sect("parts list", self.bom_warnings, "warning:")
        if bd is not None:
            lines += sect("the board reading (sch2kicad vero board.json)", bd.problems, "problem:")
            lines += sect("the schematic connects differently from the board", bd.diffs, "problem:")
        if d is not None:
            lines += sect("the drawing wires it differently (re-read it; the netlist is what a board is)",
                          d.splits + d.merges + d.misnamed, "warning:")
            lines += sect("pin positions a few pixels out", d.misplaced, "note:")
            if d.unclaimed:
                at = ", ".join(f"({x},{y})" for x, y in d.unclaimed[:8])
                lines += sect("wire in the drawing that nothing in the reading reaches", [at], "warning:")
            lines += sect("parts the drawing does not draw, so nothing of them was checked",
                          d.undrawn, "note:")
            if d.unmatched:
                lines += sect("no wire found under these pins, so nothing there was checked "
                              "(the nearest ink says where the drawing puts them)",
                              d.unmatched[:10], "note:")
        lines += sect("ERC warnings", self.erc_warnings, "warning:")
        lines += sect("notes", self.notes, "note:")
        if self.compare:
            lines.append(f"side by side: {self.compare}")
        lines.append("READY" if self.ok else "NOT READY — fix the problems above")
        return "\n".join(lines)


# ------------------------------------------------------------------ netlist

def export_netlist(sch: Path, out: Path) -> Path:
    r = subprocess.run([str(kicad_cli()), "sch", "export", "netlist", "--format", "kicadsexpr",
                        "-o", str(out), str(sch)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"netlist export failed:\n{r.stdout}\n{r.stderr}")
    return out


def parse_netlist(path: Path) -> tuple[dict[str, dict], dict[str, set[str]], dict[str, str]]:
    """Components, nets (name → {"REF.PIN"}) and each node's electrical pin type."""
    root = sexpr.parse(path.read_text())
    comps: dict[str, dict] = {}
    for comp in sexpr.children(sexpr.child(root, "components") or ["components"], "comp"):
        ref = str(sexpr.value(sexpr.child(comp, "ref")))
        comps[ref] = {
            "value": str(sexpr.value(sexpr.child(comp, "value"), 1, "")),
            "footprint": str(sexpr.value(sexpr.child(comp, "footprint"), 1, "")),
        }
    nets: dict[str, set[str]] = {}
    pintypes: dict[str, str] = {}
    for net in sexpr.children(sexpr.child(root, "nets") or ["nets"], "net"):
        name = str(sexpr.value(sexpr.child(net, "name"), 1, ""))
        nodes = set()
        for n in sexpr.children(net, "node"):
            node = f"{sexpr.value(sexpr.child(n, 'ref'))}.{sexpr.value(sexpr.child(n, 'pin'))}"
            nodes.add(node)
            pintypes[node] = str(sexpr.value(sexpr.child(n, "pintype"), 1, ""))
        nets[name] = nodes
    return comps, nets, pintypes


def _power_symbol_nets(sch: Path) -> dict[str, str]:
    """#PWR reference → the net it names (a power symbol's Value is its net name)."""
    out = {}
    for sym in sexpr.children(sexpr.parse(sch.read_text()), "symbol"):
        props = {str(p[1]): str(p[2]) for p in sexpr.children(sym, "property") if len(p) > 2}
        if props.get("Reference", "").startswith("#PWR"):
            out[props["Reference"]] = props.get("Value", "")
    return out


def fold_output_driven(errors: list[str], sch: Path, nets: dict[str, set[str]],
                       pintypes: dict[str, str]) -> tuple[list[str], list[str]]:
    """A power net an output pin drives (an op-amp buffering VREF) has no power output for
    KiCad's ERC, and a PWR_FLAG would clash with the output pin. Where KiCad's own
    netlist shows the output driving the net, the complaint becomes a note."""
    import re
    drivers = {name: sorted(n for n in nodes if pintypes.get(n) == "output") for name, nodes in nets.items()}
    pwr = _power_symbol_nets(sch)
    keep: list[str] = []
    folded: dict[str, list[str]] = {}
    for e in errors:
        m = re.match(r"\[power_pin_not_driven\].* Symbol (\S+) Pin (\S+) ", e)
        if m:
            ref, pin = m.groups()
            net = pwr.get(ref) or next((name for name, nodes in nets.items() if f"{ref}.{pin}" in nodes), None)
            if net and drivers.get(net):
                folded[net] = drivers[net]
                continue
        keep.append(e)
    notes = [f"{net} is driven by an output pin ({', '.join(outs)}), which KiCad's ERC doesn't count "
             f"as a power source; a PWR_FLAG would clash with it, so there is none" for net, outs in folded.items()]
    return keep, notes


def fold_open_driven(errors: list[str], nets: dict[str, set[str]],
                     pintypes: dict[str, str]) -> tuple[list[str], list[str]]:
    """A base fed straight from another transistor's collector (the Angry Droid's
    2N3904 into its 2N3906) has nothing on its net KiCad counts as a driver: the house
    symbols type collector and emitter as open drivers, and KiCad's ERC takes only
    outputs, passives and the like. Where KiCad's own netlist shows an open collector
    or emitter on the input's net, the complaint becomes a note; any other undriven
    input stays an error."""
    import re
    keep: list[str] = []
    folded: dict[str, tuple[str, list[str]]] = {}
    for e in errors:
        m = re.match(r"\[pin_not_driven\].* Symbol (\S+) Pin (\S+) ", e)
        if m:
            node = f"{m.group(1)}.{m.group(2)}"
            net = next((name for name, nodes in nets.items() if node in nodes), None)
            opens = sorted(n for n in nets.get(net, ()) if pintypes.get(n) in ("open_collector", "open_emitter"))
            if opens:
                folded.setdefault(net, (node, opens))
                continue
        keep.append(e)
    notes = [f"{node} is driven by {', '.join(opens)} (open collector/emitter), which KiCad's ERC "
             f"doesn't count as a driver" for node, opens in folded.values()]
    return keep, notes


def fold_tied_outputs(errors: list[str], nets: dict[str, set[str]],
                      pintypes: dict[str, str]) -> tuple[list[str], list[str]]:
    """A BBD's two output pins tied together (the Pocket Comb's MN3207, 7 and 8) are
    two outputs on one net to KiCad's ERC. Where every output on the net belongs to
    the one part, the drawing is summing that part's own outputs and the complaint
    becomes a note; outputs of two parts on one net stay an error."""
    import re
    keep: list[str] = []
    folded: dict[str, list[str]] = {}
    for e in errors:
        m = re.match(r"\[pin_to_pin\] Pins of type Output and Output .* Symbol (\S+) Pin (\S+) ", e)
        if m:
            node = f"{m.group(1)}.{m.group(2)}"
            net = next((name for name, nodes in nets.items() if node in nodes), None)
            outs = sorted(n for n in nets.get(net, ()) if pintypes.get(n) == "output")
            if outs and len({n.split(".")[0] for n in outs}) == 1:
                folded[net] = outs
                continue
        keep.append(e)
    notes = [f"{' and '.join(outs)} are tied on {net}, as drawn: one part's outputs summed, "
             f"which KiCad's ERC reads as two outputs fighting" for net, outs in folded.items()]
    return keep, notes


def fold_strapped_inputs(errors: list[str], nets: dict[str, set[str]],
                         pintypes: dict[str, str]) -> tuple[list[str], list[str]]:
    """An LM386's gain pins, 1 and 8, strapped together for gain 200 (the Seppuku
    Octave Drone and Space Fuzz) make a net of nothing but that chip's own inputs, so
    KiCad's ERC finds nothing driving it. Where every pin on the net is an input of the
    one part, the strap is the drawing's and the complaint becomes a note; a net with
    any other part on it stays an error."""
    import re
    keep: list[str] = []
    folded: dict[str, list[str]] = {}
    for e in errors:
        m = re.match(r"\[pin_not_driven\].* Symbol (\S+) Pin (\S+) ", e)
        if m:
            node = f"{m.group(1)}.{m.group(2)}"
            net = next((name for name, nodes in nets.items() if node in nodes), None)
            nodes = nets.get(net, set())
            if (len(nodes) > 1 and len({n.split(".")[0] for n in nodes}) == 1
                    and all(pintypes.get(n) == "input" for n in nodes)):
                folded.setdefault(net, sorted(nodes))
                continue
        keep.append(e)
    notes = [f"{' and '.join(nodes)} are strapped together on {net}, as drawn; nothing else is on "
             f"that net, so KiCad's ERC finds no driver" for net, nodes in folded.items()]
    return keep, notes


def fold_pads_only(errors: list[str], nets: dict[str, set[str]],
                   pads: set[str]) -> tuple[list[str], list[str]]:
    """A net that joins nothing but pads is a wire leaving the board and coming back:
    the Halsted St OD carries each jack's line through two pads (H7 to H11) by label.
    The pads are inputs to KiCad, so no pin drives the net; nothing on the board
    could, and the complaint becomes a note. A pad on a net with a part stays an error."""
    import re
    keep: list[str] = []
    folded: dict[str, list[str]] = {}
    for e in errors:
        m = re.match(r"\[pin_not_driven\].* Symbol (\S+) Pin (\S+) ", e)
        if m and pads:
            node = f"{m.group(1)}.{m.group(2)}"
            net = next((name for name, nodes in nets.items() if node in nodes), None)
            if net is not None and all(n.split(".")[0] in pads for n in nets[net]):
                folded.setdefault(net, sorted(nets[net]))
                continue
        keep.append(e)
    notes = [f"{', '.join(nodes)} join only pads (an off-board wire), so nothing on the board drives "
             f"them; KiCad's ERC says so" for nodes in folded.values()]
    return keep, notes


def fold_fed_off_board(errors: list[str], nets: dict[str, set[str]], pads: set[str],
                       pintypes: dict[str, str]) -> tuple[list[str], list[str]]:
    """The Seppuku Memory Loss runs its input jack straight into the LM386's pin 2,
    with no cap or resistor: a net of the IN pad and a chip input. What drives it is
    the guitar, off the board, so the complaint becomes a note. Where anything else on
    the board shares the net (a resistor, an output) it stays for the other rules."""
    import re
    keep: list[str] = []
    folded: dict[str, list[str]] = {}
    for e in errors:
        m = re.match(r"\[pin_not_driven\].* Symbol (\S+) Pin (\S+) ", e)
        if m and pads:
            node = f"{m.group(1)}.{m.group(2)}"
            net = next((name for name, nodes in nets.items() if node in nodes), None)
            nodes = nets.get(net, set())
            on_pads = sorted(n for n in nodes if n.split(".")[0] in pads)
            rest = [n for n in nodes if n.split(".")[0] not in pads]
            if on_pads and rest and all(pintypes.get(n) == "input" for n in rest):
                folded.setdefault(net, sorted(rest))
                continue
        keep.append(e)
    notes = [f"{', '.join(ins)} {'is' if len(ins) == 1 else 'are'} fed only through a pad, from off the "
             f"board, so nothing on the board drives {'it' if len(ins) == 1 else 'them'}; KiCad's ERC says so"
             for ins in folded.values()]
    return keep, notes


def fold_ground_on_rail(warnings: list[str], expected: dict[str, list[str]] | None,
                        rails: set[str]) -> tuple[list[str], list[str]]:
    """A chip whose pin is called GND but sits on a supply the reading declares.

    The MN3005 and MN3101 are P-channel parts: their pin named GND is the top rail,
    so DOD's FX96 runs it on a filtered +9V and ties VDD to ground. KiCad's ERC warns
    that a ground pin is not on ground. Where circuit.json says the net is a power
    rail, that was read on purpose, and the warning becomes a note."""
    import re
    keep: list[str] = []
    folded: dict[str, list[str]] = {}
    for w in warnings:
        m = re.match(r"\[ground_pin_not_ground\].* Symbol (\S+) Pin (\S+) ", w)
        if m and expected:
            node = f"{m.group(1)}.{m.group(2)}"
            net = next((name for name, nodes in expected.items() if node in nodes), None)
            if net in rails:
                folded.setdefault(net, []).append(node)
                continue
        keep.append(w)
    notes = [f"{' and '.join(nodes)} {'are' if len(nodes) > 1 else 'is'} a pin named GND on the {net} "
             f"rail, as drawn (a P-channel chip's top supply)" for net, nodes in folded.items()]
    return keep, notes


def compare_nets(expected: dict[str, list[str]], actual: dict[str, set[str]]) -> list[str]:
    """Compare partitions of pins; net names are irrelevant, connectivity is not."""
    diffs: list[str] = []
    where: dict[str, str] = {}
    for name, nodes in actual.items():
        for n in nodes:
            where[n] = name
    exp_where: dict[str, str] = {}
    for name, nodes in expected.items():
        for n in nodes:
            exp_where[n] = name
    missing = sorted(set(exp_where) - set(where))
    extra = sorted(set(where) - set(exp_where))
    if missing:
        diffs.append(f"pins KiCad does not see: {', '.join(missing)}")
    if extra:
        diffs.append(f"pins KiCad has that circuit.json does not: {', '.join(extra)}")
    seen_actual: set[str] = set()
    for name, nodes in expected.items():
        nodes = [n for n in nodes if n in where]
        if not nodes:
            continue
        groups: dict[str, list[str]] = {}
        for n in nodes:
            groups.setdefault(where[n], []).append(n)
        label = "unconnected pin" if name.startswith("__nc__") else f"net {name}"
        if len(groups) > 1:
            parts = "; ".join(f"{', '.join(v)}" for v in groups.values())
            diffs.append(f"{label} is split in KiCad: {parts}")
        for aname in groups:
            if aname in seen_actual:
                continue
            seen_actual.add(aname)
            foreign = sorted(n for n in actual[aname] if n in exp_where and exp_where[n] != name)
            if foreign:
                others = sorted({exp_where[n] for n in foreign})
                shown = [("unconnected " + o[6:]) if o.startswith("__nc__") else o for o in others]
                diffs.append(f"{label} is joined in KiCad to {', '.join(shown)} (via {', '.join(foreign)})")
    return diffs


# ---------------------------------------------------------------------- ERC

def run_erc(sch: Path, out: Path) -> tuple[list[str], list[str]]:
    subprocess.run([str(kicad_cli()), "sch", "erc", "--format", "json", "--severity-all",
                    "-o", str(out), str(sch)], capture_output=True, text=True)
    if not out.exists():
        return ([f"ERC did not run on {sch.name}"], [])
    data = json.loads(out.read_text())
    errors, warnings = [], []
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            items = "; ".join(i.get("description", "") for i in v.get("items", []))
            msg = f"[{v.get('type')}] {v.get('description')}" + (f" — {items}" if items else "")
            (errors if v.get("severity") == "error" else warnings).append(msg)
    return errors, warnings


# ------------------------------------------------------------------- render

def render_png(sch: Path, out_png: Path, dpi: int = 150, crop: bool = True) -> Path:
    import pymupdf
    pdf = out_png.with_suffix(".pdf")
    args = [str(kicad_cli()), "sch", "export", "pdf", "--no-background-color"]
    if crop:
        args.append("--exclude-drawing-sheet")
    r = subprocess.run(args + ["-o", str(pdf), str(sch)], capture_output=True, text=True)
    if r.returncode != 0 or not pdf.exists():
        raise RuntimeError(f"PDF export failed: {r.stdout}{r.stderr}")
    doc = pymupdf.open(pdf)
    page = doc[0]
    clip = None
    if crop:
        rects = [d["rect"] for d in page.get_drawings()]
        rects += [pymupdf.Rect(b[:4]) for b in page.get_text("blocks")]
        rects = [r for r in rects if not r.is_infinite]
        if rects:
            clip = pymupdf.Rect(min(r.x0 for r in rects) - 12, min(r.y0 for r in rects) - 12,
                                max(r.x1 for r in rects) + 12, max(r.y1 for r in rects) + 12)
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    pix.save(out_png)
    doc.close()
    return out_png


def circuit_extent(circuit, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Source-pixel box around everything circuit.json places, so the title block
    and empty margins of the source don't shrink the drawing in a comparison."""
    pts = [pin.at for p in circuit.parts for pin in p.pins.values() if pin.at]
    pts += [x.at for x in circuit.ports if x.at] + [t.at for t in circuit.texts if t.at]
    pts += [u.at for u in circuit.uncertain if u.at]
    if len(pts) < 2:
        return None
    w, h = size
    m = max(30, int(0.04 * max(w, h)))   # room for refs, values and knob names
    return (max(0, int(min(p[0] for p in pts)) - m), max(0, int(min(p[1] for p in pts)) - m),
            min(w, int(max(p[0] for p in pts)) + m), min(h, int(max(p[1] for p in pts)) + m))


def side_by_side(source: Path, render: Path, out: Path, crop: tuple[int, int, int, int] | None = None,
                 layout: str = "stacked", width: int = 1600, height: int = 820) -> Path:
    """Source and KiCad render at the same scale: stacked (default) or side by side."""
    from PIL import Image, ImageDraw
    from .ingest import _font
    a = Image.open(source).convert("RGB")
    if crop:
        a = a.crop(crop)
    b = Image.open(render).convert("RGB")
    pad, gap = 44, 24
    if layout == "side":
        a = a.resize((max(1, int(a.width * height / a.height)), height), Image.Resampling.LANCZOS)
        b = b.resize((max(1, int(b.width * height / b.height)), height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (a.width + b.width + gap * 3, height + pad + gap), "white")
        canvas.paste(a, (gap, pad))
        canvas.paste(b, (a.width + gap * 2, pad))
        d = ImageDraw.Draw(canvas)
        x_mid = a.width + gap * 1.5
        d.line([(x_mid, pad), (x_mid, pad + height)], fill=(200, 200, 200), width=2)
        f = _font(24)
        d.text((gap, 10), "ORIGINAL", fill=(190, 0, 0), font=f)
        d.text((a.width + gap * 2, 10), "KICAD (generated)", fill=(190, 0, 0), font=f)
    else:
        a = a.resize((width, max(1, int(a.height * width / a.width))), Image.Resampling.LANCZOS)
        b = b.resize((width, max(1, int(b.height * width / b.width))), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, a.height + b.height + pad * 2), "white")
        d = ImageDraw.Draw(canvas)
        f = _font(20)
        d.text((8, 10), "ORIGINAL", fill=(190, 0, 0), font=f)
        canvas.paste(a, (0, pad))
        d.text((8, a.height + pad + 10), "KICAD (generated)", fill=(190, 0, 0), font=f)
        canvas.paste(b, (0, a.height + pad * 2))
    canvas.save(out)
    return out


# -------------------------------------------------------------------- verify

def verify(sch: Path, build_json: Path | None = None, source_image: Path | None = None,
           work: Path | None = None, render: bool = True) -> Report:
    sch = sch.resolve()
    work = work or sch.parent / "sch2kicad"
    work.mkdir(parents=True, exist_ok=True)
    build_json = build_json or work / "build.json"
    meta = json.loads(build_json.read_text()) if build_json.exists() else {}
    rep = Report(sch=sch)
    net_path = export_netlist(sch, work / "kicad.net")
    comps, nets, pintypes = parse_netlist(net_path)
    rep.components = len(comps)
    rep.nets = len(nets)
    expected = meta.get("expected")
    if expected is not None:
        rep.netlist_diffs = compare_nets(expected, nets)
        rep.netlist_ok = not rep.netlist_diffs
    else:
        rep.netlist_diffs = ["no build.json next to the schematic: nothing to compare against"]
    rep.erc_errors, rep.erc_warnings = run_erc(sch, work / "erc.json")
    rep.erc_errors, driven_notes = fold_output_driven(rep.erc_errors, sch, nets, pintypes)
    rep.notes += driven_notes
    rep.erc_errors, open_notes = fold_open_driven(rep.erc_errors, nets, pintypes)
    rep.notes += open_notes
    rep.erc_errors, tied_notes = fold_tied_outputs(rep.erc_errors, nets, pintypes)
    rep.notes += tied_notes
    rep.erc_errors, strap_notes = fold_strapped_inputs(rep.erc_errors, nets, pintypes)
    rep.notes += strap_notes
    rep.erc_errors, pad_notes = fold_pads_only(rep.erc_errors, nets, set(meta.get("pads") or ()))
    rep.notes += pad_notes
    rep.erc_errors, fed_notes = fold_fed_off_board(rep.erc_errors, nets, set(meta.get("pads") or ()), pintypes)
    rep.notes += fed_notes
    rep.erc_warnings, rail_notes = fold_ground_on_rail(rep.erc_warnings, expected, set(meta.get("rails") or ()))
    rep.notes += rail_notes
    # a library symbol with pins off the 50 mil grid makes KiCad flag every pin and the
    # short stub joining it to the grid; say that once instead of once per endpoint
    off = [w for w in rep.erc_warnings if w.startswith("[endpoint_off_grid]")]
    if off:
        import re as _re
        refs = sorted({m.group(1) for w in off for m in [_re.search(r"Symbol (\S+) Pin", w)] if m})
        rep.erc_warnings = [w for w in rep.erc_warnings if not w.startswith("[endpoint_off_grid]")]
        rep.notes.append(f"KiCad flags {len(off)} endpoints as off the 50 mil grid: the pins of "
                         f"{', '.join(refs) or 'a library symbol'} sit off-grid in the library, joined to the "
                         f"grid by short stubs. Connections are unaffected.")
    fps = lib_table("fp", sch.parent)
    for ref, c in sorted(comps.items()):
        fp = c["footprint"]
        if not fp:
            rep.footprint_problems.append(f"{ref} has no footprint")
        elif not footprint_exists(fp, fps):
            rep.footprint_problems.append(f"{ref}: footprint {fp} not found in your fp-lib-table")
    for p in meta.get("problems", []):
        if "pinout" in p and ("unconfirmed" in p or "not in the pinout table" in p or "no pinout" in p):
            rep.unconfirmed.append(p)
        else:
            rep.build_problems.append(p)
    if meta.get("block"):
        # a block is a fragment: its boundary nets are driven from outside, and the other
        # units of a shared op-amp live in another block. Say so once; anything else stays.
        expected = ("[pin_not_driven]", "[missing_unit]", "[missing_input_pin]", "[missing_power_pin]",
                    "[isolated_pin_label]")
        cut = [e for e in rep.erc_errors + rep.erc_warnings if e.startswith(expected)]
        if cut:
            rep.erc_errors = [e for e in rep.erc_errors if not e.startswith(expected)]
            rep.erc_warnings = [e for e in rep.erc_warnings if not e.startswith(expected)]
            kinds = sorted({e.split("]")[0][1:] for e in cut})
            rep.notes.append(f"expected for a block cut from a larger circuit ({len(cut)}: "
                             f"{', '.join(kinds)}) — its boundary nets connect outside the block")
    rep.uncertain = list(meta.get("uncertain", []))
    rep.notes = list(meta.get("notes", [])) + rep.notes
    if meta.get("bom"):
        # the values KiCad reports, not the ones circuit.json asked for
        info = meta["bom"]["parts"]
        parts = {ref: {"value": c["value"], **info[ref]} for ref, c in comps.items() if ref in info}
        chk = bom.check(parts, meta["bom"]["rows"], meta["bom"].get("accept"))
        rep.bom_rows, rep.bom_matched = chk.rows, chk.matched
        rep.bom_problems, rep.bom_warnings = chk.problems, chk.warnings
        rep.notes += chk.notes
    circuit = None
    if meta.get("circuit"):
        from .circuit import load as _load
        circuit = _load(Path(meta["circuit"]))
    if circuit is not None and circuit.board:
        # drawn from a stripboard: the board is the source, and it is checked exactly
        from .vero import check_circuit
        rep.board = check_circuit(circuit, circuit.board_path())
    elif circuit is not None and not circuit.ours:
        # (a schematic drawn for the reading has no drawing to check it against)
        try:
            from .trace import crosscheck_circuit
            rep.drawing = crosscheck_circuit(circuit)
        except Exception as e:      # reading the source again is a check, never the gate
            rep.notes.append(f"could not read the drawing again to check it: {e}")
    if meta.get("labelled_nets"):
        rep.notes.append("joined with net labels instead of wires: " + ", ".join(meta["labelled_nets"]))
    if meta.get("moved"):
        rep.notes.append("nudged off their source position to avoid overlaps: " + ", ".join(meta["moved"]))
    if render:
        try:
            rep.render = render_png(sch, work / "render.png")
            if source_image and Path(source_image).exists():
                crop = None
                if circuit is not None and not circuit.board:
                    from PIL import Image
                    from .circuit import load
                    with Image.open(source_image) as im:
                        crop = circuit_extent(load(meta["circuit"]), im.size)
                rep.compare = side_by_side(Path(source_image), rep.render, work / "compare.png", crop=crop)
        except Exception as e:  # rendering is a convenience, never the gate
            rep.notes.append(f"render failed: {e}")
    (work / "report.txt").write_text(rep.text() + "\n")
    return rep
