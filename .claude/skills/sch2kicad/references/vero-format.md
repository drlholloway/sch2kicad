# Stripboard (vero) layouts

A vero layout says which hole each lead goes in, where the strips are cut and
where links jump between strips. It says nothing about how the circuit is drawn.
So a vero conversion is two files:

- `board.json` — what the layout shows, hole by hole. **You never trace a strip.**
  `sch2kicad vero` works the nets out from the strips, cuts and links.
- `circuit.json` — the schematic *you* draw from those nets, with
  `"board": "board.json"`. Verify then proves the schematic connects exactly the
  pins the board does.

## Workflow

1. Ask through-hole or SMD and where the project goes, as for any conversion.
2. Put the layout image beside a new `board.json` and read the board (format below).
   Zoom in: crop the board into quadrants at 3x. Every lead end is a hole.
3. `sch2kicad vero board.json` prints:
   - the nets;
   - **problems**: a lead or link in a cut hole, cut/link counts that differ from
     the ones the layout prints ("17x16, 19 cuts, 8 links"), two jacks on one strip;
   - **warnings**: a lead alone on its strip, two leads in one hole, and where the
     image disagrees with the reading about cuts and links;
   - **notes**: a chip's spare pins, alone on their strips. These are said once,
     and the skeleton marks them NC. A chip pin in a cut hole is a note too: some
     layouts cut a pin's own hole to leave it open (the Memory Lapse's LM386
     pins 1, 7, 8). The same cut under a resistor's lead is a problem.

   It also writes `board.overlay.png`: every part's leads joined by a coloured line
   and labelled, cuts read as blue crosses, links as green lines, wires as magenta
   dots. Read the overlay against the layout, part by part.
4. Fix the reading until there are no problems, and each warning is explained.
5. `sch2kicad vero board.json --skeleton circuit.json` writes every part, pin and
   net, with no positions. Op-amps come back as units with `supply`.
6. Draw the schematic. Give every pin a position on an imaginary sheet, signal left
   to right, and rename the `N_xx` nets to meaningful names (names are free; the
   check compares connections). Keep `IN`, `OUT`, `GND` and `+9V` as they are:
   the off-board wires must keep their names. Add blocks. A generator script with a
   position table is the easy way.
7. `sch2kicad build circuit.json -o ... -p tht`. Besides the netlist, ERC and
   footprints, it prints `board: N pins on the ... layout, the schematic agrees OK`.
   A difference there is a `problem:`. Then make the blocks, as for any conversion.

## board.json

```json
{
  "schema": "sch2kicad/vero@1",
  "name": "Moka Pot Fuzz",
  "source": {"file": "MOKA POT.png", "origin": "Dirtbox Layouts", "image": "MOKA POT.png"},
  "board": {"rows": 16, "cols": 17, "strips": "rows"},
  "stated": {"cuts": 19, "links": 8},
  "cuts": ["A3", "B8", "D2/D3"],
  "links": [["A7", "C7"], ["G8", "P8"]],
  "wires": [{"name": "Input", "at": "J1"}, {"name": "9v", "at": "B17"},
            {"name": "Gain 1", "at": "M1"}],
  "parts": [
    {"ref": "R1", "type": "resistor", "value": "330", "pins": {"1": "L2", "2": "O2"}},
    {"ref": "C3", "type": "capacitor_polarized", "value": "47uF", "pins": {"+": "E2", "-": "G2"}},
    {"ref": "Q2", "type": "pnp", "part": "2N3906", "pins": {"C": "C5", "B": "D5", "E": "E5"}},
    {"ref": "IC1", "type": "opamp_dual", "part": "NE5532",
     "pins": {"1": "K5", "2": "L5", "3": "M5", "4": "N5", "5": "N8", "6": "M8", "7": "L8", "8": "K8"}},
    {"ref": "GAIN", "type": "pot", "value": "B100K", "label": "Gain",
     "pins": {"1": "Gain 1", "2": "Gain 2", "3": "Gain 3"}},
    {"ref": "VOLUME", "type": "pot", "value": "A100K", "label": "Volume",
     "pins": {"1": "Volume 1", "2": {"net": "OUT"}, "3": "Volume 3"}}
  ],
  "notes": ["Volume 2 to Output"]
}
```

