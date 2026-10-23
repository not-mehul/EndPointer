"""Send catalog endpoints and record every exchange."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from endpoint_checker import templating
from endpoint_checker.catalog import Catalog, Endpoint

TIMEOUT = 30
RETRY_STATUSES = {429, 500, 502, 503, 504}
_SECRET_KEYS = {
    "password",
    "otp",
    "apikey",
    "token",
    "csrftoken",
    "usertoken",
    "xapikey",
    "xverkadaauth",
    "xverkadatoken",
    "authorization",
    "cookie",
}


class Skip(Exception):
    """The call can't be made in this run (missing prerequisite)."""


def _norm(key: str) -> str:
    return key.lower().replace("-", "").replace("_", "")


def redact(value: Any) -> Any:
    """Copy of `value` with password/token/key fields replaced by "***"."""
    if isinstance(value, dict):
        return {k: ("***" if _norm(str(k)) in _SECRET_KEYS and v not in (None, "") else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


@dataclass
class Exchange:
    endpoint_id: str | None
    method: str
    url: str
    status: int  # 0 = no response
    request_body: Any = None
    response_json: Any = None
    response_text: str = ""
    elapsed_ms: int = 0
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    error: str = ""
    raw_json: Any = field(default=None, repr=False, compare=False)  # unredacted, never written

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def message(self) -> str:
        body = self.response_json
        if isinstance(body, dict):
            for key in ("message", "error", "detail", "id"):
                if body.get(key):
                    return str(body[key])[:200]
        return (self.error or self.response_text or "").strip()[:200]

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint_id,
            "method": self.method,
            "url": self.url,
            "status": self.status,
            "request": self.request_body,
            "response": self.response_json,
            "text": self.response_text[:2000] if self.response_json is None else "",
            "elapsed_ms": self.elapsed_ms,
            "ts": self.ts,
            "error": self.error,
        }


class Transport:
    """Turns an Endpoint + variables into a request; keeps the session's cookies."""

    def __init__(self, catalog: Catalog, *, session: requests.Session | None = None, stamp: str | None = None):
        self.catalog = catalog
        self.session = session or requests.Session()
        self.stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
        self.vars: dict[str, Any] = {}  # session-wide: login results, setup results
        self.exchanges: list[Exchange] = []
        self.before_send: Callable[[Endpoint], None] = lambda endpoint: None
        self.sleep = time.sleep

    def scope(self, extra: dict | None = None) -> dict:
        return {**self.catalog.defaults, **self.vars, **(extra or {})}

    def build(self, endpoint: Endpoint, variables: dict | None = None) -> dict:
        """The request for `endpoint` (raises templating.Unresolved)."""
        api = self.catalog.apis[endpoint.api]
        scope = self.scope(variables)
        if endpoint.host:
            scope["host"] = endpoint.host
        base = templating.render(api["base_url"], scope, stamp=self.stamp)
        path = templating.render(endpoint.path, scope, stamp=self.stamp)
        url = base.rstrip("/") + "/" + str(path).lstrip("/")
        headers: dict = {}
        cookies: dict = {}
        if endpoint.auth != "none":
            headers.update(templating.render(api.get("headers") or {}, scope, stamp=self.stamp))
            cookies.update(templating.render(api.get("cookies") or {}, scope, stamp=self.stamp))
        headers.update(templating.render(endpoint.headers, scope, stamp=self.stamp))
        query = templating.render(endpoint.query, scope, stamp=self.stamp)
        body = templating.render(endpoint.body, scope, stamp=self.stamp) if endpoint.body is not None else None
        return {
            "method": endpoint.method,
            "url": url,
            "params": {k: v for k, v in query.items() if v is not None} or None,
            "headers": {k: str(v) for k, v in headers.items()},
            "cookies": {k: str(v) for k, v in cookies.items()} or None,
            "json": body,
        }

    def send(
        self,
        endpoint: Endpoint,
        variables: dict | None = None,
        *,
        body_update: dict | None = None,
        retries: int | None = None,
    ) -> Exchange:
        """Send one call. Reads are retried on 429/5xx; writes are sent once."""
        self.before_send(endpoint)
        request = self.build(endpoint, variables)
        if body_update and isinstance(request["json"], dict):
            request["json"] = {**request["json"], **body_update}
        attempts = 1 + (retries if retries is not None else (2 if endpoint.is_read else 0))
        exchange = None
        for attempt in range(attempts):
            exchange, response = self._once(endpoint, request)
            retry = exchange.status == 0 or exchange.status in RETRY_STATUSES
            if not retry or attempt == attempts - 1:
                break
            self.sleep(_delay(response, attempt))
        return exchange

    def _once(self, endpoint: Endpoint, request: dict) -> tuple[Exchange, requests.Response | None]:
        started = time.monotonic()
        exchange = Exchange(
            endpoint.id,
            request["method"],
            request["url"],
            0,
            request_body=redact(request["json"]),
        )
        response = None
        try:
            response = self.session.request(timeout=TIMEOUT, **request)
        except requests.RequestException as e:
            exchange.error = f"{type(e).__name__}: {e}"
        else:
            exchange.status = response.status_code
            if response.content:
                try:
                    exchange.raw_json = response.json()
                except ValueError:
                    exchange.response_text = response.text[:4000]
            exchange.response_json = redact(exchange.raw_json)
            if response.url and response.url != request["url"]:
                exchange.url = response.url.split("?")[0]
        exchange.elapsed_ms = int((time.monotonic() - started) * 1000)
        self.exchanges.append(exchange)
        return exchange, response


def _delay(response, attempt: int) -> float:
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            return min(float(header), 10.0)
        except ValueError:
            pass
    return 1.0 * 2**attempt
