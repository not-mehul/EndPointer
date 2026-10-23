"""Verdict for one exchange: does the endpoint still behave as recorded?

Two signals:

1. **Status** — 404/405/410 suggests the route or method moved, 400/409/422
   that the payload is no longer accepted. 401/403 and 429/5xx are
   inconclusive (permissions / server trouble), not evidence of a change.
   An endpoint can list extra normal statuses in `response.status`.
2. **Shape** — the JSON body is compared with `response.schema`: missing
   fields and type changes count, new fields are informational.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from endpoint_checker import shapes
from endpoint_checker.catalog import Endpoint
from endpoint_checker.http import Exchange

OK = "ok"
CHANGED = "changed"  # might have changed
FAILING = "failing"  # route / method / payload rejected
INCONCLUSIVE = "inconclusive"  # auth, rate limit, server error
NOT_EXERCISED = "not exercised"
SEVERITY = {OK: 0, INCONCLUSIVE: 1, CHANGED: 2, FAILING: 3}


@dataclass
class Verdict:
    status: str
    reason: str
    diffs: list[shapes.Diff] = field(default_factory=list)

    @property
    def significant_diffs(self) -> list[shapes.Diff]:
        return [d for d in self.diffs if d.significant]

    @property
    def added(self) -> list[shapes.Diff]:
        return [d for d in self.diffs if d.kind == "added"]


def judge(exchange: Exchange, endpoint: Endpoint) -> Verdict:
    status = exchange.status
    message = exchange.message()
    suffix = f" — {message}" if message else ""
    if status == 0:
        return Verdict(INCONCLUSIVE, f"no response{suffix}")
    if endpoint.status_ok(status):
        if not 200 <= status < 300:
            return Verdict(OK, f"HTTP {status} (normal for this endpoint)")
        if not endpoint.schema or exchange.response_json is None:
            return Verdict(OK, f"HTTP {status}")
        diffs = shapes.compare(endpoint.schema, exchange.response_json)
        significant = [d for d in diffs if d.significant]
        if significant:
            return Verdict(
                CHANGED,
                f"HTTP {status}, response shape differs: " + "; ".join(str(d) for d in significant[:4]),
                diffs,
            )
        return Verdict(OK, f"HTTP {status}", diffs)
    if status in (404, 405, 410, 501):
        what = {405: "method no longer allowed", 501: "not implemented"}.get(
            status, "route or object not found — path may have changed"
        )
        return Verdict(FAILING, f"HTTP {status}: {what}{suffix}")
    if status in (400, 409, 415, 422):
        return Verdict(FAILING, f"HTTP {status}: request rejected — payload may have changed{suffix}")
    if status in (401, 403):
        return Verdict(INCONCLUSIVE, f"HTTP {status}: not permitted{suffix}")
    if status == 429 or status >= 500:
        return Verdict(INCONCLUSIVE, f"HTTP {status}: server/rate-limit error{suffix}")
    return Verdict(CHANGED, f"HTTP {status}: unexpected status{suffix}")


def combine(verdicts: list[Verdict]) -> Verdict:
    """Worst verdict of several calls; a retry that succeeded isn't 'inconclusive'."""
    if any(v.status == OK for v in verdicts):
        verdicts = [v for v in verdicts if v.status != INCONCLUSIVE] or verdicts
    worst = max(verdicts, key=lambda v: SEVERITY[v.status])
    diffs, seen = [], set()
    for v in verdicts:
        for d in v.diffs:
            if (d.kind, d.path) not in seen:
                seen.add((d.kind, d.path))
                diffs.append(d)
    return Verdict(worst.status, worst.reason, diffs)
