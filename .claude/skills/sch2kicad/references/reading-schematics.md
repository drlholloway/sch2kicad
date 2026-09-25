# Reading a schematic into circuit.json

## Order of work

1. `grid.png` first: find the signal path, the rail(s), the power section, ports,
   title block, notes. Decide the blocks now — pedal circuits are made of
   recognisable stages (input buffer, gain stage, clipping, tone stack, volume,
   output buffer, LFO, power/filtering/charge pump).
2. Then each tile, left to right, top to bottom. For every part: ref, value/part,
   type, and each pin's net and position. Tiles are 2–3× zoom with a 25 px grid
   (5 px minor); labels are source pixels.
3. Trace every net from end to end before moving on. At each junction dot, list
   everything that meets there. Check `dots.json` agrees with what you see.
4. Count: every part in the drawing is in `parts`; every ref in the parts list /
   BOM appears once; every pin has a net or `"NC"`.
5. Build, then compare `compare.png` part by part.
6. Read the `drawing:` line of the report, and open the drawing again at every
   pin it names. It traces the source's own wires and compares the nets with
   yours, so it catches what the eye slides over: a crossing taken for a
   junction, a pin on its neighbour's net, VA where the flag says VB. It also
   says how many pins it could trace — the rest is on you.

**Pin positions are part of the reading, not decoration.** Put each one where
the wire actually meets the part, to within a few pixels: the layout follows
them, and so does the cross-check, which cannot tell a wrongly placed pin from
a wrongly wired one. Where a symbol's own pin line is drawn, the wire meets it
at the far end of that line, not at the body.

## Connections

- **Dot = joined.** A junction dot joins everything that touches it.
- **Plain crossing = not joined** (no dot). Check: would joining it make nonsense —
  a pot wiper shorted to the rail, a base tied to the supply? Then it's a crossing.
- **T without a dot** is normally joined; say so in `uncertain` if the style is odd.
- **Hop-overs** (a little arc where one wire jumps another, Aion FX) are crossings.
- **Same-named labels/flags join** (Vref, VA, VB, +9V, SWGND) even with no wire.
- A wire ending on a part's body with no dot is a pin; one passing near it is not.

## Parts

- **Pots**: find which end is `cw` — a "cw" mark, lug numbers (1 = ccw, 3 = cw), or
  function (volume: cw end to the signal; gain as a variable resistor: cw is the
  end that raises gain). Say which in `uncertain` if nothing marks it. The wiper is
  the arrow. A pot wired as a variable resistor has wiper and one end joined — draw
  that as drawn (two pins on one net), don't drop a pin.
- **Electrolytics**: the `+` mark or the straight plate is `+`; the curved plate is
  `-`. Unmarked non-polar caps are `capacitor`.
- **Diodes**: the bar is the cathode `K`. Two diodes back to back are two parts.
- **BJTs**: the arrow is on the emitter — pointing out = NPN, in = PNP. Base is the
  bar in the middle. Chuck's drawings put the collector up for NPN.
- **FETs**: arrow on the gate/channel; `BS170`, `2N7000` are N-MOSFETs; `J201`,
  `2N5457` N-JFETs.
- **Op-amps**: `+`/`-` inputs as drawn (inverting input is often on top), output at
  the point. Units: `U1A`/`U1B`, `IC1.1`/`IC1.2`, `IC1A`. Power pins (4/8) go in
  `supply`. Count the sections on the ref before choosing the type: two is
  `opamp_dual` (power 8/4), three or four on one ref is `opamp_quad` (units `A`–`D`,
  power 4/11). A sheet that says only "typical opamp" without a part number leaves
  the packaging open — ask which chip before spreading sections across one ref.
- **Values**: keep the source's spelling. Watch 4.7K vs 47K, 10nF vs 10uF, 1M vs
  1K, 330 vs 33 — zoom in until sure; a value you can't read is `uncertain`.
- **Read the emitter from the arrow, never from position.** A transistor drawn
  "upside-down" has its arrow on the top lead; that lead is `E`. When a note says
  parts are installed upside-down, check whether the drawing already shows it (the
  Germanium Filter does) — if so, as drawn *is* as installed; say that on the sheet
  so nobody turns the parts around a second time.
