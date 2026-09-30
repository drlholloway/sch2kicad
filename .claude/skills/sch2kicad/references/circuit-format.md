# circuit.json

What you write after reading a schematic. Coordinates are pixels of the work
directory's `source.png` (origin top-left, y down). `sch2kicad ingest` writes a
skeleton; fill it in.

```json
{
  "schema": "sch2kicad/circuit@1",
  "name": "Ripped Speaker - cb mod",
  "source": {"file": "EHX Ripped Speaker Fuzz - cb mod v1.0.png",
             "origin": "Chuck's Boneyard, PedalPCB forum",
             "image": "source.png", "width": 1396, "height": 809},
  "title_block": {"title": "Ripped speaker - cb mod", "author": "Chuck D. Bones",
                  "company": "Celestial Engineering", "rev": "1.0", "date": "18-Aug-26",
                  "comments": ["EHX Ripped Speaker Fuzz - cb mod v1.0.dsn"]},
  "nets": {
    "GND": {"kind": "ground"},
    "VCC": {"kind": "power", "draw": "wire"}
  },
  "parts": [
    {"ref": "R5", "type": "resistor", "value": "10K",
     "pins": {"1": ["VCC", 339, 130], "2": ["Q1_C", 339, 175]}},
    {"ref": "Q1", "type": "npn", "part": "2N5089",
     "pins": {"B": ["Q1_B", 313, 274], "C": ["Q1_C", 339, 250], "E": ["Q1_E", 339, 298]}},
    {"ref": "VR1", "type": "pot", "value": "C10K", "label": "FUZZ",
     "pins": {"cw": ["Q1_E", 339, 318], "wiper": ["FUZZ_W", 352, 338], "ccw": ["GND", 339, 358]}},
    {"ref": "C3", "type": "capacitor_polarized", "value": "10uF",
     "pins": {"+": ["FUZZ_W", 375, 342], "-": ["GND", 375, 362]}}
  ],
  "ports": [{"name": "INPUT", "net": "IN", "at": [127, 274]}],
  "texts": [{"text": "+5.20V", "at": [345, 188], "kind": "voltage"}],
  "notes": ["Q2 & Q3 are installed upside-down (emitter & collector swapped)."],
  "blocks": [{"name": "Q1 fuzz stage", "parts": ["R3", "R4", "Q1", "R5", "VR1", "C3"],
              "description": "2N5089 common-emitter stage; FUZZ sets the emitter bypass."}],
  "uncertain": [{"note": "C4 value could be 1uF or 10uF — smudged", "refs": ["C4"], "at": [515, 245]}]
}
```

## Pins

Each pin is `[net, x, y]` — the net it joins and **where the wire meets the
part** in the source. The positions decide how the symbol is rotated and
mirrored (a pot with `cw` at the top and the wiper on the right comes out that
way), so get the *relative* positions right; a few pixels off is fine. Also
accepted: `{"net": "X", "at": [x, y]}`. A pin with net `"NC"` (or `null`) is
deliberately unconnected and gets a no-connect flag.

