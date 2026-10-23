"""Captured calls and how they group into endpoints.

A `Call` is one request/response seen in the browser (or read from a HAR).
Calls with the same method, host and *path template* belong to the same
endpoint: ids in the path are replaced by `{name}` placeholders, so
`GET /api/users/42` and `GET /api/users/97` are both `GET /api/users/{user_id}`.
GraphQL calls are told apart by their operation name.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

MAX_BODY = 512 * 1024  # bodies larger than this keep their first bytes as text only

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX = re.compile(r"^[0-9a-f]{16,}$", re.I)
_MIXED = re.compile(r"^(?=.*\d)(?=.*[a-zA-Z])[0-9a-zA-Z_\-]{16,}$")
_DIGITS = re.compile(r"^\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def is_id_segment(segment: str) -> bool:
    return bool(
        _DIGITS.match(segment)
        or _UUID.match(segment)
        or _HEX.match(segment)
        or _MIXED.match(segment)
        or _DATE.match(segment)
        or ("@" in segment and "." in segment)
    )


def _singular(word: str) -> str:
    word = re.sub(r"[^a-z0-9]+", "_", word.lower()).strip("_")
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def template_path(path: str) -> tuple[str, dict[str, str]]:
    """`/users/42/posts/9` → (`/users/{user_id}/posts/{post_id}`, {"user_id": "42", "post_id": "9"})."""
    segments = path.split("/")
    out: list[str] = []
    params: dict[str, str] = {}
    for i, segment in enumerate(segments):
        if segment and is_id_segment(segment):
            prev = next((s for s in reversed(segments[:i]) if s and not is_id_segment(s)), "")
            name = (_singular(prev) or "id") + "_id" if prev else "id"
            base, n = name, 2
            while name in params:
                name, n = f"{base}_{n}", n + 1
            params[name] = segment
            out.append("{" + name + "}")
        else:
            out.append(segment)
    return "/".join(out) or "/", params


# ----------------------------------------------------------------------
# Redaction: secrets are replaced by a keyed fingerprint, so a stored token
# can still be followed from the response that issued it to the requests
# that use it, without the token itself ever being written to disk.
# ----------------------------------------------------------------------

_SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "x-csrf-token",
    "x-xsrf-token",
}
_SECRET_KEYS = {
    "password",
    "passwd",
    "pwd",
    "otp",
    "apikey",
    "secret",
    "clientsecret",
    "csrf",
    "xsrf",
    "sessionid",
}
_SECRET_SUFFIXES = ("token", "secret", "password", "apikey", "passcode")
_SCHEME = re.compile(r"^(Bearer|Basic|Token|Digest|Bot)\s+(.+)$", re.I)
REDACTED = re.compile(r"^(?:\w+ )?<secret:[0-9a-f]{10}>$")


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def is_secret_key(key: str) -> bool:
    n = _norm(key)
    return n in _SECRET_KEYS or n.endswith(_SECRET_SUFFIXES)


def is_secret_header(name: str) -> bool:
    n = name.lower()
    return n in _SECRET_HEADERS or (n.startswith("x-") and (n.endswith("-token") or n.endswith("-auth")))


class Redactor:
    def __init__(self, salt: bytes):
        self.salt = salt

    def fingerprint(self, value: str) -> str:
        return "<secret:" + hmac.new(self.salt, value.encode(), hashlib.sha256).hexdigest()[:10] + ">"

    def secret(self, value: Any) -> Any:
        if value in (None, "") or (isinstance(value, str) and REDACTED.match(value)):
            return value
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True)
        return self.fingerprint(str(value))

    def header(self, name: str, value: str) -> str:
        if not is_secret_header(name) or not value or REDACTED.match(value):
            return value
        m = _SCHEME.match(value)
        if m:
            return f"{m.group(1)} {self.fingerprint(m.group(2).strip())}"
        return self.fingerprint(value)

    def headers(self, headers: dict[str, str]) -> dict[str, str]:
        return {k: self.header(k, v) for k, v in headers.items()}

    def body(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: (self.secret(v) if is_secret_key(str(k)) else self.body(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.body(v) for v in value]
        return value

    def query(self, query: dict[str, str]) -> dict[str, str]:
        return {k: (self.secret(v) if is_secret_key(k) else v) for k, v in query.items()}


# ----------------------------------------------------------------------
# Calls
# ----------------------------------------------------------------------


def parse_body(text: str | None, mime: str = "", *, base64_encoded: bool = False) -> tuple[Any, str]:
    """(parsed value, kind) for a body: kind is json | form | text | binary | ''."""
    if text is None or text == "":
        return None, ""
    if base64_encoded:
        try:
            text = base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None, "binary"
    stripped = text.lstrip()
    if stripped[:1] in ("{", "[") or "json" in mime:
        try:
            return json.loads(text), "json"
        except ValueError:
            pass
    if "x-www-form-urlencoded" in mime:
        return dict(parse_qsl(text, keep_blank_values=True)), "form"
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY]
    return text, "text"


@dataclass
class Call:
    id: str
    ts: float  # seconds since the epoch
    method: str
    url: str  # without the query string
    status: int = 0  # 0: no response (failed or still pending)
    query: dict[str, str] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    request_kind: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: Any = None
    response_kind: str = ""
    mime: str = ""
    resource_type: str = ""  # xhr | fetch | document | script | … (lower case, as DevTools names them)
    page: str = ""  # URL of the page that made the call
    duration_ms: int = 0
    size: int = 0
    error: str = ""
    graphql_op: str = ""

    # --- derived ----------------------------------------------------------

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc

    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"

    @property
    def template(self) -> str:
        return template_path(self.path)[0]

    @property
    def params(self) -> dict[str, str]:
        return template_path(self.path)[1]

    @property
    def key(self) -> str:
        key = f"{self.method} {self.host}{self.template}"
        return f"{key}#{self.graphql_op}" if self.graphql_op else key

    @property
    def full_url(self) -> str:
        if not self.query:
            return self.url
        from urllib.parse import urlencode

        return self.url + "?" + urlencode(self.query)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Call:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def summary(self) -> dict:
        """What the live list needs (no bodies)."""
        return {
            "id": self.id,
            "ts": self.ts,
            "method": self.method,
            "url": self.url,
            "host": self.host,
            "path": self.path,
            "template": self.template,
            "key": self.key,
            "status": self.status,
            "mime": self.mime,
            "type": self.resource_type,
            "duration_ms": self.duration_ms,
            "size": self.size,
            "error": self.error,
            "graphql_op": self.graphql_op,
        }

    def redacted(self, redactor: Redactor) -> Call:
        data = self.to_dict()
        data["request_headers"] = redactor.headers(self.request_headers)
        data["response_headers"] = redactor.headers(self.response_headers)
        data["query"] = redactor.query(self.query)
        data["request_body"] = redactor.body(self.request_body)
        data["response_body"] = redactor.body(self.response_body)
        return Call.from_dict(data)


def graphql_operation(body: Any, query: dict[str, str]) -> str:
    if isinstance(body, dict):
        op = body.get("operationName")
        if isinstance(op, str) and op:
            return op
        text = body.get("query")
        if isinstance(text, str):
            m = re.search(r"\b(?:query|mutation|subscription)\s+(\w+)", text)
            if m:
                return m.group(1)
    return query.get("operationName", "")


def make_call(
    *,
    id: str,
    ts: float,
    method: str,
    url: str,
    request_headers: dict[str, str] | None = None,
    request_text: str | None = None,
    status: int = 0,
    response_headers: dict[str, str] | None = None,
    response_text: str | None = None,
    response_base64: bool = False,
    mime: str = "",
    resource_type: str = "",
    page: str = "",
    duration_ms: int = 0,
    size: int = 0,
    error: str = "",
) -> Call:
    """Build a Call from raw request/response text (DevTools events and HAR entries both end up here)."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    req_headers = {k.lower(): v for k, v in (request_headers or {}).items()}
    res_headers = {k.lower(): v for k, v in (response_headers or {}).items()}
    request_body, request_kind = parse_body(request_text, req_headers.get("content-type", ""))
    response_body, response_kind = parse_body(
        response_text, mime or res_headers.get("content-type", ""), base64_encoded=response_base64
    )
    if response_kind == "text" and isinstance(response_body, str) and not _texty(mime):
        response_body, response_kind = None, "binary"
    method = method.upper()
    return Call(
        id=id,
        ts=ts,
        method=method,
        url=f"{parts.scheme}://{parts.netloc}{parts.path}",
        status=status,
        query=query,
        request_headers=req_headers,
        request_body=request_body,
        request_kind=request_kind,
        response_headers=res_headers,
        response_body=response_body,
        response_kind=response_kind,
        mime=mime.split(";")[0].strip().lower(),
        resource_type=resource_type.lower(),
        page=page,
        duration_ms=duration_ms,
        size=size or len(response_text or ""),
        error=error,
        graphql_op=graphql_operation(request_body, query) if "graphql" in url.lower() else "",
    )


def _texty(mime: str) -> bool:
    mime = mime.lower()
    return (
        not mime or mime.startswith("text/") or any(t in mime for t in ("json", "xml", "javascript", "form"))
    )