- **IC pins by number, not by where they are drawn.** Chuck's LM386 puts pins 1/8
  on top and 7 at the bottom; the house symbol doesn't. Give every IC pin its
  number and position; unconnected pins (`"NC"`) don't steer the orientation.
- **Pots as variable resistors** (wiper tied to one end, e.g. GAIN/DRIVE in an
  op-amp's feedback) are common: two pins on one net, keep all three.
- **Toggle switches**: the pivot dot is the common; the open circles are the throws.
  Read its **action** too and put it in `action` — `on/on`, `on/off/on`, `on/on/on`
  — because the netlist is identical for all three and they are different parts to
  order. Where to find it: written by the switch (`on-on-on`, `cntr off`), in the
  parts list (Aion puts it in the type column, "Toggle switch, SPDT on-off-on"), in
  the notes, or **in how the drawing uses it**. A DPDT whose two poles are chained
  so the circuit has a third setting is an `on/on/on`: a plain on/on has only two
  positions and cannot do it. That is how the Disorder's is read — pole B picks one
  of two signals, pole A picks that or a third, which is three settings from one
  toggle. Left out, the build says so and stays READY, so a switch nobody marked is
  not a blocker; a switch marked wrong is.
- **Rotary switches** (`switch_2p3t`): one pole per `unit`, `com` plus `1`/`2`/`3` in the
  order the shaft turns. Read the wiper's pivot as `com` and the throws in sweep order;
  a throw with no wire on it is `"NC"`, not a tidy-up onto the nearest bus. The Gargler
  is the worked example: its two poles are chained so the three positions give filter 1,
  both, filter 2.
- **Annotations are not nets**: `+5.20V`, `+4.50V` near a node are measured
  voltages → `texts` with `"kind": "voltage"`. `+8.72V` written over a rail is
  also an annotation (the rail is still whatever feeds it).

## Source styles seen so far

**Chuck's Boneyard (PedalPCB forum)** — TinyCAD PNGs, ~1400 px wide, very clean.
Junction dots everywhere; plain crossings are not joined. Pots carry a `cw` mark
at one end and a bold knob name (FUZZ, LEVEL) nearby → `label`. Measured
voltages in plain text next to nodes. Off-board connections are small squares
with names (INPUT, OUT, OUTPUT, +9V, POWER, LED+) — but the same square also
marks net labels: `Vref`, `Vcc` next to a part are rails (power nets drawn as
flags), not off-board connections. Each ground is its own
symbol. The rail is usually drawn as a line across the top → `"draw": "wire"`.
Typical power section: 1N5817 in series from +9V, a 10–22 Ω resistor and a
100 µF cap; RLED from the protected node to LED+. **Read the NOTES block** — it
can override the drawing (e.g. "Q2 & Q3 installed upside-down (emitter &
collector swapped)": draw the connection as the note says and flag it). Title
block: Title / Author / company line / File / Revision / Date.

**PedalPCB build docs** — Affinity Publisher PDFs; the schematic is a large
embedded image (3000+ px) on the "Schematic Diagram" page (usually 3 or 4),
sometimes placed turned on the page — the image itself is upright. Faint "PedalPCB"
watermark logos behind the drawing — ignore them. Pots show lugs 1/2/3 (use them
for cw/ccw; which end is up varies pot to pot) and are named by their knob:
`PRE`, `TONE` is the ref. Transistors show 1/2/3 = E/B/C; electrolytics show `+`;
resistors and film caps carry no pin numbers. Op-amps are drawn as units `IC1.1`,
`IC1.2` with a separate power unit (`IC1` with pins 8/4). Give that its own entry
(unit `C`, positioned `supply`) so it lands beside its filter. A single TL071 is
drawn as `IC3.1` plus `IC3.2`, but it is one entry. Red Muffin gives each 4558 its
own 100R/100u/100n rail (`IC1_V+`, a power net drawn as wire), and buffers VREF
with the TL071; verify notes that VREF is driven by an output rather than a power
source. Rails are flags: `VCC`, `VREF`, ground. Pads are small circles:
`IN`/`OUT` each with a ground pad beside it (the jack sleeve), `SW` and `GND`
for the footswitch, `+`/`-` for the DC jack. The wiring page shows IN/GND/SW/OUT
going to the 3PDT, the same as the house pad array, so make the ports `IN`, `OUT`,
`SW`, `+`, `-`. Leave out the sleeve pads and the GND pad (they are the array's
GND pad) and say so in a note. The status LED (`LED`, with its resistor to `SW`)
sits on the board. The parts list (pages 2–3) is a text table that ingest reads
into `bom`. Its knob names can differ from the drawing's (Thunder Pipe: SUSTAIN
and VOLUME on the drawing, GAIN and LEVEL in the list), and the status LED isn't
in it.

