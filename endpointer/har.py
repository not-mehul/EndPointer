"""HAR files (DevTools → Network → "Save all as HAR with content"), in and out.

Importing a HAR gives the same calls as a live capture, so a session recorded
in any browser (Firefox and Safari included) can be filtered, saved, graphed
and checked for drift.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from endpointer.model import Call, make_call


def _headers(items: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in items or []:
        name = str(h.get("name", "")).lower()
        if name and not name.startswith(":"):
            out[name] = f"{out[name]}, {h.get('value', '')}" if name in out else str(h.get("value", ""))
    return out


def _ts(text: str) -> float:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def calls_from_har(data: dict | str, *, prefix: str = "har") -> list[Call]:
    if isinstance(data, str):
        data = json.loads(data)
    entries = (data or {}).get("log", {}).get("entries", [])
    pages = {p.get("id"): p.get("title", "") for p in (data or {}).get("log", {}).get("pages", [])}
    out = []
    for i, entry in enumerate(entries, 1):
        request, response = entry.get("request", {}), entry.get("response", {})
        url = request.get("url", "")
        if not url.startswith(("http://", "https://")):
            continue
        content = response.get("content") or {}
        out.append(
            make_call(
                id=f"{prefix}-{i}",
                ts=_ts(entry.get("startedDateTime", "")),
                method=request.get("method", "GET"),
                url=url,
                request_headers=_headers(request.get("headers")),
                request_text=(request.get("postData") or {}).get("text"),
                status=int(response.get("status") or 0),
                response_headers=_headers(response.get("headers")),
                response_text=content.get("text"),
                response_base64=content.get("encoding") == "base64",
                mime=content.get("mimeType", ""),
                resource_type=entry.get("_resourceType", ""),
                page=pages.get(entry.get("pageref"), ""),
                duration_ms=int(entry.get("time") or 0),
                size=int(content.get("size") or 0) if (content.get("size") or 0) > 0 else 0,
                error=entry.get("_error") or "",
            )
        )
    return out


def _body_text(value: Any, kind: str) -> str | None:
    if value is None:
        return None
    if kind == "json":
        return json.dumps(value)
    if kind == "form" and isinstance(value, dict):
        from urllib.parse import urlencode

        return urlencode(value)
    return str(value)


def har_from_calls(calls: list[Call], *, name: str = "endpointer") -> dict:
    """A HAR 1.2 document (for Postman, Insomnia, Charles, or the browser's own DevTools)."""
    entries = []
    for c in sorted(calls, key=lambda c: c.ts):
        body = _body_text(c.request_body, c.request_kind)
        res = _body_text(c.response_body, c.response_kind)
        entries.append(
            {
                "startedDateTime": datetime.fromtimestamp(c.ts).astimezone().isoformat(),
                "time": c.duration_ms,
                "request": {
                    "method": c.method,
                    "url": c.full_url,
                    "httpVersion": "HTTP/1.1",
                    "headers": [{"name": k, "value": v} for k, v in c.request_headers.items()],
                    "queryString": [{"name": k, "value": v} for k, v in c.query.items()],
                    "cookies": [],
                    "headersSize": -1,
                    "bodySize": len(body or ""),
                    **(
                        {"postData": {"mimeType": c.request_headers.get("content-type", ""), "text": body}}
                        if body is not None
                        else {}
                    ),
                },
                "response": {
                    "status": c.status,
                    "statusText": "",
                    "httpVersion": "HTTP/1.1",
                    "headers": [{"name": k, "value": v} for k, v in c.response_headers.items()],
                    "cookies": [],
                    "content": {
                        "size": c.size,
                        "mimeType": c.mime,
                        **({"text": res} if res is not None else {}),
                    },
                    "redirectURL": c.response_headers.get("location", ""),
                    "headersSize": -1,
                    "bodySize": c.size,
                },
                "cache": {},
                "timings": {"send": 0, "wait": c.duration_ms, "receive": 0},
                "_resourceType": c.resource_type,
            }
        )
    return {"log": {"version": "1.2", "creator": {"name": name, "version": "1"}, "entries": entries}}