- **Holes** are a row letter and a column number, as printed on the board (`D6`;
  rows past Z are `AA`...).
- **`strips`**: `"rows"` when the copper runs along a lettered row (Dirtbox), or
  `"cols"`.
- **`columns`**: `"letters"` when the columns are lettered too (A–R across the top).
  A hole is then row letter then column letter: `"CE"` is row C, column E. In this
  mode `"NC"` is a hole (row N, column C), so write `null` for no connection.
- **`stated`**: the counts the layout prints. Always copy them; they catch a
  missed cut.
- **`cuts`**: `"A3"` is a cut at a hole: the hole joins nothing. `"D2/D3"` is a cut
  between two neighbouring holes on a strip.
- **`links`**: a wire joining two holes. Only its ends connect, so a link drawn
  over cut squares is still one link.
- **`wires`**: off-board wires, at the hole where they are soldered. A label beside
  a strip's end means the end hole on that side (column 1 on the left, the last
  column on the right).
  - `Input`/`Output`/`Ground`/`9v` become `IN`/`OUT`/`GND`/`+9V`.
  - Other names keep their own name (`Gain 3` → `GAIN_3`).
- **Part pins** are a hole, a wire name, `{"net": "OUT"}` for a lug wired straight
  to a jack, or `"NC"`. The pin names are the ones circuit.json uses. Op-amps and
  other ICs give package pin numbers: the skeleton splits an op-amp into units,
  and the check maps them back.
- **Off-board parts** (pots, switches) go in `parts` with their lugs on wire names.
  The layout lists their values ("Gain: 100kB" → `B100K`).
- **A DPDT** is `switch_dpdt` with lugs `"1"`–`"6"`: 1-2-3 down one pole, 4-5-6 down
  the other, 2 and 5 the commons. The skeleton splits it into poles A and B
  (`a`/`com`/`b`), and the check maps them back to lugs. Lugs joined off the board
  ("Switch 1 to Switch 4") share a made-up net: `{"net": "SW_1_4"}` on both.
- **A lug that is also a jack** ("Input to Gain 3" where Gain 3 is soldered to the
  board) is a second wire at the same hole: `{"name": "Input", "at": "E15"}`.

## The designer's schematic

Layout posts often link the schematic they were drawn from (Dirtbox links it
under the layout). Find it. It is an independent check of the whole reading,
and it gives the refs the layout leaves out. Renumber `board.json` to match it.
Where the layout departs from the schematic (an added pulldown, a decoupler left
out), the board is what gets built, so keep the board's version and say so in
`notes`.

When the schematic comes from a build doc with a parts list (PedalPCB), ingest
the doc as usual. Copy its `bom.json` rows into circuit.json's `"bom"`, so verify
checks every value. Parts that live off the board (the LED's resistor at the
footswitch) go in `bom_accept`.

## Reading the Dirtbox style

- Refs are printed only for transistors, diodes and ICs. Take the rest from the
  designer's schematic. Failing that, number resistors and capacitors yourself:
  by column, then by row.
- The transistor labels outside the board (`Q2` above, `Q4`/`Q5` below) name the
  part in that column, nearest the edge. Where two transistors share a column, the
  circuit decides which is which: an NPN's collector and a PNP's emitter face the
  higher voltage.
- Letters on the transistor body (`C B E`, `D G S`) sit on its holes.
- A film cap's red box hides its lead holes. The leads are at the box's two ends,
  centred across it.
- An electrolytic's grey (or green) band is its negative side. Check each one
  against the schematic, and report any disagreement to the user: the Sub Octave
  layout and the Underminer schematic disagree about C4.
