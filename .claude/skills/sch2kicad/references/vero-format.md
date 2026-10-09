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
     and the skeleton marks them NC.

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

## What the image check can and cannot see

It finds the hole grid from the copper, then looks for:
- **cuts**: a pure red square round a hole that still shows;
- **links**: a black bar between holes, across the strips.

A cut under a chip's body shows a darker red, and is seen. A cut under a
translucent resistor or a part's lettering can't be seen, and neither can a link
passing under a part. Those come back as "in the reading, but not seen" warnings.
Look, then say so in the report. A slanting link, or a link along a strip, is not
checked.
