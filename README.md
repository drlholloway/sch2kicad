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
  `Label`), `TH_NPN`/`TH_PNP` (E-B-C), `TH_JFET`, `TH_Op_amp_Dual`,
  `TH_Op_amp_Quad` (V+ on 4, V- on 11 — the TL074/LM324 order), the 3PDT
  pad array for IN/OUT/GND — or, when the circuit has no true bypass
  (`"true_bypass": false`: a JFET-switched send like the Abductor II's, where a
  3PDT's pads would only strand a switch nobody fits), the house `JIN`/`JOUT` jack
  sockets, tip to the signal and the other pin to ground. A transistor in
  another lead order gets the stock KiCad symbol for that order on the same
  TO-92 footprint. Other ICs (TL071,
  the 4000-series logic) use the stock KiCad symbol on a DIP package picked by
  pin count. Four come from the tool's own library
  `src/sch2kicad/data/symbols/sch2kicad.kicad_sym`, copied into the built
  project with a `sym-lib-table` so they still resolve when it is opened: the
  **CD4517**, the **NE570** compandor and the **CD4007** (its two pairs and
  inverter as three units), which KiCad has no symbols for, and the **LM13700**, which it
  does — but KiCad's brings the amplifier bias current out beside the output,
  so the wire from the LFO has to cross the signal it controls. Ours takes it
  out of the base of the triangle, the way the datasheet draws it, and numbers
  its units in datasheet order, so the drawing's IC4A is unit A.
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
  really names, and which pins it could not trace, so the coverage is plain. A
  part's own ink is not a conductor: an outline is dropped, and a body a run of
  pixels goes straight through is cut out from between the two pins it joins —
  which is what lets the check work on a hand-drawn sheet, where every part is a
  hollow shape whose boundary touches both its leads. A part drawn *round* its
  terminals is known by its shape rather than by any measure of reach: four runs
  of ink closing a rectangle with the pins inside are an outline, and dropping
  them is what keeps a switch's own box from joining everything it encloses. A
  body can also come back as **one run broken in two**: a capacitor whose plates
  stand a pixel apart reads as two leads facing each other across a hairline
  between its pins, which the tracer would bridge, and is cut at its pins like any
  other run through a body — but only a hairline, so a pin read out along its
  wire never has that wire taken for body. A
  part is judged one place at a time, since a sheet may draw its supply pins
  somewhere else entirely and the page between the two is nobody's body. What
  counts as ink is read off the page as well: the wires' own colour, picked by
  which colour runs furthest in a straight line, and measured across the pair of
  pixels a line thinner than one of them is split over. A crossing the drawing
  marks by **breaking** the wire that is jumped is read as a crossing, not welded
  into a junction: where two wires really meet the ink is continuous, so the gap
  is evidence. A run that **stops just past** the wire it crosses is the same
  evidence read the other way — at a real crossing nothing ends, so ink carried a
  stroke or two beyond has been drawn to the far rim of a junction dot. A pin
  with no wire under it is reported with **how far the nearest ink is**, because
  that is what says what to do about it: a wire eleven pixels away is a pin to
  move, and the drawing is where the answer comes from. Where a whole part has no
  wire in the space it would stand in, and none anywhere near it, the drawing
  simply does not draw that part — a status LED the builder adds, the spare gate
  of a quad nobody used — and it is named and taken off the tally rather than
  counted as a pin the check could not read. A rail's **name** is the other way a
  drawing joins two pins, and which wire it belongs to is settled by the blank
  paper between them rather than the distance: a flag stands between its name and
  its stem, and where the sheet draws symbols in another colour that flag is not
  ink the tracer keeps, so a distance is measured across a symbol taken out of
  the picture. A wire drawn on the **slant** is read as one line, so two that
  cross in an X with no dot stay apart, as on a vector page. A sheet drawn light
  on dark is inverted onto white at ingest, and a circuit set sideways on an
  upright page is turned with `ingest --turn`, its vector segments turned with it.
  A rail's name counts only as the reading spells it or in capitals, so OCR
  making "Vr" of a rotated part number cannot weld two rails.
  On a sheet drawn in more than one colour, the junction dots say which is the
  wires': they are drawn in it (an Eagle sheet's red parts outran its green wires).
  A PDF that stores its image mirrored and flips it back on the page is flipped
  back at ingest, so digits read the right way and a DIP's notch sits on top.
  A word written inside a chip, between its own pins, is that pin's name and not a
  rail's flag: DOD's FX96 writes `GND` inside both BBD chips, whose GND pin runs
  on a filtered +9V. A crossing stepped over with a little **arc** is a hop as
  well, found by the arc itself — a semicircle of ink with bare paper inside,
  from a wire end over a wire running straight through to a conductor as far
  past — and a sheet that draws its hops that way has said what a plain crossing
  is: a join (the Gnomeratron VTF). Where an Eagle sheet joins two parts with no
  wire at all — a green junction dot between two red leads, or a green stub
  shorter than any wire the tracer takes — the dot or the stub is a conductor of
  its own, as long as ink of another colour leaves it: on a sheet in one colour a
  part's leads are wire ink already, and a "dot" with no wire is lettering (the
  Abductor II). All twenty-nine sheets,
  from eighteen publishers, are checked against their drawings, and **3699 of
  the 3702 pins they draw are traced** — two of the other three are rheostat
  wipers the sheet ties inside the part and never draws, the third a lead that
  runs inside a pot's reach and touches none of its pins: twenty-five agree
  outright, and nine disagreements stand on the other four — every one of them the drawing's own
  doing, never a join the tracer invented: an off-page connector drawn twice, a
  jack whose normalling lug is joined until a plug lifts it, a pair of pads joined
  only by a label, a battery with its wire drawn straight through the cells. They are recorded
  rather than silenced by moving a pin. Planted faults — a pin moved to another
  net, a crossing read as a junction, two rails mixed up, a resistor shorted end
  to end, a ground pin read as the supply — are caught.