- A diode's band is the cathode.
- **Translucent parts** (a cap lying over a cut, the Moka Pot's zener over M4) show what is under
  them. A darker red square seen through one is a cut.
- Cut squares cover a link's bar where they cross it. A link drawn over three cut
  squares is still one link from end to end.

## Other drawing styles

Not every Dirtbox layout is drawn the same way. The Memory Lapse (2024) differs
from the 2021 and 2026 ones:
- the columns are lettered;
- a cut is a red dot on a small white square (grey under a chip);
- links are blue, and two links meeting at a hole are marked with a black dot;
- electrolytics are pink, with the negative side shaded grey.

The 2019 layouts (Octave Drone, Fuzz Overloader, Space Fuzz) draw cuts as pink
squares with a white dot, and film caps as red ovals or boxes. A film cap's lead
hole drawn inside its oval can read as a cut ("the image shows a cut at BF"): look,
and say so.

A layout with no designer schematic often has other people's layouts of the same
circuit (Tagboard Effects, Effects Layouts). They are a cross-check of the topology
and of odd values ("1nF" printed on an electrolytic), not a source of truth: report
the differences and keep what the layout prints until the user says otherwise.

Deadend Fx build docs (Google Drive PDFs, linked under many Dirtbox clones) are
the usual designer's schematic: vector, schematic on page 4 (sometimes 4 and 5),
parts list as an image on page 2, so type the `bom` rows in yourself. Their refs
win. They routinely differ from the layout in the same few ways, all kept as the
board has them and noted: a 1N4001 across the supply where Dirtbox puts a 1N5817
in series; a status-LED zener and resistor ("CLR") off the board; an expression
jack left out; extra trimmers grounded; an op-amp's halves swapped. Dirtbox
numbers a toggle's outer lugs the other way round from Deadend.

The post's comments can show the layout itself is wrong (the Sick Pitch King Jr.:
a link shorting a switch, a cap with a dangling end, two parts missing, the Green
Monster's 47Ω for 470K). Build the layout as drawn, so the board check holds, and
a corrected copy beside it; ask the user which to keep. A stated count can be a
typo too (Rattle Crow "12 cuts" with 11 drawn): count, and say so.

When the user picks the designer's version over the layout (Lane usually does),
keep both: `gen.py` writes the as-drawn `circuit.json` with `"board"`, and
`gen.py final` applies the choices on top and writes `final/circuit.json` with no
`"board"` (it no longer matches the stripboard). Build `final/` as the project to
use, and say in its notes which choices were made and what the layout had.

A vactrol drawn as an LED and an LDR is one part on the board: an `ic` with
`"lib_id": "Isolator:VTL5C"`, `"footprint": "OptoDevice:PerkinElmer_VTL5C"`,
pins 1 (LED −), 2 (LED +), 3 and 4 (the LDR).

Aleph Null's schematics (linked under the Dirtbox posts from PedalPCB forum
threads, madbean threads and Flickr) are KiCad sheets with every ref; their refs
win. The PedalPCB threads need the browser (the site blocks plain downloads), and
the schematic is the post's Flickr image titled "... Schematic": fetch the
photo page and take its `_o` original. Dirtbox's own refs for transistors,
diodes and ICs often number them in a different order: renumber to the schematic.

A dual-gang pot (the Troglodyte's DECAY) is one `ic` part on the house
`2025_PedalDivision_Potentiometers:16mm_Alpha_style_Dual` (footprint
`2025_PedalDivision_Potentiometers:POT_16mm_Dual`), pins 1-3 one gang and 4-6 the
other. Keep it whole in board.json; in circuit.json draw it as units A (1-3) and
B (4-6). An off-board module like the BTDR-3H reverb brick is a part whose pins
are wire names; a wire named like a hole ("RB1") reads as a hole, so rename it
("RB pin 1") and say so in `notes`. A layout that prints no ground wire still has
a ground strip: add a `Ground` wire at the hole of the lug that lands there.

`--skeleton` will not overwrite an existing file without `--force`, and it says
so only in its output: after changing board.json, rewrite the skeleton with
`--force`, or the generator draws from stale nets.

An off-board wire's net keeps its board name in circuit.json (`BYPASS_LED+`, not
a tidier `LED+`): the board check matches wires by name.

