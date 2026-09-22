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
C-B-E — and every report says so. The render is placed under the source image in
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
sch2kicad ingest doc.pdf --page 3         # extract it: source.png, tiles/, ocr.json, dots.json
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
RunOffGroove) is in
[reading-schematics.md](.claude/skills/sch2kicad/references/reading-schematics.md).

## Profiles

- **tht** — Pedal Division through-hole parts: `TH_Resistor_1/4w`,
  `TH_Film_Cap`, `TH_Electrolytic_Cap`, `16mm_Alpha_style` pots (knob name in
  `Label`), `TH_NPN`/`TH_PNP` (E-B-C), `TH_JFET`, `TH_Op_amp_Dual`, the 3PDT
  pad array for IN/OUT/GND. A transistor in another lead order gets the stock
  KiCad symbol for that order on the same TO-92 footprint.
- **smd** — 0805 passives, SMD electrolytics, SOT-23 transistors on symbols in
  SOT-23 order (B-E-C), SOIC op-amps; panel parts stay through-hole.

Profiles are TOML in `src/sch2kicad/data/profiles/`; the lead-order table is
`src/sch2kicad/data/pinouts.toml`.

## Status

- **M1 (done):** Chuck's Boneyard PNGs → verified projects + block schematics.
  Ripped Speaker, Bloody Germanium OD and Germanium Filter are regression cases
  in `samples/circuits/` (the tests need no images). The source PNGs/PDFs are
  third-party and stay out of git; drop them in `samples/` to run `compare`.
- **M2:** image-processing cross-check of the reading (wire tracing against
  `circuit.json`), rail alignment.
- **M3:** PedalPCB / Parasit Studio / RunOffGroove PDFs end to end, BOM
  cross-check against the doc's parts list.

## Development

```sh
uv run --group dev pytest        # end-to-end tests need KiCad 10 + the Pedal Division library
```
