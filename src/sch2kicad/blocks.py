"""Save each functional block of a circuit as its own schematic.

A block file holds only that block's parts, laid out as in the source, wired
together. Nets that leave the block end in global labels named after the net,
power nets keep their flags, so the file can be opened on its own, pasted into
another design, or later saved into a design-block library.
"""

from __future__ import annotations

import copy
from pathlib import Path

from .build import Builder, safe_name, write_build_json
from .circuit import Circuit, Net, PinRef, Port, load
from .parts import load_profile


def _in(p, refs: set[str]) -> bool:
    """A block names whole parts (``U1``) or single units (``U1A``)."""
    return p.ref in refs or p.key in refs


def block_circuit(c: Circuit, name: str) -> Circuit:
    blk = next(b for b in c.blocks if b.name == name)
    refs = set(blk.parts)
    parts = [copy.deepcopy(p) for p in c.parts if _in(p, refs)]
    inside: dict[str, int] = {}
    outside: dict[str, int] = {}
    for p in c.parts:
        for pin in p.pins.values():
            if pin.net:
                (inside if _in(p, refs) else outside)[pin.net] = 1
        for n in p.supply.values():
            (inside if _in(p, refs) else outside)[n] = 1
    for port in c.ports:
        outside[port.net] = 1
    ports = []
    for net in inside:
        kind = c.net_kind(net)
        if net in outside and kind not in ("power", "ground"):
            # where the net leaves: the source position of the nearest outside pin
            ports.append(Port(name=net, net=net, at=_exit_point(c, refs, net)))
    nets = {k: v for k, v in c.nets.items() if k in inside}
    for n, v in list(nets.items()):
        if v.kind in ("power", "ground"):
            nets[n] = Net(n, v.kind, "flag")    # a block carries its supply as flags
    title = dict(c.title_block)
    title["title"] = f"{c.title_block.get('title') or c.name} — {name}"
    comments = [blk.description] if blk.description else []
    src = c.source.get("file")
    if src:
        comments.append(f"From {src}" + (f" ({c.source['origin']})" if c.source.get("origin") else ""))
    title["comments"] = comments
    texts = [t for t in c.texts if t.at and _near_parts(c, refs, t.at)]
    return Circuit(name=f"{c.name} - {name}", source=c.source, title_block=title, nets=nets, parts=parts,
                   ports=ports, texts=texts, notes=[], blocks=[], uncertain=[u for u in c.uncertain
                                                                           if set(u.refs) & ({p.ref for p in parts} | refs)],
                   path=c.path)


def _exit_point(c: Circuit, refs: set[str], net: str):
    ins = [(p.ref, pin.at) for p in c.parts if _in(p, refs) for pin in p.pins.values()
           if pin.net == net and pin.at]
    outs = [pin.at for p in c.parts if not _in(p, refs) for pin in p.pins.values() if pin.net == net and pin.at]
    outs += [port.at for port in c.ports if port.net == net and port.at]
    if not ins:
        return None
    if not outs:
        return ins[0][1]
    best = min(((a, b) for _, a in ins for b in outs), key=lambda ab: (ab[0][0] - ab[1][0]) ** 2 + (ab[0][1] - ab[1][1]) ** 2)
    a, b = best
    # just past the block's pin, toward the outside part
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) >= abs(dy):
        return (a[0] + (25 if dx > 0 else -25), a[1])
    return (a[0], a[1] + (25 if dy > 0 else -25))


def _near_parts(c: Circuit, refs: set[str], at) -> bool:
    best = None
    for p in c.parts:
        for pin in p.pins.values():
            if pin.at:
                d = (pin.at[0] - at[0]) ** 2 + (pin.at[1] - at[1]) ** 2
                if best is None or d < best[0]:
                    best = (d, p)
    return bool(best and _in(best[1], refs))


def save_blocks(circuit_path: Path, out: Path | None = None, profile: str = "tht",
                only: list[str] | None = None) -> list[Path]:
    c = load(circuit_path)
    prof = load_profile(profile)
    out = out or circuit_path.parent / "blocks"
    paths = []
    for blk in c.blocks:
        if only and blk.name not in only:
            continue
        bc = block_circuit(c, blk.name)
        project = safe_name(f"{c.name} - {blk.name}")
        b = Builder(bc, prof, project, ports_as="labels")
        res = b.run(out / project)
        write_build_json(b, res, circuit_path, out / project,
                         extra={"block": blk.name, "boundary": [p.name for p in bc.ports]})
        paths.append(res.sch)
    return paths
