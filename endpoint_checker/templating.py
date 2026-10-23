"""`{{variable}}` templates and response paths.

Templates
---------
Any string in an endpoint or scenario may contain `{{name}}`. A string that
is exactly one variable keeps the variable's type (a list stays a list, a
number a number); otherwise the value is inserted as text. `{{name|text}}`
uses `text` when `name` is not set.

Generated values:

    {{$stamp}}      run timestamp, fixed for the whole run (20261023-142501)
    {{$now}}        unix seconds; {{$now+3600}} / {{$now-60}} offsets
    {{$now_ms}}     unix milliseconds
    {{$uuid}}       random UUID
    {{$digits:6}}   random digits

Paths
-----
Saving values from a response and checking it use a small path language:

    cameraGroups[0].cameraGroupId     first group's id
    sites[*].siteId                   every site id (a list)
    apiKeys[?apiKeyName==my key].apiKeyId   filter a list by a field
    $                                 the whole body

A trailing `?` (`sites[0].siteId?`) marks a path as optional.
"""

from __future__ import annotations

import random
import re
import time
import uuid
from typing import Any

VAR = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_NOW = re.compile(r"^\$now(?:([+-])(\d+))?$")
_DIGITS = re.compile(r"^\$digits:(\d{1,3})$")

# Filled in by the session and the login.
SESSION_VARS = ("org_id", "user_id", "email", "org_short_name", "region", "shard")


class Unresolved(Exception):
    """A template referenced variables that aren't set."""

    def __init__(self, names):
        self.names = sorted(set(names))
        super().__init__("needs " + ", ".join("{{" + n + "}}" for n in self.names))


class PathError(Exception):
    """A required response path didn't match anything."""


def generated(name: str, stamp: str) -> Any:
    if name == "$stamp":
        return stamp
    if name == "$now_ms":
        return int(time.time() * 1000)
    if name == "$uuid":
        return str(uuid.uuid4())
    m = _NOW.match(name)
    if m:
        offset = int(m.group(2) or 0) * (-1 if m.group(1) == "-" else 1)
        return int(time.time()) + offset
    m = _DIGITS.match(name)
    if m:
        return "".join(random.choice("0123456789") for _ in range(int(m.group(1))))
    raise KeyError(name)


def _lookup(expr: str, scope: dict, stamp: str, missing: list) -> Any:
    name, sep, default = expr.partition("|")
    name = name.strip()
    if name.startswith("$"):
        try:
            return generated(name, stamp)
        except KeyError:
            missing.append(name)
            return None
    value = scope.get(name)
    if value is None or value == "":
        if sep:
            return default
        missing.append(name)
        return None
    return value


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def render(value: Any, scope: dict, *, stamp: str = "") -> Any:
    """Substitute variables everywhere in `value`; raise Unresolved if any are missing."""
    missing: list[str] = []
    out = _render(value, scope, stamp, missing)
    if missing:
        raise Unresolved(missing)
    return out


def _render(value: Any, scope: dict, stamp: str, missing: list) -> Any:
    if isinstance(value, dict):
        return {k: _render(v, scope, stamp, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, scope, stamp, missing) for v in value]
    if not isinstance(value, str) or "{{" not in value:
        return value
    whole = VAR.fullmatch(value)
    if whole:
        return _lookup(whole.group(1), scope, stamp, missing)
    return VAR.sub(lambda m: _text(_lookup(m.group(1), scope, stamp, missing)), value)


def variables(value: Any) -> set[str]:
    """Names of the (non-generated) variables a template uses."""
    found: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            found |= variables(v)
    elif isinstance(value, list):
        for v in value:
            found |= variables(v)
    elif isinstance(value, str):
        for m in VAR.finditer(value):
            name, sep, _ = m.group(1).partition("|")
            name = name.strip()
            if not name.startswith("$") and not sep:
                found.add(name)
    return found


# ----------------------------------------------------------------------
# Response paths
# ----------------------------------------------------------------------

_SEGMENT = re.compile(r"([^.\[\]]*)((?:\[[^\]]*\])*)")
_INDEX = re.compile(r"\[([^\]]*)\]")


def _split(path: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for ch in path:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "." and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [p for p in parts if p]


def query(data: Any, path: str) -> list[Any]:
    """Every value `path` selects in `data` (empty list when nothing matches)."""
    path = path.strip().rstrip("?").strip()
    if path in ("", "$"):
        return [data]
    if path.startswith("$."):
        path = path[2:]
    nodes = [data]
    for segment in _split(path):
        m = _SEGMENT.fullmatch(segment)
        if not m:
            raise ValueError(f"bad path segment {segment!r} in {path!r}")
        key, brackets = m.group(1), m.group(2)
        if key:
            nodes = [n[key] for n in nodes if isinstance(n, dict) and key in n]
        for op in _INDEX.findall(brackets):
            nodes = _apply(nodes, op.strip())
    return nodes


def _apply(nodes: list, op: str) -> list:
    out: list = []
    for node in nodes:
        if not isinstance(node, list):
            continue
        if op == "*":
            out.extend(node)
        elif op.startswith("?"):
            field, eq, wanted = op[1:].partition("==")
            negate = False
            if not eq:
                field, eq, wanted = op[1:].partition("!=")
                negate = True
            if not eq:
                raise ValueError(f"bad filter [{op}]")
            field, wanted = field.strip(), wanted.strip()
            out.extend(
                item
                for item in node
                if isinstance(item, dict) and field in item and (_text(item[field]) == wanted) != negate
            )
        else:
            try:
                out.append(node[int(op)])
            except (ValueError, IndexError):
                continue
    return out


def is_optional(path: str) -> bool:
    return path.strip().endswith("?")


def extract(data: Any, path: str) -> Any:
    """The value to save for `path`: every match for [*], else the first match.

    Raises PathError when a required path matches nothing; returns None for
    an optional one.
    """
    matches = [m for m in query(data, path) if m is not None]
    if not matches:
        if is_optional(path):
            return None
        raise PathError(f"response has nothing at {path.rstrip('?')}")
    return matches if "[*]" in path else matches[0]
