---
name: sch2kicad
description: Convert a schematic image or a guitar-pedal build document into a KiCad 10 schematic in the user's Pedal Division house style — verified pin for pin, footprints assigned, ready for PCB layout — and save its functional blocks (tone stack, gain stage, buffer...) as their own schematics. Use whenever the user wants to convert, redraw, copy or import a schematic from a PNG/JPG, a PDF build doc (PedalPCB, Aion FX, Parasit Studio, RunOffGroove...) or a Chuck's Boneyard post from the PedalPCB forum into KiCad; wants to copy a circuit block out of a published schematic; wants a schematic from a stripboard / vero / tagboard layout (Dirtbox Layouts, Tagboard Effects); or wants to lay out a PCB for a circuit they only have as an image.
---

# Schematic → KiCad

`sch2kicad` turns a schematic you have read into a KiCad 10 project: the
parts in the Pedal Division library (through-hole or SMD), placed where the
source drew them, wired, with power flags, 3PDT pads and the title block
filled in, then checked by KiCad itself.

**A converted schematic feeds a PCB, and one wrong connection is a dead
board.** The tool proves the KiCad file matches `circuit.json` exactly; only
careful reading makes `circuit.json` match the source. So: read slowly,
check every tile, never guess a pinout or an ambiguous connection, and always
compare the render with the source before saying it is done.

## Getting the tool

`sch2kicad` is installed from the source repo as a uv tool and is on PATH:

```sh
sch2kicad profiles
```

If that fails, reinstall (source lives at `~/Code/schematic-image-to-kicad-converter`):

```sh
uv tool install --editable ~/Code/schematic-image-to-kicad-converter
```

It needs KiCad 10 (`kicad-cli`) and reads the user's own KiCad library
tables, so symbols and footprints resolve exactly as they do in KiCad.
`tesseract` is optional (OCR anchors).

## Workflow

**0. Ask SMD or through-hole** — every conversion, before building. Use
AskUserQuestion with "Through-hole (Recommended)" first. If they skip or
don't care, use through-hole (`-p tht`); SMD is `-p smd` (JLC assembly, SOT-23
transistors). Also confirm where the project goes; default to
`~/PCB/<circuit name>/` only after they agree — that folder is their real
workspace.

**1. Ingest.** For a PDF, list the pages first and look at the thumbnail of
the likely page before choosing:

```sh
sch2kicad ingest build-doc.pdf            # lists pages, suggests one
sch2kicad ingest build-doc.pdf --page 3   # extracts that schematic
sch2kicad ingest schematic.png            # images go straight through
```

The work directory (`<input>.sch2kicad/`, or `-o`) holds `source.png`,
`grid.png`, zoomed `tiles/`, `ocr.json`, `dots.json`, `text.json` (vector
PDFs: exact text) and a `circuit.json` skeleton. Every coordinate is a pixel
of `source.png`. Ingest turns a sideways drawing upright by reading its text
("turned 90° clockwise..."); glance at `grid.png` to confirm before reading.
A circuit drawn sideways on an upright page (header upright, circuit on its side)
fools that: re-run with `--turn 270` (its text reads down the page) or `--turn 90`.
A sheet drawn light on dark is inverted onto white.
When the doc prints its parts list as a table (PedalPCB, Aion FX), ingest
reads it into `bom.json` and the skeleton's `"bom"`. Keep it there: verify
checks the drawing against it. If `circuit.json` already existed, copy the
rows in yourself.

**2. Read.** Look at `grid.png` for the whole circuit, then read **every
tile** (they overlap — a part on an edge is whole in a neighbour). Use
`dots.json` for junctions (a dot list is ground truth: a crossing with no dot
is not a connection) and `ocr.json`/`text.json` to pin references and values
to positions. Write `circuit.json` — format in
[references/circuit-format.md](references/circuit-format.md), reading
checklist and per-source quirks in
[references/reading-schematics.md](references/reading-schematics.md). Name
nets that leave a block meaningfully (`TONE_IN`, not `N7`): they become the
labels in block files.

**3. Build and verify.** One command does both; exit 0 means READY:

```sh
sch2kicad build circuit.json -o ~/PCB/Name -p tht
```

```
Ripped Speaker - cb mod.kicad_sch: 44 parts, 31 nets
  netlist:    matches circuit.json pin for pin  OK
  ERC:        0 errors, 0 warnings
  footprints: all resolve  OK
notes:
  note: Q2: 2N2222 assumed E-B-C for through-hole — TO-92 parts also come as C-B-E ...
READY
```

`problem:` lines block READY; `warning:`/`note:` lines do not, but say them
back. Verify re-reads KiCad's own netlist and ERC — it never trusts the
builder. `sch2kicad verify <file.kicad_sch>` re-runs it alone.

