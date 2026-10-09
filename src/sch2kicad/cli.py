"""sch2kicad command line.

Exit codes: 0 worked / ready, 1 the work failed or is not ready, 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_build(a) -> int:
    from .build import build
    from .verify import verify
    from .circuit import load
    circuit = Path(a.circuit)
    out = Path(a.out) if a.out else circuit.parent / "kicad"
    res = build(circuit, out, profile=a.profile, name=a.name, spacing=a.spacing)
    print(f"wrote {res.sch}")
    print(f"      {res.pro}")
    if res.labelled_nets:
        print(f"  joined by net labels (no clean wire route): {', '.join(res.labelled_nets)}")
    if a.no_verify:
        return 0
    c = load(circuit)
    rep = verify(res.sch, source_image=c.source_image())
    print(rep.text())
    return 0 if rep.ok else 1


def _cmd_verify(a) -> int:
    from .verify import verify
    from .circuit import load
    sch = Path(a.schematic)
    work = sch.parent / "sch2kicad"
    src = None
    bj = work / "build.json"
    if bj.exists():
        meta = json.loads(bj.read_text())
        try:
            src = load(meta["circuit"]).source_image()
        except Exception:
            src = None
    rep = verify(sch, source_image=src, render=not a.no_render)
    print(rep.text())
    return 0 if rep.ok else 1


def _cmd_compare(a) -> int:
    """Original and KiCad render side by side (or stacked), at the same scale."""
    import platform
    import subprocess
    from PIL import Image
    from .circuit import load
    from .verify import circuit_extent, render_png, side_by_side
    sch = Path(a.schematic).resolve()
    work = sch.parent / "sch2kicad"
    work.mkdir(exist_ok=True)
    meta = json.loads((work / "build.json").read_text()) if (work / "build.json").exists() else {}
    if not meta.get("circuit"):
        print(f"error: no sch2kicad/build.json next to {sch.name}; build it with sch2kicad first", file=sys.stderr)
        return 1
    c = load(meta["circuit"])
    src = c.source_image()
    if not src or not src.exists():
        print(f"error: source image {src} not found", file=sys.stderr)
        return 1
    render = render_png(sch, work / "render-hires.png", dpi=a.dpi)
    with Image.open(src) as im:
        crop = None if c.board else circuit_extent(c, im.size)
    out = side_by_side(src, render, work / ("side-by-side.png" if a.layout == "side" else "stacked.png"),
                       crop=crop, layout=a.layout, height=a.height)
    print(out)
    if a.open:
        opener = "open" if platform.system() == "Darwin" else "xdg-open"
        subprocess.run([opener, str(out)])
    return 0


def _cmd_crosscheck(a) -> int:
    """Read the source drawing again and check circuit.json against it."""
    from .circuit import load
    from .trace import crosscheck_circuit
    c = load(Path(a.circuit))
    rep = crosscheck_circuit(c, doc=Path(a.doc) if a.doc else None)
    print(rep.text())
    return 0 if not rep.disagreements else 1


def _cmd_vero(a) -> int:
    """A stripboard layout's reading: the nets its strips make, checked against the image."""
    from .vero import derive, load_board, look, overlay, report, skeleton
    b = load_board(Path(a.board))
    nets = derive(b)
    seen = look(b)
    text, ok = report(b, nets, seen)
    print(text)
    ov = overlay(b, nets, seen, b.path.with_name(b.path.stem + ".overlay.png"))
    if ov:
        print(f"reading over the layout: {ov}")
    if a.skeleton:
        out = Path(a.skeleton)
        if out.exists() and not a.force:
            print(f"error: {out} exists; --force to replace it", file=sys.stderr)
            return 1
        out.write_text(json.dumps(skeleton(b, nets), indent=1) + "\n")
        print(f"wrote {out}: every part and net, no positions yet")
    return 0 if ok else 1


def _cmd_ingest(a) -> int:
    from .ingest import ingest
    return ingest(Path(a.input), Path(a.out) if a.out else None, page=a.page, list_only=a.list,
                  turn=a.turn)


