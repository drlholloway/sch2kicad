"""Where KiCad lives on this machine, and what its library tables say.

Library nicknames (``Device``, ``2025_PedalDivision_Resistors``) are resolved
the way KiCad resolves them: the project's table first, then the user's global
table, following nested ``Table`` entries and expanding ``${VARS}``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import sexpr

KICAD_MAJOR = "10.0"
SCH_FILE_VERSION = "20260306"  # what KiCad 10.0 writes
GENERATOR = "sch2kicad"


def _mac_app() -> Path:
    return Path("/Applications/KiCad/KiCad.app/Contents")


def kicad_cli() -> Path:
    env = os.environ.get("KICAD_CLI")
    if env:
        return Path(env)
    found = shutil.which("kicad-cli")
    if found:
        return Path(found)
    if platform.system() == "Darwin":
        p = _mac_app() / "MacOS" / "kicad-cli"
        if p.exists():
            return p
    for p in (Path("/usr/bin/kicad-cli"), Path("/usr/local/bin/kicad-cli"),
              Path(r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe")):
        if p.exists():
            return p
    raise FileNotFoundError("kicad-cli not found; install KiCad 10 or set KICAD_CLI")


def shared_support() -> Path:
    env = os.environ.get("KICAD_SHARED_SUPPORT")
    if env:
        return Path(env)
    if platform.system() == "Darwin":
        return _mac_app() / "SharedSupport"
    for p in (Path("/usr/share/kicad"), Path(r"C:\Program Files\KiCad\10.0\share\kicad")):
        if p.exists():
            return p
    raise FileNotFoundError("KiCad shared support directory not found; set KICAD_SHARED_SUPPORT")


def config_dir() -> Path:
    env = os.environ.get("KICAD_CONFIG_HOME")
    if env:
        return Path(env) / KICAD_MAJOR
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Preferences" / "kicad" / KICAD_MAJOR
    if system == "Windows":
        return Path(os.environ["APPDATA"]) / "kicad" / KICAD_MAJOR
    return Path.home() / ".config" / "kicad" / KICAD_MAJOR


@lru_cache(maxsize=1)
def path_vars() -> dict[str, str]:
    ss = shared_support()
    v = {
        "KICAD10_SYMBOL_DIR": str(ss / "symbols"),
        "KICAD10_FOOTPRINT_DIR": str(ss / "footprints"),
        "KICAD10_3DMODEL_DIR": str(ss / "3dmodels"),
        "KICAD10_TEMPLATE_DIR": str(ss / "template"),
    }
    common = config_dir() / "kicad_common.json"
    if common.exists():
        try:
            user_vars = (json.loads(common.read_text()).get("environment") or {}).get("vars") or {}
            v.update({k: str(x) for k, x in user_vars.items()})
        except (json.JSONDecodeError, AttributeError):
            pass
    for k in list(v):
        if k in os.environ:
            v[k] = os.environ[k]
    return v


def expand(uri: str, extra: dict[str, str] | None = None) -> str:
    vars_ = dict(path_vars())
    if extra:
        vars_.update(extra)
    return re.sub(r"\$\{([^}]+)\}", lambda m: vars_.get(m.group(1), os.environ.get(m.group(1), m.group(0))), uri)


@dataclass
class LibTable:
    """Nickname → path for one kind of library (symbols or footprints)."""

    kind: str  # "sym" or "fp"
    libs: dict[str, Path] = field(default_factory=dict)

    def resolve(self, nickname: str) -> Path | None:
        return self.libs.get(nickname)


def _read_table(path: Path, into: dict[str, Path], extra: dict[str, str], seen: set[Path]) -> None:
    path = path.resolve()
    if path in seen or not path.exists():
        return
    seen.add(path)
    root = sexpr.parse(path.read_text())
    for lib in sexpr.children(root, "lib"):
        name = str(sexpr.value(sexpr.child(lib, "name"), 1, ""))
        typ = str(sexpr.value(sexpr.child(lib, "type"), 1, ""))
        uri = expand(str(sexpr.value(sexpr.child(lib, "uri"), 1, "")), extra)
        disabled = sexpr.child(lib, "disabled") is not None
        if disabled:
            continue
        if typ == "Table":
            _read_table(Path(uri), into, extra, seen)
        elif name and name not in into:
            into[name] = Path(uri)


def lib_table(kind: str, project_dir: Path | None = None) -> LibTable:
    fname = {"sym": "sym-lib-table", "fp": "fp-lib-table"}[kind]
    libs: dict[str, Path] = {}
    seen: set[Path] = set()
    extra = {"KIPRJMOD": str(project_dir)} if project_dir else {}
    if project_dir and (project_dir / fname).exists():
        _read_table(project_dir / fname, libs, extra, seen)
    user = config_dir() / fname
    if user.exists():
        _read_table(user, libs, extra, seen)
    else:  # fresh install: fall back to the stock table
        _read_table(shared_support() / "template" / fname, libs, extra, seen)
    return LibTable(kind, libs)


def footprint_exists(fp_id: str, table: LibTable) -> bool:
    if ":" not in fp_id:
        return False
    nick, name = fp_id.split(":", 1)
    lib = table.resolve(nick)
    return bool(lib and (lib / f"{name}.kicad_mod").exists())
