"""Dependencies between calls: which response a later request took its values from.

When a value from one call's response (an id, a cursor, a token) turns up in a
later request (its path, query, body or auth headers), the later call depends
on the earlier one. Calls are grouped into endpoints, so the graph has one node
per endpoint and one edge per producer → consumer pair, with the fields that
carried the value.

Only distinctive values count: ids, long strings and big numbers, never
`true`, `0`, `"en"` or other values that could match by coincidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from endpointer.model import REDACTED, Call, is_id_segment, is_secret_header

MAX_LEAVES = 5000
_SCHEME = re.compile(r"^(Bearer|Basic|Token|Digest|Bot)\s+", re.I)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_ID_FIELD = re.compile(r"(^id$|[a-z0-9]Id$|[_\-]id$|ID$)")
_BORING = {
    "true",
    "false",
    "null",
    "none",
    "undefined",
    "asc",
    "desc",
    "default",
    "active",
    "enabled",
    "disabled",
}


def distinctive(value: Any) -> str | None:
    """The value as a string if it is specific enough to link two calls, else None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value) if abs(value) >= 1000 else None
    if isinstance(value, float):
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) < 4 or len(value) > 4096 or value.lower() in _BORING:
        return None
    if REDACTED.match(value):
        return _SCHEME.sub("", value)
    if _DATE.match(value):
        return None
    if is_id_segment(value):
        return value
    if len(value) >= 8 and any(c.isdigit() for c in value) and " " not in value:
        return value
    return None


def leaves(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """(path, scalar) for every scalar in a JSON value; list indexes collapse to []."""
    stack = [(path, value)]
    count = 0
    while stack and count < MAX_LEAVES:
        p, v = stack.pop()
        if isinstance(v, dict):
            for k, sub in v.items():
                stack.append((f"{p}.{k}" if p else str(k), sub))
        elif isinstance(v, list):
            for sub in v[:200]:
                stack.append((f"{p}[]", sub))
        else:
            count += 1
            yield p, v


def weak_ids(call: Call) -> Iterator[tuple[str, str]]:
    """Short ids (`"id": 7`) from the response: too common to match anywhere, fine for path segments."""
    if call.response_body is None or call.response_kind != "json":
        return
    for path, v in leaves(call.response_body):
        name = path.rsplit(".", 1)[-1].replace("[]", "")
        if (
            _ID_FIELD.search(name)
            and not isinstance(v, bool)
            and isinstance(v, (int, str))
            and str(v)
            and distinctive(v) is None
        ):
            yield f"response.{path}", str(v)


def produced(call: Call) -> Iterator[tuple[str, str]]:
    """(where, value) pairs the call's response hands out."""
    if call.response_body is not None and call.response_kind == "json":
        for path, v in leaves(call.response_body):
            d = distinctive(v)
            if d:
                yield f"response.{path}" if path else "response", d
    for name, v in call.response_headers.items():
        if name == "set-cookie" or not is_secret_header(name):
            continue
        d = distinctive(_SCHEME.sub("", v))
        if d:
            yield f"header.{name}", d


def consumed(call: Call) -> Iterator[tuple[str, str]]:
    """(where, value) pairs the call's request uses."""
    for name, v in call.params.items():
        yield f"path.{{{name}}}", v
    for name, v in call.query.items():
        for part in str(v).split(","):
            d = distinctive(part)
            if d:
                yield f"query.{name}", d
    if call.request_body is not None and call.request_kind in ("json", "form"):
        for path, v in leaves(call.request_body):
            d = distinctive(v)
            if d:
                yield f"body.{path}" if path else "body", d
    for name, v in call.request_headers.items():
        if name == "cookie" or not is_secret_header(name):
            continue
        d = distinctive(_SCHEME.sub("", v))
        if d:
            yield f"header.{name}", d


@dataclass
class Link:
    source: str  # producer endpoint key
    target: str  # consumer endpoint key
    count: int = 0
    fields: list[tuple[str, str]] = field(default_factory=list)  # (producer field, consumer field)
    calls: list[tuple[str, str]] = field(
        default_factory=list
    )  # (producer call id, consumer call id), first few

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "count": self.count,
            "fields": [{"from": a, "to": b} for a, b in self.fields],
            "calls": [list(c) for c in self.calls],
        }


def links(calls: Iterable[Call]) -> list[Link]:
    """Endpoint-level dependencies, from calls in the order they happened."""
    latest: dict[str, tuple[Call, str]] = {}  # value → (most recent call that produced it, where)
    weak: dict[str, tuple[Call, str]] = {}  # short ids, matched against path segments only
    out: dict[tuple[str, str], Link] = {}
    for call in sorted(calls, key=lambda c: c.ts):
        for where, value in consumed(call):
            hit = latest.get(value) or (weak.get(value) if where.startswith("path.") else None)
            if not hit:
                continue
            source, source_where = hit
            if source.key == call.key:
                continue
            link = out.setdefault((source.key, call.key), Link(source.key, call.key))
            link.count += 1
            pair = (source_where, where)
            if pair not in link.fields and len(link.fields) < 8:
                link.fields.append(pair)
            if len(link.calls) < 5 and (source.id, call.id) not in link.calls:
                link.calls.append((source.id, call.id))
        if call.ok:
            for where, value in produced(call):
                latest[value] = (call, where)
            for where, value in weak_ids(call):
                weak[value] = (call, where)
    return list(out.values())


def feeds(link_list: Iterable[Link]) -> dict[str, int]:
    """Endpoint key → number of other endpoints that use its values."""
    counts: dict[str, int] = {}
    for link in link_list:
        counts[link.source] = counts.get(link.source, 0) + 1
    return counts