def _cmd_blocks(a) -> int:
    from .blocks import save_blocks
    paths = save_blocks(Path(a.circuit), Path(a.out) if a.out else None, profile=a.profile,
                        only=a.block or None)
    for p in paths:
        print(f"wrote {p}")
    return 0 if paths else 1


def _cmd_pinout(a) -> int:
    from .parts import lookup_pinout, pinout_assumption
    rc = 0
    for pn in a.part:
        for t in ("npn", "njfet"):
            order, why = lookup_pinout(t, pn)
            if order or why:
                break
        if order:
            assumed = pinout_assumption(t, pn)
            print(f"{pn}: {'-'.join(order)}" + (f" (assumed — {assumed})" if assumed else ""))
        elif why:
            print(f"{pn}: ASK — {why}")
            rc = 1
        else:
            print(f"{pn}: unknown — ask for the lead order")
            rc = 1
    return rc


def _cmd_profiles(a) -> int:
    from .parts import load_profile, profile_dir
    for p in sorted(profile_dir().glob("*.toml")):
        prof = load_profile(p.stem)
        print(f"{prof.name:6} {prof.data.get('description', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sch2kicad", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="prepare an image or build-doc PDF for reading")
    p.add_argument("input")
    p.add_argument("-o", "--out", help="work directory (default: <input>.sch2kicad/)")
    p.add_argument("--page", type=int, help="PDF page holding the schematic (1-based)")
    p.add_argument("--list", action="store_true", help="just list the PDF's pages")
    p.add_argument("--turn", type=int, choices=(0, 90, 180, 270),
                   help="turn the drawing this many degrees clockwise instead of guessing "
                        "(a circuit drawn sideways on an upright page)")
    p.set_defaults(fn=_cmd_ingest)

    p = sub.add_parser("vero", help="stripboard layout reading → nets, checked against the image")
    p.add_argument("board", help="board.json")
    p.add_argument("--skeleton", help="write a circuit.json with every part and net, to draw")
    p.add_argument("--force", action="store_true", help="replace an existing --skeleton file")
    p.set_defaults(fn=_cmd_vero)

    p = sub.add_parser("build", help="circuit.json → KiCad project, then verify")
    p.add_argument("circuit")
    p.add_argument("-o", "--out", help="output directory (default: kicad/ next to circuit.json)")
    p.add_argument("-p", "--profile", default="tht", help="tht (default) or smd, or a .toml path")
    p.add_argument("-n", "--name", help="project name (default: circuit name)")
    p.add_argument("--spacing", type=float, default=1.15, help="layout spread factor (default 1.15)")
    p.add_argument("--no-verify", action="store_true")
    p.set_defaults(fn=_cmd_build)

    p = sub.add_parser("verify", help="check a built schematic against its circuit.json")
    p.add_argument("schematic")
    p.add_argument("--no-render", action="store_true")
    p.set_defaults(fn=_cmd_verify)

    p = sub.add_parser("crosscheck", help="read the drawing again and check circuit.json against it")
    p.add_argument("circuit")
    p.add_argument("--doc", help="the PDF, if the work directory has moved")
    p.set_defaults(fn=_cmd_crosscheck)

    p = sub.add_parser("compare", help="original and KiCad render side by side")
    p.add_argument("schematic")
    p.add_argument("--layout", choices=("side", "stacked"), default="side")
    p.add_argument("--height", type=int, default=900, help="panel height for --layout side (px)")
    p.add_argument("--dpi", type=int, default=220, help="render resolution")
    p.add_argument("--open", action="store_true", help="open the image when done")
    p.set_defaults(fn=_cmd_compare)

    p = sub.add_parser("blocks", help="save each block of a circuit as its own schematic")
    p.add_argument("circuit")
    p.add_argument("-o", "--out", help="directory (default: blocks/ next to the project)")
    p.add_argument("-p", "--profile", default="tht")
    p.add_argument("-b", "--block", action="append", help="only this block (repeatable)")
    p.set_defaults(fn=_cmd_blocks)

    p = sub.add_parser("pinout", help="look up transistor lead order")
    p.add_argument("part", nargs="+")
    p.set_defaults(fn=_cmd_pinout)

    p = sub.add_parser("profiles", help="list footprint profiles")
    p.set_defaults(fn=_cmd_profiles)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