| type | pins | notes |
|---|---|---|
| `resistor`, `capacitor`, `inductor`, `ldr`, `switch_spst` | `1`, `2` | ends are interchangeable |
| `capacitor_polarized` | `+`, `-` | electrolytic / tantalum |
| `diode`, `led`, `zener`, `schottky` | `A`, `K` | the bar is K |
| `pot`, `trimmer` | `cw`, `wiper`, `ccw` | `1`/`2`/`3` also accepted = ccw/wiper/cw (lug numbers). `cw` → lug 3 |
| `npn`, `pnp` | `B`, `C`, `E` | set `part`; `pinout` only once confirmed |
| `njfet`, `pjfet`, `nmos`, `pmos` | `G`, `D`, `S` | ditto |
| `opamp_dual` | `+`, `-`, `out` per unit | `unit: "A"`/`"B"`; power in `supply` |
| `opamp_quad` | `+`, `-`, `out` per unit | `unit: "A"`…`"D"`, supply on `"E"`. Drawn V+ on 4, V- on 11 — the TL074/TL084/LM324 order; a quad the table does not know says so in the report, so check the datasheet and set `lib_id` if it differs |
| `opamp_single` | `+`, `-`, `out` | `part` must be a known symbol (TL071, LM741...) |
| `lm386`, `ic` | pin numbers `"1"`…`"16"`, or the symbol's pin names | `ic` needs `lib_id` unless `part` is in the profile's `named_parts` (suffix grades match: `LM13700N` → `LM13700`). A multi-unit IC's unit is the one holding the listed pin numbers, so `unit` can keep the drawing's name (`A`, `BUF1`); the report says which KiCad unit it became. The package comes from the profile by pin count (DIP-8/14/16, SOIC for smd). A part with no KiCad symbol can come from the tool's own library (`CD4517` → `sch2kicad:CD4517`), which is copied into the built project. A TL431 is an `ic` with pins `REF`, `A`, `K` by name: TI and onsemi number the same TO-92 leads opposite ways, and the SOT-23 is numbered differently again |
| `switch_spdt` | `com`, `a`, `b` | throws are interchangeable; set `action` |
| `switch_dpdt` | `com`, `a`, `b` per pole | `unit: "A"`/`"B"`; set `action`. Unlike an SPDT's, the throws are **paired across the poles**: `a` on both poles is made in one position (lugs 1 and 4), `b` in the other (3 and 6). Name them so — the throws at the same end of the lever, or the drawing's lug numbers 1/4 and 3/6 — whichever way each pole is drawn |
| `switch_2p3t` | `com`, `1`, `2`, `3` per pole | `unit: "A"`/`"B"`. The 16mm mini rotary. `1`/`2`/`3` are the order the shaft turns through, so unlike a toggle's throws they are **not** interchangeable. A throw the drawing leaves dangling is `"NC"`. It lands on the 2P4T footprint, whose fourth position the part has whether the circuit uses it or not |
| `transformer` | pin numbers `"1"`…`"6"` | The Xicon 42TM series on the house `TM011-R` footprint: one winding 1-2-3 and the other 4-5-6, 2 and 5 the centre taps, **as Xicon number them** — not as the drawing does. An unused tap is `"NC"`. Say how the drawing's numbers map in a note |

## Part fields

- `ref` — as printed (`R1`, `VR3`, `RLED`, `CLR`). PedalPCB names pots by their
  knob (`PRE`, `TONE`) and the status LED `LED`; use those as the ref. Multi-unit
  ICs share a ref and differ by `unit` (`U1` + `"unit": "A"`), however the
  source spells it (`U1A`, `IC1.1`).
- `value` — exactly as printed; pots keep their taper letter (`A100K`, `B50K`, `C10K`).
- `part` — part number for semiconductors and ICs (`2N5089`, `TL072`, `1N5817`).
- `label` — the knob/switch function printed next to a pot or switch (`FUZZ`). Becomes the `Label` field.
- `action` — a toggle's action: `"on/on"`, `"on/off/on"` or `"on/on/on"` (`"on/off"`
  for an SPST). Three switches with the same lugs and the same netlist are three
  different parts to order, and the netlist cannot carry the difference, so the
  schematic has to: it becomes the switch's value (`DPDT on/on/on`) and its
  description. Spellings fold, so `"on-on-on"`, `"cntr off"` and a whole parts-list
  row (`"Toggle switch, SPDT on-off-on"`) all read. **A DPDT drawn with a third
  live position is an `on/on/on`** — a plain on/on cannot do it. Left out, the build
  stays READY and says the action is unmarked, so set it whenever the drawing, the
  parts list or the notes say. `on/on/on` on a single-pole switch is a problem:
  one pole cannot make three different live positions.
- `pinout` — lead order pin 1→3 on the board, e.g. `"EBC"`. Only set it after the
  user settles it; it overrides the pinout table (including the E-B-C assumption for
  through-hole 2N2222/2N2907). Unknown parts without it are never READY.