With a `"bom"` in `circuit.json`, verify also compares every value KiCad
reports with the parts list (`parts list: 63 of 63 agree with the drawing`).
A value that differs, or a listed part missing from the drawing, is a
`problem:`. Usually it is a misread, so zoom in and read that part again. If
the drawing and the list really disagree, ask the user which is right, then
fix the reading or record the answer in `"bom_accept"`
(`{"R5": "drawing is right, the list has a typo"}`). Knobs named differently
(SUSTAIN on the drawing, GAIN in the list) and parts the list leaves out (a
status LED) are warnings: pass them on.

Verify also **reads the drawing again** and checks the reading against it —
the PDF's own vectors where the page has them, the pixels otherwise. It joins
wires the way a schematic joins them and compares the nets that fall out with
`circuit.json`, so a crossing read as a junction, a pin on the wrong net or two
rails mixed up show up as a `warning:` naming the pins:

```
  drawing:    132 of 168 pins traced through 468 conductors, the reading agrees  OK
```

Every line it prints is worth opening the drawing for. A **split** ("X is 2
separate things in the drawing") means the reading joins what the drawing keeps
apart — a short on the board; **joined** is the reverse — a connection the board
would not have. Either one is fatal, and either can also be the checker being
fooled, so go and look. A `note:` about pin positions a few pixels out, or pins
with no wire under them, is not a fault — but the second says which part of the
reading nothing checked, so read those by eye. `sch2kicad crosscheck circuit.json` runs it alone. It needs the work
directory (`ingest.json`, `source.png`) beside `circuit.json`, so read and build
from the work copy.

**4. Look at it.** Read `sch2kicad/compare.png` (source on top, KiCad below,
source cropped to the circuit) and go part by part: every value, every
polarity mark, every junction. Values, polarity and part numbers are yours
alone to check: nothing in verify knows that you read 4.7K as 47K. Fix
`circuit.json` and rebuild until the two match.

The sheet places its own text: references, values and knob names go where
nothing else is, and any a wire crosses is moved after routing. A chip's power
unit is eased off its neighbours until both its rails have room for a flag —
it is the one part worth moving, since where a supply box sits carries no
meaning and a source often draws it far smaller than KiCad's symbol of it. A
rail's flag belongs to the node and not to each pin on it, so pins of one rail
a few millimetres apart are wired together under a single symbol, and a pin
with no room of its own reaches for a flag the rail already has rather than
settle for a net label. Two things the build will tell you it could not do. `note: joined with net labels
instead of wires` names a net with no clean route — correct, but it reads
worse; `note: drawn touching` names parts left with no daylight between them,
which you can pull apart in KiCad in seconds. Wires follow the drawing's own: where the source is beside the reading, each net is routed along the wires the drawing runs for it (a collector's column stays one column), every pin is tried as the tree's start, and a step of a grid or two between bends pays extra. Before routing, parts step up to two grid along an axis to put back in line the pins of one net the drawing lines up (KiCad's symbols are rarely the drawn length), and flags the drawing stands on one baseline — Chuck's row of grounds — end level. A jog still left is usually a sheet whose symbols are far bigger than drawn (Abductor II). To show the user, make a high-resolution side-by-side and open it on
their screen (images you read are not shown to them in the terminal):

```sh
sch2kicad compare ~/PCB/Name/Name.kicad_sch --open      # --layout stacked for very wide sheets
```

**5. Blocks.** Each block in `circuit.json` becomes its own schematic: parts,
internal wiring, flags, and a global label for every net that leaves it:

```sh
sch2kicad blocks circuit.json -o ~/PCB/Name/blocks -p tht
```

Blocks stay as standalone schematics for now — do not write into the user's
design-block libraries (`CryptidEffectsDesignBlocks` etc.) unless asked.
`sch2kicad blocks` doesn't verify; run `sch2kicad verify` on each block file. A
block is cut from a larger circuit, so its boundary nets are driven from
outside; verify says so in a note rather than failing on it.

**6. Report.** READY or not, what was asked or flagged, where the files are.
Remind them: in KiCad, *Update PCB from Schematic* with **Group footprints
based on symbol groups** brings the footprints in grouped by block.

## Stripboard (vero) layouts

A vero layout shows holes, cuts and links, not a schematic. Read it into
`board.json`, hole by hole. `sch2kicad vero board.json` works the nets out of the
strips and checks the cuts and links against the image. It also checks the counts
the layout prints, and writes an overlay of the reading to compare with the
layout. `--skeleton circuit.json` then writes every part and net. You draw the
schematic by giving each pin a position, signal left to right. The build proves
it connects exactly what the board does (`board: ... the schematic agrees OK`).
Format, workflow and the Dirtbox style:
[references/vero-format.md](references/vero-format.md).

