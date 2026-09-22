# Image Schematic to KiCAD schematic converter
## From image to schematic in one line

# Introduction

Give it a build document or a schematic image and it turns the circuit into a
KiCad 10 schematic, ready for PCB layout, in the Pedal Division library, and
saves each functional block (tone stack, gain stage, buffer...) as its own
schematic you can copy into other designs.

It is a **Claude Code skill** (`sch2kicad`) backed by a command-line tool of
the same name. Claude reads the schematic; the tool does everything that has
to be exact and then checks the result with KiCad itself.

```
image / PDF ──ingest──▶ tiles + OCR + junction dots ──Claude reads──▶ circuit.json
circuit.json ──build──▶ .kicad_sch/.kicad_pro ──verify (kicad-cli)──▶ READY / NOT READY
circuit.json ──blocks──▶ one schematic per block
```

## What "verified" means

`sch2kicad verify` never trusts the code that wrote the file. It asks KiCad for
the netlist of the finished schematic and compares it, pin for pin, with
`circuit.json`; runs KiCad's ERC; and looks up every footprint in your
`fp-lib-table`. It stays **NOT READY** while any transistor's lead order is
unknown (germanium parts, BF245...) or anything read from the source is marked
uncertain. Through-hole 2N2222/2N2907 are assumed E-B-C — they also ship
C-B-E — and every report says so. When the build doc has a parts list (PedalPCB,
Aion FX), every value KiCad reports is checked against it too: a disagreement or a
listed part missing from the drawing keeps it NOT READY until the reading is fixed
or the difference is accepted. The render is placed under the source image in
`compare.png` for the one check a program can't do: whether the reading was
right.

## Install

Needs KiCad 10, [uv](https://docs.astral.sh/uv/), and optionally `tesseract`
(OCR anchors). Symbols and footprints come from your own KiCad library tables,
so the Pedal Division library must be installed there.

```sh
uv tool install --editable .                          # puts sch2kicad on PATH
ln -s "$PWD/.claude/skills/sch2kicad" ~/.claude/skills/sch2kicad   # the skill, everywhere
```

Then ask Claude Code to convert a schematic, e.g. *"convert
samples/RedMuffin-PedalPCB.pdf to KiCad"*.

## The command line

```sh
sch2kicad ingest doc.pdf                  # list pages, suggest the schematic
sch2kicad ingest doc.pdf --page 3         # extract it: source.png, tiles/, ocr.json, dots.json, bom.json
sch2kicad build circuit.json -o out -p tht    # tht (default) or smd; builds and verifies
sch2kicad verify out/Name.kicad_sch       # re-check a built schematic
sch2kicad compare out/Name.kicad_sch --open   # original and KiCad side by side
sch2kicad blocks circuit.json -o out/blocks   # each block as its own schematic
sch2kicad pinout 2N5089 2N2222 J201       # lead-order table
sch2kicad profiles
```

Exit codes: 0 worked / READY, 1 failed / NOT READY, 2 bad usage.

`circuit.json` is documented in
[.claude/skills/sch2kicad/references/circuit-format.md](.claude/skills/sch2kicad/references/circuit-format.md);
how to read the different sources (Chuck's Boneyard, PedalPCB, Parasit Studio,
Aion FX) is in
[reading-schematics.md](.claude/skills/sch2kicad/references/reading-schematics.md).

## Profiles

- **tht** — Pedal Division through-hole parts: `TH_Resistor_1/4w`,
  `TH_Film_Cap`, `TH_Electrolytic_Cap`, `16mm_Alpha_style` pots (knob name in
  `Label`), `TH_NPN`/`TH_PNP` (E-B-C), `TH_JFET`, `TH_Op_amp_Dual`, the 3PDT
  pad array for IN/OUT/GND. A transistor in another lead order gets the stock
  KiCad symbol for that order on the same TO-92 footprint. Other ICs (TL071,
  LM13700, the 4000-series logic) use the stock KiCad symbol on a DIP package
  picked by pin count. A part KiCad has no symbol for at all — so far the
  CD4517 — comes from `src/sch2kicad/data/symbols/sch2kicad.kicad_sym`, which
  is copied into the built project with a `sym-lib-table` so it still resolves
  when the project is opened.
- **smd** — 0805 passives, SMD electrolytics, SOT-23 transistors on symbols in
  SOT-23 order (B-E-C), SOIC op-amps; panel parts stay through-hole.

Profiles are TOML in `src/sch2kicad/data/profiles/`; the lead-order table is
`src/sch2kicad/data/pinouts.toml`.

## Status

- **M1 (done):** Chuck's Boneyard PNGs → verified projects + block schematics.
  Ripped Speaker, Bloody Germanium OD and Germanium Filter are regression cases
  in `samples/circuits/` (the tests need no images). The source PNGs/PDFs are
  third-party and stay out of git; drop them in `samples/` to run `compare`.
- **M2 (done):** the reading checked against the drawing. `sch2kicad
  crosscheck circuit.json` — and every `verify` — reads the source a second
  time, by its vectors where the page has them and by its pixels otherwise,
  builds a netlist from conductors alone and compares it with the reading. It
  says which nets the drawing splits or joins differently, which rail a flag
  really names, and which pins it could not trace, so the coverage is plain.
  All eight samples agree with their drawings; planted faults — a pin moved to
  another net, a crossing read as a junction, two rails mixed up — are caught.
- **M3 (under way):** build docs end to end, with a cross-check against the doc's
  parts list. Done: PedalPCB Thunder Pipe (63 of 63 parts agree) and Red Muffin
  (60 of 60; 4558 power units on their own filtered rails, TL071 VREF buffer),
  the Aion FX Tri-Vibe (66 of 66; a vector schematic, the LM13700's OTAs and
  buffers) and Parasit Studio's Theremin Fuzz (33 of 34; CD4069/CD4013/CD4046 on
  stock symbols) and their Higgs Particle Phaser (47 of 50; three CD4517 dual
  shift registers on the tool's own symbol), all regression cases. Parts lists
  are read from PedalPCB, Aion FX and Parasit Studio docs.
- **The sheet you get back (done):** a pass over the layout. Every reference,
  value and knob name is placed where nothing else already is — clear of the
  other parts, of their pins and of each other — and any that a wire ends up
  crossing is moved afterwards, since text carries no connection and a wire
  does. An IC's power unit stands beside the amplifier it feeds, in the most
  open spot that still leaves its own rails room for their flags, instead of in
  a row at the bottom of the sheet. Flags reach further and step around
  obstacles before a pin settles for a net label. Notes and voltages keep off
  the drawing. Across the eight samples nothing overlaps anything, and the
  flags that used to fall back to labels are down from 13 to 5.

## Development

```sh
uv run --group dev pytest        # end-to-end tests need KiCad 10 + the Pedal Division library
```