- `supply` — IC power pins → nets, e.g. `{"V+": "VCC", "V-": "GND"}`. Put it on one
  unit; the power unit is drawn beside the amplifier with flags.
  - When the source draws the power unit on its own (PedalPCB's `IC1` with pins
    8/4, often on its own filtered rail), give it its own entry with the unit after
    the amplifiers, no `pins`, and positioned supply pins:
    `{"ref": "IC1", "unit": "C", "type": "opamp_dual", "part": "RC4558",
    "supply": {"V+": ["IC1_V+", 747, 1235], "V-": ["GND", 747, 1293]}}`. It is placed
    where it is drawn, and a block can list it as `IC1C`.
  - A single-unit op-amp (TL071) has its power pins on the amplifier: put `supply`
    (nets only) on the amplifier's entry, even when the source draws `IC3.2` apart.
  - Same for a logic chip whose supply box is drawn on its own (`U1` with `VDD`/`VSS`):
    `{"ref": "U1", "unit": "G", "type": "ic", "part": "CD4069",
    "supply": {"VDD": ["VA", 340, 255], "VSS": ["GND", 340, 215]}}`. `V+`/`V-` also
    find `VDD`/`VCC` and `VSS`/`VEE` pins, and `VDD` finds a pin KiCad calls `VCC`
    (its 4049) and the other way round.
- `footprint`, `lib_id` — overrides, rarely needed.
- `dnp` — true for parts marked do-not-populate / optional.

## Nets

Unlisted nets are signals. List only what needs a kind:

- `"kind": "ground"` — drawn as ground flags at each pin (like the source's ground symbols).
- `"kind": "power"` — a supply rail. `"draw": "flag"` (default) puts a flag at each
  pin; `"draw": "wire"` routes it as wire, for a rail the source draws as a line.
  Use the source's names when it has them (`VCC`, `VREF`/`Vref`, `VA`, `VB`, `+9V`).

## Ports, texts, notes, blocks, uncertain

- `ports` — off-board connections (`IN`/`INPUT`, `OUT`, `+9V`, `LED+`, `SW`...): the
  small squares/circles the wires run to. `at` is the connection point. IN/OUT land
  on the 3PDT pad array; others get their own pad.
- `true_bypass` — `false` when no 3PDT switches the jacks: a buffered or
  JFET-switched bypass, where the footswitch only switches a control line. IN and OUT
  then come in on the house jack sockets (`JIN`/`JOUT`: tip to the signal, the other
  pin to ground), with no 3PDT array and no separate jack ground pad. Bring the
  footswitch out as a pad per lug it uses, named for the lug (`FOOTSW 2`).
- `texts` — annotations that are not nets: measured voltages (`"kind": "voltage"`),
  remarks next to parts. Knob names go in `label`, not here.
- `notes` — the NOTES block, one string per note.
- `blocks` — functional groups, every part in at most one. Each becomes a named
  group on the sheet and its own schematic with `sch2kicad blocks`.
- `uncertain` — anything you could not read with confidence. Keep them until the
  user has looked.

## Parts list

- `bom` — the build doc's parts list, one row per line:
  `{"ref": "D3", "value": "GE", "type": "Germanium Diode", "notes": "(1N34A, ...)", "page": 3}`.
  Ingest writes it; don't edit the rows (they are what the doc says). It reads
  both a table (PedalPCB, Aion FX) and a grid of column pairs under headings
  (Parasit Studio). Verify checks every part against it. Rows whose type is
  hardware (sockets, jacks, footswitch, enclosure) are skipped. A row matches by
  ref or by the part's `label`, so a knob or switch the list names by function
  (`VOLUME`, `Octave`) finds the drawn `VOL`, `SW1`.
- `bom_accept` — differences the user has looked at, ref → why:
  `{"R5": "drawing is right, the list has a typo"}`. They become notes instead of
  problems.
