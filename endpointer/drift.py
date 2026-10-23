"""Endpoint shapes, and drift between two looks at the same endpoints.

A *shape* summarises every call to one endpoint: the statuses it answered,
the query parameters and request body it was sent, and the JSON Schema of its
responses (inferred, see `shapes`). Your library stores a shape per saved
endpoint as its baseline; each capture can be turned into shapes too. Drift is
the difference between a baseline and a later shape:

| Level     | Means                                                              |
|-----------|--------------------------------------------------------------------|
| failing   | it used to succeed and now only errors (404, 405, 400, 422, …)      |
| changed   | a response field disappeared or changed type, or a new status code |
| unsure    | only "not now" answers: logged out (401/403), rate limit, 5xx      |
| minor     | new response fields, or the request is sent differently            |
| same      | nothing to report                                                  |
| new       | in the later look only                                             |
| not seen  | in the baseline only: not exercised this time (not proof it's gone) |
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from endpointer import shapes
from endpointer.model import Call

FAILING, CHANGED, UNSURE, MINOR, SAME, NEW, NOT_SEEN = (
    "failing",
    "changed",
    "unsure",
    "minor",
    "same",
    "new",
    "not seen",
)
ORDER = {FAILING: 0, CHANGED: 1, NEW: 2, UNSURE: 3, MINOR: 4, NOT_SEEN: 5, SAME: 6}
# Answers that say "not now" rather than "this changed": logged out, no permission, rate limit, server trouble.
INCONCLUSIVE = {0, 401, 403, 407, 408, 429, 500, 502, 503, 504}


@dataclass
class Shape:
    key: str
    method: str
    host: str
    template: str
    graphql_op: str = ""
    statuses: list[int] = field(default_factory=list)
    query: list[str] = field(default_factory=list)
    request_schema: dict = field(default_factory=dict)
    response_schema: dict = field(default_factory=dict)
    response_kind: str = ""
    count: int = 0
    first_seen: float = 0
    last_seen: float = 0
    example: str = ""  # id of a representative call

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "method": self.method,
            "host": self.host,
            "template": self.template,
            "graphql_op": self.graphql_op,
            "statuses": self.statuses,
            "query": self.query,
            "request_schema": self.request_schema,
            "response_schema": self.response_schema,
            "response_kind": self.response_kind,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "example": self.example,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Shape:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def add(self, call: Call) -> None:
        if call.status not in self.statuses:
            self.statuses = sorted([*self.statuses, call.status])
        for name in call.query:
            if name not in self.query:
                self.query = sorted([*self.query, name])
        if call.request_kind in ("json", "form") and call.request_body is not None:
            self.request_schema = shapes.merge(self.request_schema, shapes.infer(call.request_body))
        if call.ok and call.response_kind == "json":
            self.response_schema = shapes.merge(self.response_schema, shapes.infer(call.response_body))
        if call.ok and call.response_kind:
            self.response_kind = call.response_kind
        if not self.example or (call.ok and call.response_kind == "json"):
            self.example = call.id
        self.count += 1
        self.first_seen = min(self.first_seen or call.ts, call.ts)
        self.last_seen = max(self.last_seen, call.ts)


def shapes_of(calls: Iterable[Call]) -> dict[str, Shape]:
    out: dict[str, Shape] = {}
    for call in sorted(calls, key=lambda c: c.ts):
        shape = out.get(call.key)
        if shape is None:
            shape = out[call.key] = Shape(call.key, call.method, call.host, call.template, call.graphql_op)
        shape.add(call)
    return out


@dataclass
class Drift:
    key: str
    level: str
    notes: list[str] = field(default_factory=list)
    response: list[shapes.Diff] = field(default_factory=list)
    request: list[shapes.Diff] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "level": self.level,
            "notes": self.notes,
            "response": [
                {"kind": d.kind, "path": d.path, "detail": d.detail, "text": str(d)} for d in self.response
            ],
            "request": [
                {"kind": d.kind, "path": d.path, "detail": d.detail, "text": str(d)} for d in self.request
            ],
        }


def _ok(statuses: Iterable[int]) -> bool:
    return any(200 <= s < 400 for s in statuses)


def compare(old: Shape, new: Shape) -> Drift:
    """How `new` differs from the baseline `old` (the same endpoint)."""
    drift = Drift(new.key, SAME)
    level = SAME

    def raise_to(target: str) -> None:
        nonlocal level
        if ORDER[target] < ORDER[level]:
            level = target

    if _ok(old.statuses) and not _ok(new.statuses):
        if all(s in INCONCLUSIVE for s in new.statuses):
            raise_to(UNSURE)
            drift.notes.append(f"answered {_list(new.statuses)}: log in again (or retry) to check it")
        else:
            raise_to(FAILING)
            drift.notes.append(f"used to answer {_list(old.statuses)}, now only {_list(new.statuses)}")
    else:
        unexpected = [s for s in new.statuses if s not in old.statuses and s not in INCONCLUSIVE]
        if unexpected:
            raise_to(CHANGED if any(s >= 400 for s in unexpected) else MINOR)
            drift.notes.append(f"new status {_list(unexpected)} (before: {_list(old.statuses)})")
    if old.response_kind and new.response_kind and old.response_kind != new.response_kind:
        raise_to(CHANGED)
        drift.notes.append(f"response was {old.response_kind}, now {new.response_kind}")

    drift.response = shapes.diff_schemas(old.response_schema, new.response_schema)
    if any(d.significant for d in drift.response):
        raise_to(CHANGED)
    elif any(d.kind == "added" for d in drift.response):
        raise_to(MINOR)

    drift.request = shapes.diff_schemas(old.request_schema, new.request_schema)
    added_q = [q for q in new.query if q not in old.query]
    if added_q:
        drift.notes.append("new query parameter " + ", ".join(added_q))
    if drift.request or added_q:
        raise_to(MINOR)
    drift.level = level
    return drift


def compare_all(baseline: dict[str, Shape], later: dict[str, Shape]) -> list[Drift]:
    """Drift for every endpoint in either, worst first."""
    out = []
    for key, shape in later.items():
        out.append(
            compare(baseline[key], shape) if key in baseline else Drift(key, NEW, ["not in the baseline"])
        )
    for key in baseline:
        if key not in later:
            out.append(Drift(key, NOT_SEEN, ["not called this time"]))
    return sorted(out, key=lambda d: (ORDER[d.level], d.key))


def _list(statuses: Iterable[int]) -> str:
    return ", ".join("no response" if s == 0 else str(s) for s in statuses) or "nothing"
