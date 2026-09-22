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
  `supply`.
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
page, drawn in KiCad, embedded as a low-resolution image **rotated 90° inside
the image** — ingest turns it upright; confirm on `grid.png`. Dense CD40xx
logic at ~2000 px: read tiles slowly, pin numbers matter. Pin numbers visible;
rails as flags (`VA`, `+9V`); CD40xx logic appears. The sheet asks not to be
reposted — keep conversions private.

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
BUF1, BUF2. The tool finds KiCad's unit from the pins (the stock symbol puts OTA
B first, so the drawing's IC4A is KiCad's IC4C). The `V+` on the buffers is pin
11 inside the chip: nothing to wire. The wiring page shows the board's pads
(`IN`, `GND`, `+V`, `OUT`), so the ports are `IN`, `OUT` and `+9V`. The
footswitch, status LED and `LEDR` sit on Aion's separate bypass board, which
the schematic leaves out, but the parts list has LED and LEDR. Ask whether to
put the status LED on the board (from +9V through LEDR to an `SW` port, as on
PedalPCB boards) or accept the two rows in `bom_accept`. The parts list (PART /
VALUE / TYPE / NOTES, several pages) is a text table that ingest reads into
`bom`. It also lists hardware (sockets, jacks, footswitch, enclosure), which the
check skips.
