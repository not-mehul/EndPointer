"""The local web server behind the window: static UI files plus a small JSON API.

It listens on 127.0.0.1 only. Every API call must carry the session token in
an `X-Endpointer-Token` header (which a page on another site can't add without
a CORS pre-flight that this server never grants), and requests whose Host
header isn't this server are refused (DNS rebinding).
"""

from __future__ import annotations

import json
import secrets
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from endpointer.app import App

STATIC = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/app.css": ("app.css", "text/css"),
}
MAX_UPLOAD = 200 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _static(name: str) -> bytes:
    return resources.files("endpointer").joinpath("ui", name).read_bytes()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app: App, port: int = 0):
        self.app = app
        self.token = secrets.token_urlsafe(24)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        app.ignored_origins |= {self.origin, f"http://localhost:{self.server_port}"}

    @property
    def url(self) -> str:
        return self.origin + "/"


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        pass

    # --- plumbing ---------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = 200, extra: dict | None = None) -> None:
        self._send(
            status,
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            extra,
        )

    def _host_ok(self) -> bool:
        port = self.server.server_port
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ApiError("upload too large", 413)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as e:
            raise ApiError(f"invalid JSON: {e}") from e

    def _handle(self, method: str) -> None:
        if not self._host_ok():
            self._send(403, b"forbidden", "text/plain")
            return
        parts = urlsplit(self.path)
        path = parts.path
        if method == "GET" and path in STATIC:
            name, ctype = STATIC[path]
            body = _static(name)
            if name == "index.html":
                body = body.replace(b"__ENDPOINTER_TOKEN__", self.server.token.encode())
            self._send(200, body, f"{ctype}; charset=utf-8")
            return
        if not path.startswith("/api/"):
            self._send(404, b"not found", "text/plain")
            return
        if not secrets.compare_digest(self.headers.get("X-Endpointer-Token", ""), self.server.token):
            self._json({"error": "missing or wrong token"}, 403)
            return
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        try:
            route = ROUTES.get((method, path))
            if route is None:
                raise ApiError("no such API", 404)
            body = self._body() if method == "POST" else {}
            result = route(self.server.app, query, body)
            if isinstance(result, tuple):  # (data, filename): a download
                data, filename = result
                self._json(data, extra={"Content-Disposition": f'attachment; filename="{filename}"'})
            else:
                self._json(result if result is not None else {"ok": True})
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except KeyError as e:
            self._json({"error": f"not found: {e}"}, 404)
        except (RuntimeError, OSError, ValueError) as e:
            self._json({"error": str(e)}, 409)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


# ----------------------------------------------------------------------
# Routes: (method, path) → fn(app, query, body)
# ----------------------------------------------------------------------


def _need(data: dict, name: str) -> Any:
    if data.get(name) in (None, ""):
        raise ApiError(f"missing {name}")
    return data[name]


def status(app: App, q: dict, b: dict) -> dict:
    return {
        "version": app.version,
        "browser": app.browser_status(),
        "captures": app.captures(),
        "live": app.live.id,
        "home": str(app.store.home),
        "rules": app.store.rules.to_dict(),
    }


def library_entry(app: App, q: dict, b: dict) -> dict:
    entry = app.store.entry(_need(q, "key"))
    if entry is None:
        raise KeyError(q["key"])
    return entry


def delete_capture(app: App, q: dict, b: dict) -> None:
    capture_id = _need(b, "id")
    if capture_id in ("live", app.live.id):
        app.clear_live()
    else:
        app.store.delete_capture(capture_id)


ROUTES = {
    ("GET", "/api/status"): status,
    ("GET", "/api/overview"): lambda app, q, b: app.overview(
        q.get("capture", "live"), show_all=q.get("all") == "1"
    ),
    ("GET", "/api/call"): lambda app, q, b: app.call(q.get("capture", "live"), _need(q, "id")),
    ("GET", "/api/endpoint"): lambda app, q, b: app.endpoint(q.get("capture", "live"), _need(q, "key")),
    ("GET", "/api/library"): lambda app, q, b: app.library(),
    ("GET", "/api/library/entry"): library_entry,
    ("GET", "/api/drift"): lambda app, q, b: app.drift(q.get("capture", "live"), q.get("against", "library")),
    ("GET", "/api/export/har"): lambda app, q, b: (
        app.export_har(q.get("capture", "live")),
        f"{q.get('capture', 'live')}.har",
    ),
    ("POST", "/api/drift/record"): lambda app, q, b: app.drift(
        b.get("capture", "live"), "library", record=True
    ),
    ("POST", "/api/drift/accept"): lambda app, q, b: app.accept(b.get("capture", "live"), _need(b, "key")),
    ("POST", "/api/check"): lambda app, q, b: app.check_library(),
    ("POST", "/api/library/save"): lambda app, q, b: app.save_endpoints(
        b.get("capture", "live"), list(_need(b, "keys"))
    ),
    ("POST", "/api/library/update"): lambda app, q, b: app.update_endpoint(
        _need(b, "key"), name=b.get("name"), notes=b.get("notes"), tags=b.get("tags")
    ),
    ("POST", "/api/library/remove"): lambda app, q, b: app.remove_endpoint(_need(b, "key")),
    ("POST", "/api/browser/launch"): lambda app, q, b: app.launch_browser(b.get("url", "")),
    ("POST", "/api/browser/attach"): lambda app, q, b: app.attach_browser(
        int(_need(b, "port")), b.get("host") or "127.0.0.1"
    ),
    ("POST", "/api/browser/stop"): lambda app, q, b: app.stop_browser(),
    ("POST", "/api/recording"): lambda app, q, b: app.set_recording(bool(b.get("on"))),
    ("POST", "/api/capture/new"): lambda app, q, b: app.new_live(),
    ("POST", "/api/capture/clear"): lambda app, q, b: app.clear_live(),
    ("POST", "/api/capture/save"): lambda app, q, b: app.save_live(b.get("name") or None),
    ("POST", "/api/capture/rename"): lambda app, q, b: (
        app.save_live(_need(b, "name"))
        if b.get("id") in ("live", app.live.id)
        else app.store.rename_capture(_need(b, "id"), _need(b, "name"))
    ),
    ("POST", "/api/capture/delete"): delete_capture,
    ("POST", "/api/import/har"): lambda app, q, b: app.import_har(
        _need(b, "har"), b.get("name") or "Imported HAR"
    ),
    ("POST", "/api/rules"): lambda app, q, b: app.set_rule(
        _need(b, "kind"), _need(b, "value"), bool(b.get("on", True))
    ),
}