**Parasit Studio** — OpenOffice "Build Document" PDFs, schematic on the last
page, embedded as a low-resolution image **rotated 90° inside the image** —
ingest turns it upright; confirm on `grid.png`. Dense CD40xx logic at ~2000 px:
read tiles slowly at 4-6x, pin numbers matter. Their newer sheets are drawn in
KiCad (the Higgs, 2024), and those render cleanly enough that `dots.json` is
worth having — on the Higgs it found all 40 junctions and nothing else, so it
is a real check on a reading: list the dots, account for every one, and be sure
no junction you drew is missing from the list. A dangling wire end shows as a
small open square (an unconnected output; `"NC"` in `circuit.json`). Two kinds
of net marker: octagons (`IN`, `OUT`, `+9V`, `VA` — a crossed
octagon is a rail) and arrows with a name beside a pin (`INPUT`, `GND`,
`OUTPUT`, or an auto-generated `Net 10`). The same node can carry both
spellings — the Theremin Fuzz has `IN`/`INPUT` and `OUT`/`OUTPUT` — so make
each one net and say so in a note. A `J1` connector block is the board's
off-board pads (`IN`, `G`, `9v`, `OUT`) → ports. CD40xx chips are drawn as
units (`U1.1`-`U1.6`, `U2.1`) plus a separate box with the supply pins (`VDD`,
`VSS`): give that box its own `ic` entry with only `supply`, positioned, and it
is placed where it is drawn. Unit numbers follow the datasheet, so they match
KiCad's; a part number like `CD4069UBE` finds the stock `4xxx:4069` by itself.
`CD4517` has no KiCad symbol and comes from the tool's own library instead
(`sch2kicad:CD4517`, halves A/B and a supply unit C, pins as TI numbers them).
Unused gates are drawn with their inputs grounded — keep them, a floating CMOS
input is a fault. Boards carry **build options**: pads (`A`, `B`, `C`, `X`) for
links or an off-board switch, a second set of pads for another pot, a resistor
to leave out. The doc's "Build Options" page says what each build needs; ask
the user which one to draw. The status LED and its resistor are wired off the
board. The parts list is a grid of ref/value column pairs under bold headings
("Capacitors", "Resistors", "IC's"...), which ingest reads into `bom`; the
heading becomes the row's type, and a footnote mark or an aside in the cell
(`R20 * 10K–22K`, `MAX (trimpot) 50K`) goes to the row's notes. Its "BOM Notes"
can overrule the drawing — on the Higgs the rate LED's resistor is 4.7K on the
sheet and "10K–22K, at least 10K to reduce ticking" in the list; ask which to
build and say so in a note. Where a value is a range, pick one and put the
reason in `bom_accept`. It
names pots and switches by function (`VOLUME`, `TRIMPOT`, `Octave`) where the
drawing uses `VOL`, `TRIM1`, `SW1` — put the drawn name in `label` and the
check pairs them up. The sheet asks not to be reposted — keep conversions
private.