FuzzDog (pedalparts.co.uk) build docs are a good designer's schematic for clones
(the Mind Warp is FuzzDog's Brain Bomb). Their parts lists have no type column; a
value with a taper letter is read as a knob.

SabroTone layouts (DIY Layout Creator, e.g. the Engineer's Thumb) print every
part's ref and a full parts list; keep their refs. A cut is a red dot in a white
square with a red outline, links are blue, and a chip's cuts under its body come
through only as GIF dither, so they are "not seen". A resistor's body is drawn
longer than one pitch: its leads are at the holes either side of the body's
centre. Write a lug wired to ground or a jack as `{"net": "GND"}` / `{"net": "OUT"}`;
GND joins the board's ground wire. A chip with a power unit (LM13700) is drawn
with `"supply"`, and the board check maps it to package pins.

Off-board wiring notes under the layout can be wrong. Read the post's comments
for corrections ("Surge 2 connects to Feedback 2, not Feedback 3"), and ask the
user which to follow.

Vintage Japanese circuits (the Shin-ei batch) rarely have a designer schematic with
refs. Look for the factory sheet or a redraw: tonehome.de (photos of base-plate
diagrams), Fuzz Central (fuzzcentral.ssguitar.com), a Deadend Fx or Aion FX build
doc of the same circuit, or another layout site's version read as a second
`board.json` and compared net for net (the OB-28 against SabroTone's). Number
the resistors and capacitors yourself when none of them print refs.
freestompboxes, diystompboxes and the PedalPCB forum are behind bot checks for
curl; use the browser or do without.

- **Positive ground** (germanium PNP, the FY-2 Germanium): the wires are `-9v`
  and `Ground+`, which read as nets `-9V` and `GND`. Set the user's lead order
  with `"pinout"` on each transistor; the layout's body letters give the holes.
- **A 4P4T rotary** (the ME-7's selector, lugs A–D and 1–16) is one `ic` with
  `"part": "4P4T"`, which resolves to `sch2kicad:SW_Rotary_4x4`. Throws 1–16 are
  pins 1–16 (pole A 1–4, B 5–8, C 9–12, D 13–16), and the commons A–D are pins
  17–20, on a 1x20 row of off-board wiring pads. Draw it a pole at a time, as
  units A–D, like the dual-gang pot.
- **A transformer used as a choke** (the 42TM013 in Shin-ei wahs) uses only the
  primary's outer leads. Its other pins sit alone on their strips (a warning),
  and the image check reads the outline's vertical sides as links. Both are
  expected; say so.
- **Off-board notes** in older layouts carry typos ("Fuzz Adjust 2 to Balance 3"
  with no Balance pot; "Fuzz Sw 3 to Fuzz Sw 3"). Work out what each means from
  the circuit, and never wire one that would short a stage (the 6Tr Fuzz Wah's
  "Volume 2 to Fuzz Sw 4" puts the boost's input on its output).
- **The Unverified tag** means nobody has reported a working build. Read such a
  layout with suspicion: the 2024 6Tr Fuzz Wah's wah half has no bias as drawn.

## What the image check can and cannot see

It finds the hole grid from the copper, then looks for:
- **cuts**: a pure red square round a hole that still shows, or a red dot in the
  hole on a pale square, even with a lead drawn across it;
- **links**: a black or blue bar between holes, across the strips.

A cut under a chip's body shows a darker red, and is seen. A cut under a
translucent resistor or a part's lettering can't be seen, and neither can a link
passing under a part. Those come back as "in the reading, but not seen" warnings.
Look, then say so in the report. A slanting link, or a link along a strip, is not
checked.
