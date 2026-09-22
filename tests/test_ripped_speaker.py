"""End to end on a real Boneyard schematic: needs KiCad 10 and the Pedal Division library."""

import shutil
from pathlib import Path

import pytest

from sch2kicad.build import build
from sch2kicad.kicad_env import kicad_cli
from sch2kicad.symlib import SymbolResolver
from sch2kicad.verify import verify

HERE = Path(__file__).resolve().parent
CIRCUIT = HERE.parent / "samples" / "circuits" / "ripped-speaker-cb-mod.circuit.json"


def _have_kicad():
    try:
        kicad_cli()
        return SymbolResolver().exists("2025_PedalDivision_Resistors:TH_Resistor_1/4w")
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _have_kicad(), reason="needs KiCad 10 and the Pedal Division library")


SAMPLES = sorted((HERE.parent / "samples" / "circuits").glob("*.circuit.json"))


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_every_sample_is_ready(tmp_path, circuit):
    rep = verify(build(circuit, tmp_path / "board").sch, render=False)
    assert rep.ok, rep.text()


@pytest.mark.parametrize("circuit", SAMPLES, ids=lambda p: p.name.split(".")[0])
def test_every_block_is_ready(tmp_path, circuit):
    from sch2kicad.blocks import save_blocks
    for sch in save_blocks(circuit, tmp_path / "blocks"):
        rep = verify(sch, render=False)
        assert rep.ok, rep.text()


@pytest.mark.parametrize("profile", ["tht", "smd"])
def test_build_matches_netlist(tmp_path, profile):
    res = build(CIRCUIT, tmp_path / profile, profile=profile)
    rep = verify(res.sch, render=False)
    assert rep.netlist_ok, rep.netlist_diffs
    assert rep.erc_errors == []
    assert rep.footprint_problems == []
    assert rep.unconfirmed == []
    assert rep.ok, rep.text()
    # the 2N2222s are E-B-C by assumption, and the report must say so
    if profile == "tht":
        assert sum("assumed E-B-C" in n for n in rep.notes) == 2


def test_unknown_pinout_blocks_ready(tmp_path):
    import json
    data = json.loads(CIRCUIT.read_text())
    for p in data["parts"]:
        if p.get("part") == "2N2222":
            p["part"] = "MP38A"          # germanium, lead order unknown: must ask
    data["source"]["image"] = str((CIRCUIT.parent / data["source"]["image"]).resolve())
    c = tmp_path / "c.json"
    c.write_text(json.dumps(data))
    rep = verify(build(c, tmp_path / "out").sch, render=False)
    assert len(rep.unconfirmed) == 2
    assert not rep.ok


def test_explicit_pinout_overrides_assumption(tmp_path):
    import json
    data = json.loads(CIRCUIT.read_text())
    for p in data["parts"]:
        if p.get("part") == "2N2222":
            p["pinout"] = "CBE"
    data["source"]["image"] = str((CIRCUIT.parent / data["source"]["image"]).resolve())
    c = tmp_path / "c.json"
    c.write_text(json.dumps(data))
    res = build(c, tmp_path / "out")
    rep = verify(res.sch, render=False)
    assert rep.ok, rep.text()
    # a CBE part must not land on the house E-B-C symbol
    sch = res.sch.read_text()
    assert "Transistor_BJT:Q_NPN_CBE" in sch
