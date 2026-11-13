"""What the window shows: the live capture, saved captures, the library, drift, the graph.

`App` owns the capture browser and the live capture, and builds every view
from calls: relevance (`classify`), endpoint shapes and drift (`drift`), and
dependencies (`deps`). The HTTP layer in `server` only translates requests.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from endpointer import cdp, classify, deps, drift, har
from endpointer.model import Call, is_secret_header
from endpointer.store import Capture, Store, new_capture_id
import contextlib

LIVE = "live"
AUTOSAVE_SECONDS = 15


@dataclass
class Analysis:
    calls: list[Call]
    relevance: dict[str, classify.Relevance]  # call id → relevance
    shapes: dict[str, drift.Shape]
    endpoint_relevance: dict[str, classify.Relevance]
    links: list[deps.Link]


def analyse(calls: list[Call], *, rules: classify.Rules, saved: set[str]) -> Analysis:
    useful = [c for c in calls if not classify.is_static(c)]
    links = deps.links(useful)
    feeds = deps.feeds(links)
    sites = {classify.site_of(urlsplit(c.page).netloc) for c in calls if c.page.startswith("http")}
    sites |= {classify.site_of(c.host) for c in calls if c.resource_type == "document"}
    relevance = {
        c.id: classify.score(c, rules=rules, sites=sites, saved=saved, feeds=feeds.get(c.key, 0))
        for c in calls
    }
    endpoint_relevance: dict[str, classify.Relevance] = {}
    for c in calls:
        r = relevance[c.id]
        best = endpoint_relevance.get(c.key)
        if best is None or r.score > best.score:
            endpoint_relevance[c.key] = r
    return Analysis(calls, relevance, drift.shapes_of(calls), endpoint_relevance, links)


class App:
    def __init__(self, store: Store, *, browser: str | None = None, headless: bool = False):
        self.store = store
        self.browser_path = cdp.find_browser(browser or store.setting("browser"))
        self.headless = headless
        self.lock = threading.RLock()
        self.version = 0
        self.live = Capture(new_capture_id(), "Live capture", time.time())
        self.live_dirty = False
        self.process = None
        self.recorder: cdp.Recorder | None = None
        self.ignored_origins: set[str] = set()
        self.skipped: dict[
            str, dict[str, str]
        ] = {}  # library check capture id → endpoint key → why it wasn't replayed
        self._cache: dict[str, tuple[int, Analysis]] = {}
        self._stop = threading.Event()
        threading.Thread(target=self._autosave, name="autosave", daemon=True).start()

    # --- capture browser ----------------------------------------------

    def _on_call(self, call: Call) -> None:
        with self.lock:
            self.live.calls.append(call)
            self.live.ended = call.ts
            self.live_dirty = True
            self.version += 1

    def _ignore(self, url: str) -> bool:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}" in self.ignored_origins

    def browser_status(self) -> dict:
        r = self.recorder
        return {
            "found": bool(self.browser_path),
            "path": self.browser_path or "",
            "connected": bool(r and r.alive),
            "recording": bool(r and r.alive and r.recording),
            "tabs": [
                {"url": p.get("url", ""), "title": p.get("title", "")}
                for p in (r.pages() if r and r.alive else [])
            ],
            "error": (r.error if r and not r.alive else "") or "",
        }

    def launch_browser(self, url: str = "") -> dict:
        if self.recorder and self.recorder.alive:
            if url:
                self.recorder.navigate(url)
            return self.browser_status()
        if not self.browser_path:
            raise RuntimeError(
                "No Chrome, Edge, Brave or Chromium found. Install one, or set ENDPOINTER_BROWSER to its path, "
                "or start one yourself with --remote-debugging-port=9222 and attach to it."
            )
        # Start blank and navigate once recording is on, so the page's first calls are captured too.
        self.process, port = cdp.launch(self.browser_path, self.store.profile_dir, headless=self.headless)
        self._connect(port)
        if url and self.recorder:
            self.recorder.wait_ready()
            self.recorder.navigate(url)
        return self.browser_status()

    def attach_browser(self, port: int, host: str = "127.0.0.1") -> dict:
        self._connect(port, host)
        return self.browser_status()

    def _connect(self, port: int, host: str = "127.0.0.1") -> None:
        deadline = time.monotonic() + 10
        while True:
            try:
                ws_url = cdp.browser_ws_url(port, host)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        if self.recorder:
            self.recorder.close()
        self.recorder = cdp.Recorder(ws_url, self._on_call, ignore=self._ignore)
        with self.lock:
            self.version += 1

    def stop_browser(self) -> None:
        if self.recorder:
            self.recorder.close()
            self.recorder = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except Exception:
                self.process.kill()
        self.process = None
        self.save_live()
        with self.lock:
            self.version += 1

    def set_recording(self, on: bool) -> None:
        if self.recorder:
            self.recorder.recording = on
        with self.lock:
            self.version += 1

    def shutdown(self) -> None:
        self._stop.set()
        self.stop_browser()

    # --- captures -------------------------------------------------------

    def _autosave(self) -> None:
        while not self._stop.wait(AUTOSAVE_SECONDS):
            with contextlib.suppress(OSError):
                self.save_live()

    def save_live(self, name: str | None = None) -> dict:
        with self.lock:
            if name:
                self.live.name = name
            if self.live.calls and (self.live_dirty or name):
                self.store.save_capture(self.live)
                self.live_dirty = False
            return self.live.meta()

    def new_live(self) -> dict:
        with self.lock:
            self.save_live()
            self.live = Capture(new_capture_id(), "Live capture", time.time())
            self.version += 1
            return self.live.meta()

    def clear_live(self) -> None:
        with self.lock:
            self.live.calls = []
            self.store.delete_capture(self.live.id)
            self.version += 1

    def capture(self, capture_id: str) -> Capture:
        if capture_id in (LIVE, self.live.id):
            return self.live
        return self.store.load_capture(capture_id)

    def captures(self) -> list[dict]:
        saved = [c for c in self.store.captures() if c["id"] != self.live.id]
        return [{**self.live.meta(), "live": True}, *saved]

    def import_har(self, data: dict, name: str) -> dict:
        capture = Capture(new_capture_id(), name or "Imported HAR", time.time())
        capture.calls = har.calls_from_har(data, prefix=capture.id)
        if capture.calls:
            capture.started = min(c.ts for c in capture.calls) or capture.started
            capture.ended = max(c.ts for c in capture.calls)
        self.store.save_capture(capture)
        with self.lock:
            self.version += 1
        return capture.meta()

    def export_har(self, capture_id: str) -> dict:
        capture = self.capture(capture_id)
        return har.har_from_calls([c.redacted(self.store.redactor) for c in capture.calls], name="endpointer")

    # --- views ----------------------------------------------------------

    def analysis(self, capture_id: str) -> Analysis:
        capture = self.capture(capture_id)
        stamp = self.version
        cached = self._cache.get(capture.id)
        if cached and cached[0] == stamp:
            return cached[1]
        with self.lock:
            calls = list(capture.calls)
        result = analyse(calls, rules=self.store.rules, saved={e["key"] for e in self.store.library()})
        self._cache = {
            capture.id: (stamp, result),
            **{k: v for k, v in self._cache.items() if k != capture.id},
        }
        if len(self._cache) > 6:
            self._cache.pop(next(reversed(self._cache)))
        return result

    def overview(self, capture_id: str, *, show_all: bool = False) -> dict:
        a = self.analysis(capture_id)
        baselines = self.store.baselines()
        endpoints = []
        for key, shape in a.shapes.items():
            r = a.endpoint_relevance[key]
            if not show_all and not r.important:
                continue
            level = drift.compare(baselines[key], shape).level if key in baselines else ""
            endpoints.append(
                {
                    **_shape_summary(shape),
                    "important": r.important,
                    "score": r.score,
                    "reasons": r.reasons,
                    "saved": key in baselines,
                    "drift": level,
                }
            )
        endpoints.sort(key=lambda e: -e["last_seen"])
        shown = {e["key"] for e in endpoints}
        calls = [
            {**c.summary(), "important": a.relevance[c.id].important, "score": a.relevance[c.id].score}
            for c in a.calls
            if show_all or a.relevance[c.id].important
        ]
        hidden = sum(1 for c in a.calls if not a.relevance[c.id].important)
        return {
            "capture": capture_id,
            "version": self.version,
            "calls": calls[-2000:],
            "endpoints": endpoints,
            "links": [link.to_dict() for link in a.links if link.source in shown and link.target in shown],
            "counts": {
                "calls": len(a.calls),
                "hidden": hidden,
                "endpoints": len(a.shapes),
                "important": sum(1 for r in a.endpoint_relevance.values() if r.important),
            },
        }

    def call(self, capture_id: str, call_id: str) -> dict:
        capture = self.capture(capture_id)
        for c in capture.calls:
            if c.id == call_id:
                a = self.analysis(capture_id)
                r = a.relevance.get(c.id)
                return {
                    **c.redacted(self.store.redactor).to_dict(),
                    "key": c.key,
                    "template": c.template,
                    "host": c.host,
                    "full_url": c.redacted(self.store.redactor).full_url,
                    "relevance": {"score": r.score, "important": r.important, "reasons": r.reasons}
                    if r
                    else None,
                    "uses": [link.to_dict() for link in a.links if link.target == c.key],
                    "feeds": [link.to_dict() for link in a.links if link.source == c.key],
                }
        raise KeyError(call_id)

    def endpoint(self, capture_id: str, key: str) -> dict:
        a = self.analysis(capture_id)
        shape = a.shapes.get(key)
        entry = self.store.entry(key)
        if shape is None and entry is None:
            raise KeyError(key)
        out: dict[str, Any] = {"key": key, "library": entry}
        if shape is not None:
            out["shape"] = shape.to_dict()
            out["calls"] = [c.summary() for c in a.calls if c.key == key][-50:]
            r = a.endpoint_relevance[key]
            out["relevance"] = {"score": r.score, "important": r.important, "reasons": r.reasons}
            if entry and entry.get("baseline"):
                out["drift"] = drift.compare(drift.Shape.from_dict(entry["baseline"]), shape).to_dict()
        out["uses"] = [link.to_dict() for link in a.links if link.target == key]
        out["feeds"] = [link.to_dict() for link in a.links if link.source == key]
        return out

    # --- library --------------------------------------------------------

    def library(self) -> list[dict]:
        out = []
        for e in self.store.library():
            base = e.get("baseline") or {}
            out.append(
                {
                    "key": e["key"],
                    "name": e.get("name", ""),
                    "notes": e.get("notes", ""),
                    "tags": e.get("tags", []),
                    "method": base.get("method", ""),
                    "host": base.get("host", ""),
                    "template": base.get("template", ""),
                    "graphql_op": base.get("graphql_op", ""),
                    "statuses": base.get("statuses", []),
                    "saved": e.get("saved"),
                    "updated": e.get("updated"),
                    "last_check": e.get("last_check"),
                    "history": len(e.get("history", [])),
                }
            )
        return out

    def save_endpoints(self, capture_id: str, keys: list[str]) -> list[dict]:
        capture = self.capture(capture_id)
        a = self.analysis(capture_id)
        by_id = {c.id: c for c in capture.calls}
        saved = []
        for key in keys:
            shape = a.shapes.get(key)
            if shape is None:
                continue
            saved.append(self.store.save_endpoint(shape, by_id.get(shape.example), capture_id=capture.id))
        with self.lock:
            self.version += 1
        return saved

    def accept(self, capture_id: str, key: str) -> dict:
        """Make what this capture saw the endpoint's new baseline (the old one goes to history)."""
        result = self.save_endpoints(capture_id, [key])
        if not result:
            raise KeyError(key)
        return result[0]

    def update_endpoint(self, key: str, **fields: Any) -> dict:
        entry = self.store.update_endpoint(key, **fields)
        with self.lock:
            self.version += 1
        return entry

    def remove_endpoint(self, key: str) -> None:
        self.store.remove_endpoint(key)
        with self.lock:
            self.version += 1

    # --- drift ----------------------------------------------------------

    def drift(self, capture_id: str, against: str = "library", *, record: bool = False) -> dict:
        a = self.analysis(capture_id)
        later = {k: s for k, s in a.shapes.items() if a.endpoint_relevance[k].important}
        if against == "library":
            baseline, label = self.store.baselines(), "your library"
        else:
            b = self.analysis(against)
            baseline = {k: s for k, s in b.shapes.items() if b.endpoint_relevance[k].important}
            label = next((c["name"] for c in self.captures() if c["id"] == against), against)
        report = self._drift_report(
            baseline, later, capture_id=capture_id, against=against, label=label, record=record
        )
        why = self.skipped.get(self.capture(capture_id).id, {}) if against == "library" else {}
        for result in report["results"]:
            if result["key"] in why and result["level"] == drift.NOT_SEEN:
                result["notes"] = [f"not replayed: {why[result['key']]}"]
        return report

    def _drift_report(
        self, baseline: dict, later: dict, *, capture_id: str, against: str, label: str, record: bool
    ) -> dict:
        results = drift.compare_all(baseline, later)
        if against == "library" and record:
            now = time.time()
            for d in results:
                if d.level not in (drift.NEW, drift.NOT_SEEN):
                    with contextlib.suppress(KeyError):
                        self.store.update_endpoint(
                            d.key,
                            last_check={"at": now, "capture": self.capture(capture_id).id, "level": d.level},
                        )
        summary: dict[str, int] = {}
        for d in results:
            summary[d.level] = summary.get(d.level, 0) + 1
        return {
            "capture": capture_id,
            "against": against,
            "against_label": label,
            "summary": summary,
            "results": [
                {**d.to_dict(), **_shape_summary(later.get(d.key) or baseline[d.key])} for d in results
            ],
        }

    def check_library(self, *, wait: float = 6) -> dict:
        """Replay every saved GET endpoint from the capture browser, then compare the live capture to the library.

        Each request is sent from a tab on the page that originally made it, so it
        carries your current session. Only GET requests are replayed: nothing is
        created, changed or deleted.
        """
        r = self.recorder
        if not (r and r.alive):
            raise RuntimeError("Open the capture browser first (and log in to the sites you check).")
        sent, skipped = [], []
        started = time.time()
        for entry in self.store.library():
            example = entry.get("example") or {}
            if example.get("method") != "GET" or not example.get("url"):
                skipped.append({"key": entry["key"], "why": "not a GET request"})
                continue
            call = Call.from_dict(example)
            if any("<secret:" in str(v) for v in call.query.values()):
                skipped.append({"key": entry["key"], "why": "needs a secret in its URL"})
                continue
            origin = (
                urlsplit(call.page).scheme + "://" + urlsplit(call.page).netloc
                if call.page.startswith("http")
                else call.origin
            )
            if not any(p.get("url", "").startswith(origin) for p in r.pages()):
                try:
                    r.open(call.page if call.page.startswith("http") else origin + "/")
                    time.sleep(3)
                except cdp.CDPError as e:
                    skipped.append({"key": entry["key"], "why": str(e)})
                    continue
            headers = {
                k: v
                for k, v in call.request_headers.items()
                if k in ("accept", "content-type") or (k.startswith("x-") and "<secret:" not in v)
            }
            headers.update(self._current_auth(call.host))
            script = (
                f"fetch({json.dumps(call.full_url)}, {{credentials: 'include', headers: {json.dumps(headers)}}})"
                ".then(r => r.status).catch(e => String(e))"
            )
            try:
                result = r.evaluate(script, prefer_origin=origin)
                sent.append({"key": entry["key"], "result": result})
            except cdp.CDPError as e:
                skipped.append({"key": entry["key"], "why": str(e)})
        time.sleep(min(wait, 1 + 0.2 * len(sent)))
        # Judge only what the replay just saw: earlier calls in the live capture would blur the comparison.
        # The replayed calls become their own capture, so the check can be looked at again later
        # and earlier calls in the live capture don't blur the comparison.
        with self.lock:
            fresh = [c for c in self.live.calls if c.ts >= started]
            self.live.calls = [c for c in self.live.calls if c.ts < started]
            self.live_dirty = True
            check = Capture(
                new_capture_id(), time.strftime("Library check %b %d %H:%M"), started, time.time(), fresh
            )
            self.store.save_capture(check)
            self.version += 1
        self.drift(check.id, "library", record=True)
        self.skipped[check.id] = {s["key"]: s["why"] for s in skipped}
        report = self.drift(check.id, "library")
        report["replayed"] = sent
        report["skipped"] = skipped
        return report

    def _current_auth(self, host: str) -> dict[str, str]:
        """Auth headers (Authorization, X-…-Token) of the latest live call to `host`: your current session.

        Cookies aren't copied (the browser adds its own with `credentials: 'include'`).
        """
        with self.lock:
            calls = list(self.live.calls)
        for c in reversed(calls):
            if c.host != host:
                continue
            found = {
                k: v
                for k, v in c.request_headers.items()
                if k != "cookie" and is_secret_header(k) and "<secret:" not in v
            }
            if found:
                return found
        return {}

    # --- rules ----------------------------------------------------------

    def set_rule(self, kind: str, value: str, on: bool) -> dict:
        rules = self.store.rules
        if kind not in ("hide_hosts", "hide_keys", "pin_hosts", "pin_keys"):
            raise KeyError(kind)
        values: list[str] = getattr(rules, kind)
        if on and value not in values:
            values.append(value)
        if not on and value in values:
            values.remove(value)
        opposite = {
            "hide_hosts": "pin_hosts",
            "pin_hosts": "hide_hosts",
            "hide_keys": "pin_keys",
            "pin_keys": "hide_keys",
        }[kind]
        if on and value in getattr(rules, opposite):
            getattr(rules, opposite).remove(value)
        self.store.set_rules(rules)
        with self.lock:
            self.version += 1
        return rules.to_dict()


def _shape_summary(shape: drift.Shape) -> dict:
    return {
        "key": shape.key,
        "method": shape.method,
        "host": shape.host,
        "template": shape.template,
        "graphql_op": shape.graphql_op,
        "statuses": shape.statuses,
        "count": shape.count,
        "first_seen": shape.first_seen,
        "last_seen": shape.last_seen,
        "example": shape.example,
    }


def load_har_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