**Aion FX** (including their adaptations of RunOffGroove circuits, e.g. the
Tri-Vibe) — InDesign PDFs with a **vector** schematic page ("SCHEMATIC"):
ingest renders it at 300 dpi and writes `text.json` with exact text boxes — use
those instead of OCR. Crossings are drawn with **hop-over arcs** (not joined).
Rails as named flags: `+9V` (the input), `VA` (filtered supply), `VB` (half
supply, buffered by an op-amp, so verify notes it is driven by an output), and
`BYP_GND`, a star ground Aion keeps apart from GND and joins at the power input
without drawing where. Ask whether to join it to GND. On the Tri-Vibe the user
said join it, and a note says so. Op-amp power pins are drawn on one unit →
`supply` on that entry. The LM13700 is drawn as two OTAs (`IC4A` pins 1–5,
`IC4B` pins 12–16) and two Darlington "transistors" (`IC4_BUF1` 7→8, `IC4_BUF2`
10→9). Give each its own `ic` entry with its pin numbers and `unit` A, B,
BUF1, BUF2. The tool finds the symbol's unit from the pins, and on the LM13700
it ships itself they come out in datasheet order, so the drawing's IC4A is unit
A. The `V+` on the buffers is pin 11 inside the chip: nothing to wire. The wiring page shows the board's pads
(`IN`, `GND`, `+V`, `OUT`), so the ports are `IN`, `OUT` and `+9V`. The
footswitch, status LED and `LEDR` sit on Aion's separate bypass board, which
the schematic leaves out, but the parts list has LED and LEDR. Ask whether to
put the status LED on the board (from +9V through LEDR to an `SW` port, as on
PedalPCB boards) or accept the two rows in `bom_accept`. The parts list (PART /
VALUE / TYPE / NOTES, several pages) is a text table that ingest reads into
`bom`. It also lists hardware (sockets, jacks, footswitch, enclosure), which the
check skips.

