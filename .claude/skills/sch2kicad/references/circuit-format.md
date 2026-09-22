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
| `opamp_single` | `+`, `-`, `out` | `part` must be a known symbol (TL071, LM741...) |
| `lm386`, `ic` | pin numbers `"1"`…`"8"` | `ic` needs `lib_id` unless `part` is in the profile's `named_parts` |
| `switch_spdt` | `com`, `a`, `b` | throws are interchangeable |
| `switch_dpdt` | `com`, `a`, `b` per pole | `unit: "A"`/`"B"` |

## Part fields

- `ref` — as printed (`R1`, `VR3`, `RLED`, `CLR`). Multi-unit ICs share a ref
  and differ by `unit` (`U1` + `"unit": "A"`), however the source spells it
  (`U1A`, `IC1.1`).
- `value` — exactly as printed; pots keep their taper letter (`A100K`, `B50K`, `C10K`).
- `part` — part number for semiconductors and ICs (`2N5089`, `TL072`, `1N5817`).
- `label` — the knob/switch function printed next to a pot or switch (`FUZZ`). Becomes the `Label` field.
- `pinout` — lead order pin 1→3 on the board, e.g. `"EBC"`. Only set it after the
  user settles it; it overrides the pinout table (including the E-B-C assumption for
  through-hole 2N2222/2N2907). Unknown parts without it are never READY.
- `supply` — IC power pins → nets, e.g. `{"V+": "VCC", "V-": "GND"}`. Put it on one
  unit; the power unit is drawn beside the amplifier with flags.
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
- `texts` — annotations that are not nets: measured voltages (`"kind": "voltage"`),
  remarks next to parts. Knob names go in `label`, not here.
- `notes` — the NOTES block, one string per note.
- `blocks` — functional groups, every part in at most one. Each becomes a named
  group on the sheet and its own schematic with `sch2kicad blocks`.
- `uncertain` — anything you could not read with confidence. Keep them until the
  user has looked.
