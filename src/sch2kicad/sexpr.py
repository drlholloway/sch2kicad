"""Read and write KiCad S-expressions.

A parsed expression is a nested Python list. Bare tokens stay ``str``; quoted
strings become :class:`Q` so they survive a round trip; numbers stay tokens
until a caller asks for them, because KiCad's number formatting is not
something we want to disturb in text we copy verbatim (library symbols).
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator


class Q(str):
    """A string that is written quoted."""

    __slots__ = ()


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_UNESCAPE = re.compile(r"\\(.)", re.S)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _unescape(s: str) -> str:
    return _UNESCAPE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), s)


def parse(text: str) -> list:
    """Parse one top-level expression."""
    stack: list[list] = [[]]
    pos = 0
    end = len(text)
    while pos < end:
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip():
                raise ValueError(f"unparseable S-expression near offset {pos}: {text[pos:pos + 40]!r}")
            break
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            if len(stack) == 1:
                raise ValueError(f"unbalanced ')' at offset {pos}")
            done = stack.pop()
            stack[-1].append(done)
        elif m.group(3) is not None:
            stack[-1].append(Q(_unescape(m.group(3))))
        else:
            stack[-1].append(m.group(4))
    if len(stack) != 1:
        raise ValueError("unbalanced '(' in S-expression")
    if not stack[0]:
        raise ValueError("empty S-expression")
    return stack[0][0]


# ---------------------------------------------------------------- navigation

def head(node) -> str | None:
    return node[0] if isinstance(node, list) and node and isinstance(node[0], str) else None


def children(node: list, key: str) -> list[list]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == key]


def child(node: list, key: str) -> list | None:
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == key:
            return c
    return None


def value(node: list | None, index: int = 1, default=None):
    if node is None or len(node) <= index:
        return default
    return node[index]


def number(x) -> float:
    return float(x)


def walk(node) -> Iterator[list]:
    if isinstance(node, list):
        yield node
        for c in node:
            yield from walk(c)


# ------------------------------------------------------------------- writing

def fmt_num(v: float) -> str:
    """Format a coordinate the way KiCad does: up to 4 decimals, no trailing zeros."""
    if isinstance(v, bool):
        raise TypeError("bool is not a number")
    if isinstance(v, int):
        return str(v)
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _atom(x) -> str:
    if isinstance(x, Q):
        return '"' + x.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return fmt_num(x)
    if isinstance(x, str):
        if not x or re.search(r'[\s()"]', x):
            raise ValueError(f"token {x!r} must be quoted; wrap it in Q()")
        return x
    raise TypeError(f"cannot serialise {type(x).__name__}: {x!r}")


# Lists whose contents KiCad keeps on one line even though they nest.
_INLINE_HEADS = {"at", "xy", "size", "offset", "color", "start", "end", "mid", "center",
                 "font", "stroke", "fill", "effects", "justify", "pts", "pin_names",
                 "pin_numbers", "width", "type", "length", "diameter", "radius", "thickness",
                 "hide", "show_name", "do_not_autoplace", "exclude_from_sim", "in_bom",
                 "on_board", "in_pos_files", "dnp", "unit", "body_style", "uuid", "lib_id",
                 "reference", "page", "shape", "fields_autoplaced", "mirror"}


def _inline_ok(node: list) -> bool:
    h = head(node)
    if h in ("pts",):
        return len(node) <= 5
    if h in ("effects", "stroke", "fill", "font"):
        return all(not isinstance(c, list) or _inline_ok(c) for c in node) and len(dumps_inline(node)) < 90
    if any(isinstance(c, list) for c in node[1:]):
        return h in _INLINE_HEADS and len(dumps_inline(node)) < 90
    return True


def dumps_inline(node) -> str:
    if isinstance(node, list):
        return "(" + " ".join(dumps_inline(c) for c in node) + ")"
    return _atom(node)


def dumps(node, indent: int = 0) -> str:
    """Serialise with KiCad-like layout: one child list per line, tabs for indent."""
    if not isinstance(node, list):
        return _atom(node)
    if _inline_ok(node):
        return dumps_inline(node)
    pad = "\t" * (indent + 1)
    parts: list[str] = []
    # leading atoms stay on the opening line
    i = 0
    first = []
    while i < len(node) and not isinstance(node[i], list):
        first.append(_atom(node[i]))
        i += 1
    out = "(" + " ".join(first)
    for c in node[i:]:
        if isinstance(c, list):
            parts.append("\n" + pad + dumps(c, indent + 1))
        else:
            parts.append(" " + _atom(c))
    return out + "".join(parts) + "\n" + "\t" * indent + ")"


def q(s: str) -> Q:
    return Q(s)


def flat(items: Iterable) -> list:
    """Drop ``None`` entries so optional children can be written inline."""
    return [x for x in items if x is not None]
