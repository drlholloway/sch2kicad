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

**A pin is where the wire stops, and nowhere else.** Three places look right on
a zoomed crop and are not: the **pin number** written beside the terminal (a
whole sheet was once read that way, every IC pin landing in blank paper inside
its own box), the **middle of the body** (a capacitor's plate gap, a resistor's
zigzag), and a **point out along the wire** past the terminal. The last traces
fine and still hurts — the box a part's pins span is what `drop_bodies` cuts
out, so a pin placed beyond the terminal makes that box swallow the very wire
that feeds the next pin along. Read the boundary off the pixels: dump the wire
mask and the rest of the ink as two characters along the pin's own row or
column, and the terminal is where one stops and the other starts. On a
two-colour sheet that boundary is a colour change and is exact.

**The cross-check tells you which ones are wrong.** Every pin it cannot match is
reported with the distance to the nearest ink: a wire 11–20 px away is a pin to
move, and the same line says which way. A part with no wire anywhere in the
space it would stand in is reported separately, as one the drawing does not
draw — a bypass LED the builder adds, a spare gate — and those pins come off the
coverage count instead of sitting in it forever.

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
read tiles slowly at 4-6x, pin numbers matter. **Their pin numbers are written
outside the box, above the stub** — so the number is 30-40 px from the terminal
it names, and a pin read at the number lands inside the chip's own box in blank
paper. The terminal is the box edge, where the black stub meets the blue
outline: scan the column just outside an edge and every pin's row is there in
order, which reads a sixteen-pin chip off in one go. The same trap dressed
differently is the wiper of a pot or trimmer — the arrow is the symbol, so the
pin goes at the far end of the arrow's shaft where the wire begins, not at the
arrowhead's tip on the track. Their newer sheets are drawn in
KiCad (the Higgs, 2024), and those render cleanly enough that `dots.json` is
worth having — on the Higgs it found all 40 junctions and nothing else, so it
is a real check on a reading: list the dots, account for every one, and be sure
no junction you drew is missing from the list. **Their older sheets defeat it**:
the Theremin's dots are 6 px across where the detector wants 7, so it reports
one dot on the whole page and the list tells you nothing. Check the count before
you lean on it — one dot on a dense sheet means look at the pixels instead. The
cross-check needs no help there: a run that stops just past the wire it crosses
has met it, which is how those junctions read whether or not the dot is found.
A dangling wire end shows as a
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
  passing wire than its own**. 22359's OCTAVE-1 writes `GND` 18 px above R7's wire
  and 26 px below its own. The crosscheck settles that by the blank paper between
  the two rather than the distance — the flag's own bar fills 20 of those 26 px,
  and R7 has bare page all the way — so expect no warning; if one comes back, it
  is saying the name really is written against the other wire, and the answer is
  to look rather than to move a pin until it stops.
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
  box stands wider than the space its pins span, so nothing that measures how far
  a body reaches will cover it — the crosscheck recognises the closed rectangle
  instead and drops its four sides, so put the terminals where the drawing puts
  them and expect no warning.
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
not an arc — so read the pixels at every crossing. The cross-check reads that
break as a crossing too, so expect no warning from one; what it needs is a wire
long enough to be a wire on both sides of the gap, which is what stops a
capacitor's plate between its own leads from counting as a wire hopped over. And **a filled
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

**Kelly Roberts** (the Fix'd Fuzz, 2011) — a black-on-white PNG about 1600 px wide,
no reference designators at all, so number the parts yourself in reading order.
Watch for:

- **Crossings drawn as arcs** where the rail steps over a bypass loop. Every
  junction has a dot; an arc is never one.
- **A stage switched in and out by two levers drawn apart** — an in-lever and an
  out-lever, the bypass wire looping over the stage between their upper contacts.
  The two must move together or the signal stops, so each pair is one DPDT. The
  lever is one continuous line from contact down to a flat run and up to the next
  stage's contact: the flat run *is* the common, shared by the out-pole of one
  stage and the in-pole of the next.
- **A small arrow along each pot's body** points to its clockwise lug (it points
  at the input end of Level). And the wiper's pin is where its lead meets the
  arrow's *base*, not the tip: at the tip it sits 7 px from the pot's own body
  line and matches that instead of its wire.
