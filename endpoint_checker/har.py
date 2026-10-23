"""Browser HAR files (DevTools → Network → "Save all as HAR with content").

Doing something in Command with DevTools open and saving a HAR is the easiest
way to capture a new endpoint: method, payload and response included. The
same file can be judged against the catalog with `analyze`.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from endpoint_checker.catalog import Catalog
from endpoint_checker.http import Exchange, redact
from endpoint_checker.matching import Matcher

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_LONG_ID = re.compile(r"^(?=.*\d)[0-9a-zA-Z_-]{20,}$")
_VERSION = re.compile(r"^v\d+$")
_SKIP_METHODS = {"OPTIONS", "HEAD"}


@dataclass
class Candidate:
    api: str
    host: str | None
    method: str
    path: str  # templated: ids replaced by {{name}}
    query: dict
    body: Any
    status: int
    response: Any
    known: list[str]
    url: str
    params: dict = field(default_factory=dict)  # {{name}} -> example value seen

    @property
    def where(self) -> str:
        return f"{self.host} · {self.path}" if self.host else self.path


def _body(text: str | None, encoding: str | None = None) -> Any:
    if not text:
        return None
    if encoding == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _header(request: dict, name: str) -> str | None:
    for header in request.get("headers", []):
        if header.get("name", "").lower() == name:
            return header.get("value")
    return None


def _entries(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [e for e in data.get("log", {}).get("entries", []) if e.get("request", {}).get("method") not in _SKIP_METHODS]


def identity_values(entries: list[dict]) -> dict[str, str]:
    """Org/user ids seen in Command's request headers → variable names."""
    found: dict[str, str] = {}
    for entry in entries:
        request = entry.get("request", {})
        for header, var in (("x-verkada-organization-id", "org_id"), ("x-verkada-user-id", "user_id")):
            value = _header(request, header)
            if value:
                found[value] = var
    return found


def templatize(value: Any, known: dict[str, str]) -> Any:
    """Replace known identity values (org/user id, email) with {{variables}}."""
    if isinstance(value, dict):
        return {k: templatize(v, known) for k, v in value.items()}
    if isinstance(value, list):
        return [templatize(v, known) for v in value]
    if isinstance(value, str):
        if value in known:
            return "{{" + known[value] + "}}"
        for real, var in known.items():
            if len(real) >= 6 and real in value:
                value = value.replace(real, "{{" + var + "}}")
    return value


def template_path(path: str, known: dict[str, str]) -> tuple[str, dict]:
    """Identity values → {{org_id}} etc.; other ids → {{<previous segment>_id}}."""
    segments = path.split("/")
    params: dict = {}
    out = []
    for i, segment in enumerate(segments):
        if segment in known:
            out.append("{{" + known[segment] + "}}")
        elif _UUID.match(segment) or _LONG_ID.match(segment):
            prev = segments[i - 1] if i else "object"
            name = re.sub(r"[^a-z0-9]+", "_", prev.lower()).strip("_").rstrip("s") or "object"
            name = f"{name}_id"
            while name in params:
                name += "_2"
            params[name] = segment
            out.append("{{" + name + "}}")
        else:
            out.append(segment)
    return "/".join(out), params


def suggest_id(method: str, path: str) -> str:
    """A starting point for the endpoint id, e.g. POST door/{{door_id}}/unlock → door.unlock."""
    segments = [
        s
        for s in path.split("/")
        if s
        and "{{" not in s
        and not _VERSION.match(s)
        and s not in ("user", "users", "org", "orgs", "organization", "organizations", "web", "api", "__v")
    ]
    action = {"GET": "list", "POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}.get(
        method.upper(), "call"
    )
    if not segments:
        return f"custom.{action}"
    area, verb = (segments[0], action) if len(segments) == 1 else (segments[-2], segments[-1])

    def clean(s: str) -> str:
        return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_") or "x"

    return f"{clean(area)}.{clean(verb)}"


def candidates(catalog: Catalog, path: str, *, extra_identity: dict[str, str] | None = None) -> list[Candidate]:
    """Calls in the HAR that belong to one of the catalog's APIs (deduplicated)."""
    entries = _entries(path)
    known = {**identity_values(entries), **(extra_identity or {})}
    matcher = Matcher(catalog)
    seen: dict[tuple, Candidate] = {}
    for entry in entries:
        request, response = entry.get("request", {}), entry.get("response", {})
        url = request.get("url", "")
        locations = matcher.locate(url)
        if not locations:
            continue
        loc = locations[0]
        templated, params = template_path(loc.path, known)
        content = response.get("content") or {}
        candidate = Candidate(
            api=loc.api,
            host=loc.host,
            method=request.get("method", "GET").upper(),
            path=templated,
            query=templatize(dict(parse_qsl(urlsplit(url).query)), known),
            body=templatize(_body((request.get("postData") or {}).get("text")), known),
            status=int(response.get("status") or 0),
            response=_body(content.get("text"), content.get("encoding")),
            known=matcher.match(request.get("method", "GET"), url),
            url=url.split("?", 1)[0],
            params=params,
        )
        seen[(candidate.api, candidate.host, candidate.method, candidate.path)] = candidate
    return list(seen.values())


def exchanges(path: str) -> list[Exchange]:
    """HAR entries as exchanges for `analyze` (endpoints matched by URL later)."""
    out = []
    for entry in _entries(path):
        request, response = entry.get("request", {}), entry.get("response", {})
        content = response.get("content") or {}
        body = _body(content.get("text"), content.get("encoding"))
        out.append(
            Exchange(
                None,
                request.get("method", "GET").upper(),
                request.get("url", "").split("?", 1)[0],
                int(response.get("status") or 0),
                request_body=redact(_body((request.get("postData") or {}).get("text"))),
                response_json=redact(body),
                response_text="" if body is not None else (content.get("text") or "")[:2000],
                ts=entry.get("startedDateTime", ""),
                raw_json=body,
            )
        )
    return out