## Rules that are not negotiable

- **Never guess a transistor pinout.** `sch2kicad pinout 2N5089 J201 MP38A`
  shows what the table knows. Unknown or ambiguous parts (germanium, BF245...)
  — ask the user what their parts are, then set `"pinout"` on the part. The
  build refuses READY until every pinout is known. One standing exception, the
  user's call: through-hole **2N2222/2N2907 are assumed E-B-C** (they also ship
  C-B-E); the report notes it on every build — pass that note on.
- **Never smooth over an ambiguous read.** Put it in `"uncertain"` with its
  position; the sheet shows a red `?` there and a CHECK line, and verify stays
  NOT READY until the user has looked and you remove it.
- **Notes can change the circuit.** "Q2 & Q3 are installed upside-down",
  "OK to sub other diodes" — copy notes into `"notes"`, and act on the ones
  that alter a connection or a pinout (ask if unsure).
- **Values stay as drawn.** Keep `4.7K`/`3.3nF`/`A100K` exactly as the source
  writes them; the taper letter is part of a pot's value. One exception, the
  user's call: pots come in 1K, 2K, 5K, 10K, 20K, 25K, 50K, 100K, 250K, 500K, 1M
  and 2M (every taper), and the build notes any other value with the nearest
  stock one. Propose that value; Lane moved the Abductor's B47k DEPTH to B50k.
- **Source schematics are someone's work.** Keep conversions private unless
  the user says otherwise; some docs ask not to be reposted.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | built and READY (or: command worked) |
| 1 | built but NOT READY, or the work failed |
| 2 | the command was typed wrong |

## Profiles

`tht` (default): Pedal Division TH parts — `TH_Resistor_1/4w`, `TH_Film_Cap`,
`TH_Electrolytic_Cap`, `16mm_Alpha_style` pots with the knob name in `Label`,
`TH_NPN`/`TH_PNP` (E-B-C), `TH_JFET` (D-S-G), `TH_Op_amp_Dual`,
`TH_Op_amp_Quad` (V+ on 4, V- on 11), the 3PDT pad array for IN/OUT/GND and
loose pads for everything else. The array is the plain `3pdt_Switch_Connectors`
(IN, GND, SW, OUT; the jacks wire to the footswitch off the board) unless the
drawing shows its jacks — a `JI`/`JO` pad or a jack's own ground pad (`GNDJ1`) —
when it is `3pdt_Switch_Connectors+Jacks` with the jacks on `JIN`/`JOUT` sockets
(Lane, Colonel Angus). `"jacks": true/false` in circuit.json overrides the guess. A circuit with no true bypass (`"true_bypass":
false`) brings its jacks in on the house `JIN`/`JOUT` sockets instead, tip to
IN/OUT and the other pin to ground. A transistor whose lead order is not the house
order gets the stock KiCad symbol for its order on the same TO-92 footprint, so
the board is right without bending leads.

**House style** (Lane's hand edits of the Bulgarian Fuzz and Memory Lapse; both
profiles). Write `circuit.json` as the drawing names things; the build renames
on the sheet only, and says what it changed in a `house references:` note:

- knobs become `VR1`, `VR2`... and toggles `SW1`, `SW2`..., in `circuit.json` order,
  the knob name kept in the visible `Label`;
- the supply is numbered from 100: `D100` the reverse-polarity diode, `D101` the status
  LED, `R100` its resistor, then the supply's series parts, electrolytics and regulator
  (`C100`, `C101`, `REG100`) in the order the supply runs through them;
- with a 3PDT, a sheet with no status LED gets one (4K7 from the protected rail to a
  RED LED on the SW lug, said in a note) — unless an LED pad is fed through a resistor
  from a rail (Chuck's RLED to LED+): that LED is off the board and RLED is R100. A
  pad named `CTRL` is the SW lug; loose ground pads go, and `JIN`/`JOUT` sockets are
  wired to the array's JI and JO lugs;
- the corner: the LED row (rail, R100, D101, SW and GND lugs) above the power row
  (POWER socket, D100, rail caps, regulator), the jack sockets under the socket
  (each one's ground symbol lying along its pin 2, the one ground that lies down),
  everything else below. A drawn sheet's supply moves there only when it reaches the
  circuit by flags; one drawn as a wire into the circuit stays as drawn, and the jacks
  stand in a column under the drawing's left side.

The parts-list check still finds a renamed part by the drawing's ref (`R8` → `R100`).

`smd`: 0805 passives, SMD electrolytics, SOT-23 transistors on B-E-C / D-S-G /
G-S-D symbols (SOT-23 order, not TO-92 order), SOIC op-amps. Panel parts stay
through-hole. Profiles live in `src/sch2kicad/data/profiles/*.toml`.