- **Capacitor plates one pixel apart.** The crosscheck handles it (a run broken in
  two between a part's pins is its body), so expect no warning.
- **Corrections pasted onto the sheet** — the Fix'd in the name. The emitter
  resistor's original value is painted out, leaving a grey smudge, and `390r`
  added beside it in a different grey font. Read the pasted value, and say so in
  `notes`.

**Blackout Effectors' Musket** (gaussmarkov's redraw of a Big Muff, "corrected as
per fsb.org") — a 1600×382 PNG, small lettering, most labels written **vertically**
up the side of their part. Watch for:

- **Ingest used to turn this sheet on its side**: its sideways labels read better
  turned a quarter than upright. It now counts words as well as labels and only
  turns a page that reads almost nothing upright — but if `grid.png` comes out
  sideways on a sheet like this, that is the first thing to suspect.
- **Corrections in a bolder font, sometimes well away from their part**: R27's `10k`
  sits 40 px to its right, with R27's own label beside it bare. Five on this sheet
  (R27, C6, C9, C11, R17). Two publishers in a row did this: expect it on anything
  called "corrected" or "fixed", and give each pasted value to the part whose own
  value is missing.
- **A duplicate reference** (two R19s) with one number never used (R18): the
  duplicate takes the free number, as 22359's duplicate R3 did. And one cap with
  no reference at all takes the next free one.
- **Pots carry their lug numbers** (1/2/3) — use them as printed.
- **An antiparallel diode pair's labels sit between its two diodes**, so which is
  D1 and which D2 cannot be read. Identical diodes, so it changes nothing; say so.
- **Rails named `+9V(T)` and `GND(T)`.** Call them `+9V` and `GND`. OCR does not
  read these small labels, so a pin whose only ground is its own flag (R17 here)
  is not checked by the crosscheck — it is not wrong, just unchecked.

**Talisman Audio doc sheets** (Angry Droid, and three unbranded sheets in the same
hand: Djinn's Flame, Hull Fuzz, TL431 Precision Fuzz) — KiCad-drawn PNGs 850-1300 px
wide, black on white, the parts list printed beside the circuit. Watch for:

- **Pin numbers are printed on every transistor, pot, IC and transformer.** Use
  them to place pins, but not as the part's own numbering: the Djinn's transformer
  is numbered 1, 2 / 5, 4, 3 on the sheet and 1, 3 / 6, 5, 4 on the Xicon part.
- **Diagonal crossovers.** The Angry Droid crosses its multivibrator over in two
  Xs of slanting wire with no dot — a crossing, not a junction. The tracer reads a
  slanting wire as one line now, so it agrees; check the Xs by eye all the same.
- **A DPDT drawn as two boxes with the terminals inside** (Hull Fuzz). Put each
  pin on its terminal circle, inside the box — on the box's edge the cross-check
  reads the outline as wire and joins the throws. Pair the throws the way the lugs
  are numbered: `a` is 1 on pole A and 4 on pole B.
- **Power block with circles at both ends.** The right-hand `9v`/`GND` circles are
  the same rails carried on; model the left pair as the ports and say so in a note.
- **A thick op-amp triangle.** The LM386's sides are 3 px of ink, so a pin put a
  few pixels off its lead lands on the outline instead (the Djinn's pin 3 at 324
  rather than 328). Read the lead's row off the pixels.
- **The flicker LEDs, "RCF"** (Hull Fuzz D1/D2): red candle-flicker LEDs used as
  the bias pair, value as printed. The TL431 is an `ic` with pins `REF`, `A`, `K`.

**The Christine** (The Tone God's design, drawn by Taylor Livingston for musicpcb)
— a 1200 px PNG in a PDF, **white on black**. Ingest inverts it ("drawn light on
dark: inverted onto white"); read `source.png`, not the PDF page. Watch for:

- **Junction dots barely fatter than the 3 px wires.** `dots.json` finds almost
  none of them; read the dots off the picture. The tracer joins these T's anyway.
- **The 4049's supply is written "Pin 1" / "Pin 8"** at two ends of the sheet, and
  pin 8 (VSS) goes to ground through the Power Starve pot and 1k, **not** to GND:
  give it its own net. `supply` takes `VDD`/`VSS`; KiCad's 4049 names pin 1 `VCC`,
  and either name finds it.
- **A dot labelled NC** at the end of the oscillator's output wire is just the end
  of it — no part, no port.
- Pots are drawn without taper letters; the parts list has them (note them).

**PCB Guitar Mania build docs** (Acapulco Tone) — a vector page, the circuit
**drawn sideways** on an upright page, its lettering drawn as outlines (so there is
no exact text, and the header reads upright): ingest can't tell, so give
`--turn 270` (text reading down the page) or `--turn 90` (reading up). A large
grey **watermark** is drawn over it — filled shapes, ignored by the tracer; read
through it. Watch for:

- **Pads end in an X** with the lead starting where its strokes cross (IN, OUT,
  9V, LED, four GND pads). Put the port at the X's centre.
- **Stray symbols** with no reference wired to nothing (two JFETs, nets `N$3`,
  `N$27`, `N$24`) — leftovers in the drawing; don't build them, say so in a note.
- **The schematic and the parts list can disagree on a part** (Q1 is J201 on the
  sheet, 2N5457 in the BOM): keep the drawn part, note the other, and check the
  pinout table for both.
- The doc's build notes describe a v1.2 PCB trace fault; it is not in the circuit.

**KiCad sheets printed to PDF** (Refresh Audio FX's Halsted St OD) — a clean KiCad
drawing, but printed through macOS: no text layer, boxes as quads. Ingest reads it
as a vector page. Watch for:

- **The drawing's own pads** (`H1`-`H12`: IN, OUT, SW, (+), (−), jack pairs, GND).
  Bring them all over as ports: IN/OUT/SW and one `JI`/`JO` pair land on the 3PDT
  array, the rest are loose pads. A pair joined only by label (`J_IN` on H7 and
  H11) shows as one split in the cross-check; that is the drawing, not a misread.
  Nets of nothing but pads are an off-board wire: verify notes them, not an ERC error.
- **Check each LED's direction against how the SW pad is switched.** The Halsted
  drew D2 anode-on-SW, which never lights with a grounded SW; Lane had it flipped.
- A 4049 on its own rail through a resistor (`VA` → 1k → `U1_VCC`): make both rails
  `power` nets, and put the spare inverters' inputs where they are drawn.
- The file name in the title block can differ from the title (`35th St OD:Dist`);
  the parts list may carry the file name.

**Effects Layouts** (Pocket Comb) — a KiCad sheet exported as one big raster image,
placed sideways in the PDF; ingest pulls the image out upright. The layout page
and the parts list are the cross-checks. Watch for:

- **The drawing rounds values** the parts list gives exactly (4n for 3.9n, 3n for
  3.3n). Ask; Lane took the parts list's.
- **The regulator's package is on the layout, not the sheet**: "7805" drawn and
  listed, but the board has a TO-92 marked O-G-I, a 78L05 (`Regulator_Linear:L78L05_TO92`
  pins by name `VI`/`VO`/`GND`, footprint the house TO-92). Ask.
- **BBDs**: KiCad has `Audio:MN3207` and `Timer:MN3102`; the sheet's `V3207`/`V3102`
  (CoolAudio) have the same pins. The MN3207's two outputs are tied — verify notes it.
- **Custom ground names** (`GND_PADROUND` under R1): in KiCad that value is a net of
  its own; build it on GND and say so.
- **The battery symbol has the wire drawn through it**, so the tracer sees 9V and
  GND joined; bring it out as 9V and GND pads. The LED's cathode runs to a pad
  marked `L`: that is SW. A parts-list row with no part (R54) is a stray — check
  the shopping list's counts.

**Eagle sheets** (Moonn Electronics' Hot Mett) — parts red, wires green, pads as
red X marks, exported as one raster image. Watch for:

- **A pin is where the green wire starts**, at the outer tip of the part's red
  lead, not at the body: 10-20 px out on a small sheet. Read the pin there, or the
  cross-check finds no wire under it.
- **Read each capacitor's plates, not its label**: a cap hung under a junction
  (C7 below the dot) is easy to place 30 px low, which puts both ends on one net.
  A cap between two IC pins (C9 across PT2399 9 and 10) has two nets.
- **Follow a long return wire to its dot**: the Hot Mett's REPEAT wiper lands
  after R12, on the MIX side, not on the PT2399's output.
- KiCad has `Audio:PT2399` with the datasheet's numbering; REF (2) and CLK_O (5)
  may be left open on the sheet.
- The doc may ask to swap a pot's lugs 1 and 3 (Moonn's MIX, TIME, REPEAT turn
  backwards). Ask; to swap, keep each wire where it is drawn and trade which end
  is `cw` and which `ccw`, so the cross-check still agrees.
- A regulator drawn as a 7805 may be a 78L05 in TO-92: look at the board.

**Pedal Parts / FuzzDog** (ICL, the Invading Cannibal Ladybirds) — a raster sheet
placed rotated *and mirrored* on the page. Watch for:

- **The stored image is mirrored.** Ingest flips it back (`"mirrored": true` in
  `ingest.json`). If a sheet still reads 6 for 9, L for 7, or a DIP's notch sits at
  the bottom, it is mirrored: stop and fix ingest, don't read it backwards.
- **Pots have their lugs numbered 1 and 3**, with a small arrow by lug 1 pointing
  towards lug 3: lug 3 is clockwise, on every pot.
- Rails are flags (`+9V` circle with a plus, `VCC` open circle); the only pads are
  IN, OUT and `+V` (before the series 1N5817). Bring out a GND pad for the jack.
- `R2 Empty` is a DNP slot: include it with `"dnp": true`.
- A regulator drawn `78XX` is the parts list's (78L05 here).