**Tim Escobedo (Circuit Snippets)** — small hand-placed GIFs, around 460x510,
one circuit and a paragraph of prose on the page. Everything is crisp black on
white at 1 px, so read the pixels rather than the tiles when a connection
matters: a 2 px gap between two capacitor plates is the difference between a
coupling cap and a piece of wire, and at this size a zoomed crop will not settle
it. **Almost nothing is labelled.** There are no reference designators except
the odd one the prose needs (the Wobbletron's `C1`, "can be changed in value for
a different vibrato tone") — keep that one where the sheet puts it and number
everything else in reading order, saying so in a note and in the title block, so
the numbering can be reproduced. Values are printed next to each part but not
always on every part: the Wobbletron's input coupling cap has none at all, which
is a question for the user, not a value to invent. The prose under the drawing is
commentary, not circuit text — but mine it for `notes`, since it names the
designers to credit and says which parts are meant to be swapped. A version and
date sit in a corner (`v. 0.9` / `08/04`) → `rev` and `date`. `+9V` is repeated
as a stub label wherever it is needed rather than drawn as a rail, so it is a
power net with flags, not `"draw": "wire"`.

Two things to expect. **Pots are often drawn as rheostats** — a resistor box with
a diagonal arrow through it and only two terminals connected. Wire the wiper to
one end so the board gets a whole pot: tie it to the end that makes clockwise the
"more" direction, and put which way round in a note. And **the junction-dot
detector is fooled by the title** — it reads the lettering of a big hand-set
heading as dots — so filter `dots.json` by position before trusting a count.

The drawing crosscheck does work on these, but only since the tracer learned that
a part's outline is not a wire (the Wobbletron was what taught it). Every part
here is a hollow shape whose black boundary touches both its leads, or a filled
one a column of pixels runs straight through, so before that the check produced a
page of `X, Y are one thing in the drawing` naming a part's own two pins. If a
sheet in some other style brings those back, they are read the same way: account
for each one by the single part body that explains it, and any pin pair that is
*not* explained that way is a real disagreement.

**KiCad-drawn forum sheets** (22358.png, 22359.png) — someone's own KiCad file
exported as a PNG around 1000 px wide: black wires, blue symbols and values,
dark red off-page connectors. Clean to read, and the pin numbers printed beside
every IC pin are the chip's real ones, so a quad op-amp's 1/2/3, 5/6/7, 8/9/10,
12/13/14 with 4 and 11 confirm the order rather than assume it. Watch for:

- **Coloured wires.** 22359 draws wires in green, symbols in red and text in
  black. The tracer picks the wire colour out of the page itself, so the check
  works — but **a sheet like this is where pin positions go unchecked while you
  are reading it**, and 22359's were: the first thing the colour-picking did was
  show that most of its pins sit ten or twenty pixels off the ink. Read the
  coordinates off pixel dumps, not off arithmetic on a zoomed crop.
- **On a two-colour sheet a pin sits where the green stops and the red starts.**
  The symbol draws its own leads in the symbol colour, so the wire ink begins
  exactly at the pin — which makes these sheets the easiest of all to place
  precisely, once you look at the right thing. Dump the wire mask and the rest of
  the ink as two characters and read the boundary off it. Two consequences worth
  expecting: the conductors are *short*, because only the hop between two symbols
  is wire at all (a pot reaches its resistor in 16 px and its ground flag in 8);
  and a rail flag's stem is mostly symbol, so **the flag's name can sit nearer a
  passing wire than its own**. 22359 has one of those and it is genuinely
  ambiguous — OCTAVE-1's `GND` is 18 px above R7's wire and 26 px below its own.
  Check such a warning by eye and record it rather than moving a pin to silence it.
- **Sanity-check a position against the sheet's own geometry.** A KiCad sheet
  repeats itself: on 22359 every resistor body is 72 px, every capacitor 55, and
  every XOR gate 101 px from input to output. A pin that breaks the pattern is
  misread.
- **Off-page connectors** — a letter in a circle (`A`, `B1`, `B2`, `B3`) at each
  end of a link the drawer did not want to route. They are ordinary named nets,
  not board pads, so do not make them `ports`. **The crosscheck will report each
  one as a split** (`A is 2 separate things in the drawing`) and it is right: the
  drawing really does keep them apart, and the name is what joins them. Expect one
  per connector and say so.
- **Switches drawn as thick boxes** with the terminals *inside* the outline. The
  box is wider than the space its pins span, so tracing walks round it and reports
  every pole as one thing. Widening what counts as a body far enough to cover it
  costs real pins elsewhere, so this one stays a known warning.
- **The same rail written two ways** — a power port `VR` in one half of the sheet
  and a plain label `Vref` in the other, or a `Power`/`Ground` global label where
  the rest of the sheet uses a triangle. Ask, then make it one net and note it.
- **Ground drawn with two different symbols**: a triangle nearly everywhere and
  one bar/earth symbol. In KiCad those are different nets, so it is worth asking;
  on 22358 it was a slip and meant GND.
- **Values but no designators** on some parts while others are numbered. Continue
  the sheet's own run rather than renumbering it — on 22358 the drawn parts stop
  at R15 and C9, so the unnumbered ones became R16–R18 and C10–C13.
- An op-amp **drawn open loop**, no feedback at all, is not a misread to correct:
  on 22358 it is a comparator squaring the guitar signal for a PLL. Check it by
  eye, then say so in `notes`.

**Escobedo's bigger sheets** (Phuncgnosis, Gargler) add two things the Wobbletron
did not. **Crossings are a break in the wire being jumped**, one pixel each side,
not an arc — so read the pixels at every crossing, and expect the drawing
cross-check to join them anyway, since its own join tolerance is wider than the
gap. On the Gargler that is one warning, and the pixels settle it. And **a filled
symbol body is read as a junction dot by ingest**: the Phuncgnosis's LED triangles
all came back in `dots.json`, and a dot joins whatever it sits on, so a pin placed
within a few pixels of a filled triangle or a diode's bar gets welded to the far
side of the part. Put those pins on the lead, clear of the body, and the cut rule
in `drop_bodies` does the rest.

Two more habits from these two. Escobedo **puts the legend in the drawing**
(`U = typical opamp`, `U2 = 741 only`, `Q1 = 2n3904`, `Q2 = J176`) — read it
before the parts, it is the only place the semiconductors are named, and where it
says "typical opamp" the packaging is yours to choose. And a **panel switch the
profiles have no type for** (the Gargler's 2P3T) is not a reason to stop: bring
its nodes out as pads, put the wiring in a note, and the board is buildable.