- **M3 (done):** build docs end to end, with a cross-check against the doc's
  parts list — every publisher seen so far reads. PedalPCB Thunder Pipe (63 of
  63 parts agree) and Red Muffin (60 of 60; 4558 power units on their own
  filtered rails, TL071 VREF buffer),
  the Aion FX Tri-Vibe (66 of 66; a vector schematic, the LM13700's OTAs and
  buffers) and Parasit Studio's Theremin Fuzz (33 of 34; CD4069/CD4013/CD4046 on
  stock symbols) and their Higgs Particle Phaser (47 of 50; three CD4517 dual
  shift registers on the tool's own symbol), all regression cases. Parts lists
  are read from PedalPCB, Aion FX and Parasit Studio docs. A taper written as a
  word (`1M LOG`, `2M lin`) reads as its letter, so Deadastronaut's list checks
  its knobs too. A pot drawn at a value pots do not come in — they come in 1K, 2K,
  5K, 10K, 20K, 25K, 50K, 100K, 250K, 500K, 1M and 2M, every taper — gets a note
  naming the nearest one, its taper kept (the Abductor's 47k DEPTH, built B50k).
- **The sheet you get back (done):** a pass over the layout. Every reference,
  value and knob name is placed where nothing else already is — clear of the
  other parts, of their pins and of each other — and any that a wire ends up
  crossing is moved afterwards, since text carries no connection and a wire
  does. An IC's power unit stands beside the amplifier it feeds, in the most
  open spot that still leaves its own rails room for their flags, instead of in
  a row at the bottom of the sheet. Flags reach further and step around
  obstacles before a pin settles for a net label. Notes and voltages keep off
  the drawing. Across all thirteen sheets nothing overlaps anything, and the
  flags that used to fall back to labels are down from 13 to 5.
- **The supply column (done):** where a source draws a chip's supply as a box a
  tenth the size of KiCad's symbol of it, a tidy drawn row lands as a staircase
  of touching boxes, each ground pin pointing into the next box's body with
  nowhere to hang a flag. A power unit is now eased off its neighbours until
  both rails have room — the one part worth moving, since where a supply box
  sits carries no meaning — by the smallest step that frees it, so the rail
  feeding the whole column still routes as wire. The Higgs' three grounded
  supply pins get flags instead of labels, and no sample is left drawn
  touching. A rail also has to have room *on the paper* now — the Tri-Vibe had
  been standing IC2's power unit over the top edge, with its VA flag off the
  sheet entirely, and nothing measured the page because nothing knew to.
- **One flag to a node (done):** a rail's flag belongs to the node, not to each
  pin on it. Pins of one rail within 7.62 mm are joined by a wire and share a
  single symbol — the Tri-Vibe had VB arrows standing in step beside each other
  on an op-amp's output and the cap across it. A pin with no room of its own
  reaches for a flag the rail already has instead of settling for a net label,
  and where a pin's lead points straight into a neighbour's body, the wire may
  turn where it stands rather than give up. **Net-label fallbacks went 4 to 0
  here, and no sheet has an ERC warning.** One label has come back since, on the
  Phuncgnosis, where Escobedo stacks three LEDs in 43 px of a 569 px sheet and
  no arrangement of three KiCad symbols fits; the test names it, so a second one
  is still a regression. Two bugs came out of it: a flag stub could step aside
  and come back on the same axis, drawing one wire exactly over another —
  invisible on the sheet, and harmless until the wires of a rail started going
  through the same normaliser as every other net, which keeps one of the pair
  and left the other's end in the air; and a rail's wires had never gone
  through that normaliser at all, so a join onto one depended on KiCad
  inferring a T rather than on a junction dot.

- **Which switch to order (done):** a toggle's action — on/on, on/off/on,
  on/on/on — is not in the netlist. `com`/`a`/`b` is identical for all three,
  and they are three different parts, so nothing downstream can work out which
  one the circuit wants and the schematic has to say it: in the value, where it
  shows on the sheet (`DPDT on/on/on`), and in the description, where a BOM
  reads from. Spellings fold, because a publisher writes one switch two ways in
  one row — Aion's Tri-Vibe list says "SPDT cntr off" in the VALUE column and
  "on-off-on" in the TYPE column — and the parts-list check reads both, so
  stating the action canonically does not cost a match. A switch nobody marked
  gets a note rather than a guess: a wrong action loses a setting instead of
  killing a board, so it does not block READY the way an unknown lead order
  does. An `on/on/on` on a single-pole switch is a problem, though, since one
  pole cannot make three different live positions. An on/off toggle is built on
  the house mini SPDT with one throw left unused, since the library has no SPST
  and KiCad's SW_SPST lands on a throw and the middle lug. Every toggle in the samples
  states one now — the last three were settled by reading the sheets rather
  than by assuming, and two of them could have taken a centre-off for a third
  voice, which is a change to the circuit and so was the owner's call, not the
  converter's. The one switch left unmarked is the synthetic DPDT fixture,
  which keeps that path exercised. The Disorder's DPDT is the
  case that matters: its two poles are chained so the toggle picks one of three
  signals, which a plain on/on cannot do, and until now the only record of that
  was a note in `circuit.json`.

- **The rotary the sheet draws (done):** a 2P3T is a part now, not three pads
  with the wiring in a note. `switch_2p3t` takes one pole per unit — `com` plus
  `1`, `2`, `3` in the order the shaft turns, so unlike a toggle's throws they
  are **not** interchangeable, and a throw the drawing leaves dangling stays
  `NC` instead of being tidied onto the nearest bus. It lands on the house
  `2P4T.MINI` footprint, which keeps all ten holes on purpose: the position
  count on these switches is set by a stop ring, not by the contacts, so the
  part has ten pins whether the circuit uses the fourth position or not and an
  eight-hole footprint would not accept it. The Gargler was the case — its two
  poles are chained to give filter 1, both, filter 2 — and converting it took
  the cross-check from 97 to 100 of its 103 pins, because the switch's own eight
  terminals are now read off the ink rather than left off the sheet. One lesson
  from drawing the symbol: the sheets put a three-position rotary's wiper level
  with the **middle** throw, and a symbol that agrees puts the part where the
  drawing puts it. Getting that wrong left a pin with no room to route and cost
  a net label; fixing the fidelity fixed the layout, with no pin moved.

## Development

```sh
uv run --group dev pytest        # end-to-end tests need KiCad 10 + the Pedal Division library
```
