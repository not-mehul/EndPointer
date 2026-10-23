"""Console, Markdown and JSON output for a check."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from endpoint_checker.judge import CHANGED, FAILING, INCONCLUSIVE, NOT_EXERCISED, OK
from endpoint_checker.runner import CheckResult, EndpointResult

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_CODES = {"red": "31", "yellow": "33", "green": "32", "grey": "90", "bold": "1"}

SYMBOL = {OK: "✔", CHANGED: "⚠", FAILING: "✖", INCONCLUSIVE: "?", NOT_EXERCISED: "–"}
COLOR = {OK: "green", CHANGED: "yellow", FAILING: "red", INCONCLUSIVE: "grey", NOT_EXERCISED: "grey"}
TITLE = {
    FAILING: "FAILING — route, method or payload rejected",
    CHANGED: "MIGHT HAVE CHANGED — response shape or status differs",
    INCONCLUSIVE: "INCONCLUSIVE — permission / rate-limit / server errors",
}


def paint(text: str, color: str) -> str:
    return f"\033[{_CODES[color]}m{text}\033[0m" if _COLOR else text


def counts(result: CheckResult) -> dict[str, int]:
    out = {OK: 0, CHANGED: 0, FAILING: 0, INCONCLUSIVE: 0, NOT_EXERCISED: 0}
    for endpoint in result.endpoints.values():
        out[endpoint.status] += 1
    return out


def _statuses(e: EndpointResult) -> str:
    return ",".join(str(s) for s in sorted(set(e.statuses))) or "—"


def print_summary(result: CheckResult) -> None:
    c = counts(result)
    print()
    print(paint(f"Endpoint check — {result.catalog}" + (f" on {result.org}" if result.org else "") + f" ({result.mode})", "bold"))
    print(
        "  "
        + "   ".join(
            paint(f"{SYMBOL[s]} {c[s]} {label}", COLOR[s])
            for s, label in (
                (OK, "ok"),
                (CHANGED, "might have changed"),
                (FAILING, "failing"),
                (INCONCLUSIVE, "inconclusive"),
                (NOT_EXERCISED, "not exercised"),
            )
        )
    )
    for status in (FAILING, CHANGED, INCONCLUSIVE):
        items = result.by_status(status)
        if not items:
            continue
        print()
        print(paint(TITLE[status], COLOR[status]))
        for e in items:
            reason = e.verdict.reason if e.verdict else ""
            print(f"  {paint(SYMBOL[status], COLOR[status])} {e.id:44} {e.method:6} {_statuses(e):9} {reason}")
            print(paint(f"      {e.where}", "grey"))
            for d in (e.verdict.significant_diffs if e.verdict else [])[:8]:
                print(f"      {d}")
            for note in e.notes:
                print(f"      note: {note}")
    added = [e for e in result.by_status(OK) if e.verdict and e.verdict.added]
    if added:
        print()
        print(paint("OK, with new fields in the response (informational)", "grey"))
        for e in added:
            fields = ", ".join(d.path for d in e.verdict.added[:6])
            more = f" (+{len(e.verdict.added) - 6} more)" if len(e.verdict.added) > 6 else ""
            print(paint(f"  {e.id}: {fields}{more}", "grey"))
    if result.unknown:
        print()
        print(paint(f"{len(result.unknown)} call(s) that match no endpoint in the catalog (worth recording?):", "yellow"))
        for ex in result.unknown[:15]:
            print(f"  {ex.method:6} {ex.url} → {ex.status}")
    if result.leftovers:
        print()
        print(paint("Cleanup problems — check the org for leftovers named 'Endpoint check …':", "red"))
        for item in result.leftovers:
            print(f"  ! {item}")
    if result.interrupted:
        print(paint("Interrupted — results cover the steps that ran.", "yellow"))


def to_markdown(result: CheckResult) -> str:
    c = counts(result)
    lines = [
        f"# Endpoint check — {result.catalog}",
        "",
        f"- Org: {result.org or '—'}",
        f"- Mode: {result.mode}",
        f"- Scenarios: {', '.join(result.scenarios) or '—'}",
        f"- Started: {result.started} · Finished: {result.finished}",
        f"- Result: {c[OK]} ok · {c[CHANGED]} might have changed · {c[FAILING]} failing · "
        f"{c[INCONCLUSIVE]} inconclusive · {c[NOT_EXERCISED]} not exercised",
        "",
    ]
    if result.leftovers:
        lines += ["## Cleanup problems", ""] + [f"- {x}" for x in result.leftovers] + [""]
    for status in (FAILING, CHANGED, INCONCLUSIVE):
        items = result.by_status(status)
        if not items:
            continue
        lines += [f"## {TITLE[status]}", "", "| Endpoint | Method | Where | Status | Why |", "|---|---|---|---|---|"]
        for e in items:
            why = (e.verdict.reason if e.verdict else "").replace("|", "\\|")
            details = [str(d) for d in (e.verdict.significant_diffs if e.verdict else [])] + [f"note: {n}" for n in e.notes]
            if details:
                why += "<br>" + "<br>".join(x.replace("|", "\\|") for x in details[:10])
            lines.append(f"| `{e.id}` | {e.method} | `{e.where}` | {_statuses(e)} | {why} |")
        lines.append("")
    oks = result.by_status(OK)
    if oks:
        lines += ["## OK", "", "| Endpoint | Calls | New fields |", "|---|---|---|"]
        for e in oks:
            added = ", ".join(f"`{d.path}`" for d in (e.verdict.added if e.verdict else [])[:6])
            lines.append(f"| `{e.id}` | {e.calls} | {added or '—'} |")
        lines.append("")
    skipped = result.by_status(NOT_EXERCISED)
    if skipped:
        lines += ["## Not exercised", "", "| Endpoint | Why |", "|---|---|"]
        lines += [f"| `{e.id}` | {e.reason_not_exercised.replace('|', chr(92) + '|')} |" for e in skipped]
        lines.append("")
    if result.unknown:
        lines += ["## Calls that match no endpoint", ""]
        lines += [f"- `{ex.method} {ex.url}` → {ex.status}" for ex in result.unknown]
        lines.append("")
    if result.steps:
        lines += ["## Steps", "", "| Scenario | Step | Result | Detail |", "|---|---|---|---|"]
        for s in result.steps:
            lines.append(f"| {s.scenario} | {s.step} | {s.state} | {s.detail.replace('|', chr(92) + '|')} |")
        lines.append("")
    return "\n".join(lines)


def to_json(result: CheckResult) -> dict:
    return {
        "catalog": result.catalog,
        "org": result.org,
        "mode": result.mode,
        "scenarios": result.scenarios,
        "started": result.started,
        "finished": result.finished,
        "interrupted": result.interrupted,
        "counts": counts(result),
        "endpoints": {
            e.id: {
                "status": e.status,
                "method": e.method,
                "where": e.where,
                "calls": e.calls,
                "http": e.statuses,
                "reason": e.verdict.reason if e.verdict else e.reason_not_exercised,
                "diffs": [str(d) for d in (e.verdict.diffs if e.verdict else [])],
                "notes": e.notes,
            }
            for e in sorted(result.endpoints.values(), key=lambda e: e.id)
        },
        "steps": [s.__dict__ for s in result.steps],
        "leftovers": result.leftovers,
        "unmatched_calls": [f"{x.method} {x.url} -> {x.status}" for x in result.unknown],
    }


def save(result: CheckResult, directory: str | Path) -> tuple[Path, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y%m%d_%H%M%S") + "_endpoint_check"
    md = directory / f"{stem}.md"
    js = directory / f"{stem}.json"
    md.write_text(to_markdown(result), encoding="utf-8")
    js.write_text(json.dumps(to_json(result), indent=2, default=str), encoding="utf-8")
    return md, js
