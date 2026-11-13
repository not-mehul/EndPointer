"""Capture network traffic from Chrome, Edge, Brave or Chromium over the DevTools protocol.

`launch()` starts the browser with its own profile (so it doesn't touch your
everyday one, and your logins in it persist between runs) and remote debugging
on a free local port. `Recorder` connects to the browser, attaches to every
tab (and the frames and workers inside it) and turns DevTools network events
into `Call`s, response bodies included. No proxy and no certificate to install.

You can also attach to a browser you started yourself with
`--remote-debugging-port=9222 --user-data-dir=<some dir>`.
"""

from __future__ import annotations

import itertools
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from endpointer.classify import STATIC_TYPES
from endpointer.model import Call, make_call
from endpointer.websocket import WebSocket, WebSocketClosed
import contextlib

MAX_POST_DATA = 256 * 1024
BODY_TIMEOUT = 10


def browser_candidates() -> list[str]:
    system = platform.system()
    if system == "Darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        ]
    if system == "Windows":
        roots = [os.environ.get(v, "") for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rel = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
            r"Chromium\Application\chrome.exe",
        ]
        return [str(Path(root) / r) for root in roots if root for r in rel]
    names = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "brave-browser",
    ]
    return [p for p in (shutil.which(n) for n in names) if p]


def find_browser(explicit: str | None = None) -> str | None:
    for candidate in [explicit, os.environ.get("ENDPOINTER_BROWSER"), *browser_candidates()]:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _wait_for_port_file(profile: Path, process: subprocess.Popen, timeout: float = 20) -> int:
    path = profile / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the browser exited (code {process.returncode}) before it was ready")
        try:
            first = path.read_text().splitlines()[0]
            return int(first)
        except (FileNotFoundError, IndexError, ValueError):
            time.sleep(0.1)
    raise RuntimeError("the browser didn't open its DevTools port in time")


def launch(
    browser: str,
    profile: Path,
    *,
    url: str = "about:blank",
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> tuple[subprocess.Popen, int]:
    """Start the browser; returns (process, DevTools port)."""
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "DevToolsActivePort").unlink(missing_ok=True)
    args = [
        browser,
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=ChromeWhatsNewUI",
        *(["--headless=new"] if headless else []),
        *(
            ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
        ),  # Chrome refuses to run as root otherwise
        *(extra_args or []),
        url,
    ]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process, _wait_for_port_file(profile, process)


def browser_ws_url(port: int, host: str = "127.0.0.1", timeout: float = 5) -> str:
    with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=timeout) as r:
        return json.loads(r.read())["webSocketDebuggerUrl"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CDPError(Exception):
    pass


class Recorder:
    """Attaches to every tab of a browser and reports each finished request as a Call."""

    def __init__(
        self, ws_url: str, on_call: Callable[[Call], None], *, ignore: Callable[[str], bool] | None = None
    ):
        self.ws = WebSocket(ws_url)
        self.on_call = on_call
        self.ignore = ignore or (lambda url: False)
        self.alive = True
        self.error = ""
        self.recording = True
        self._ids = itertools.count(1)
        self._calls = itertools.count(1)
        self._pending: dict[int, tuple[threading.Event | None, Callable | None, list]] = {}
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str], dict] = {}
        self._extra: dict[tuple[str, str], dict] = {}
        self.sessions: dict[str, dict] = {}  # sessionId → targetInfo
        self._attaching: set[str] = set()
        self._ready = threading.Event()
        self._prefix = time.strftime("%H%M%S")
        self._thread = threading.Thread(target=self._loop, name="cdp-reader", daemon=True)
        self._thread.start()
        self.command("Target.setDiscoverTargets", {"discover": True})
        for info in self.command("Target.getTargets").get("targetInfos", []):
            self._maybe_attach(info)

    # --- protocol ---------------------------------------------------------

    def _send(self, method: str, params: dict | None, session: str | None, event, callback) -> int:
        msg_id = next(self._ids)
        message: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        with self._lock:
            self._pending[msg_id] = (event, callback, [])
        try:
            self.ws.send(json.dumps(message))
        except OSError as e:
            with self._lock:
                self._pending.pop(msg_id, None)
            raise CDPError(str(e)) from e
        return msg_id

    def command(
        self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 15
    ) -> dict:
        """Send a command and wait for its result (don't call from the reader thread)."""
        event = threading.Event()
        msg_id = self._send(method, params, session, event, None)
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(msg_id, None)
            raise CDPError(f"{method}: no answer")
        with self._lock:
            _, _, box = self._pending.pop(msg_id)
        result = box[0]
        if "error" in result:
            raise CDPError(f"{method}: {result['error'].get('message')}")
        return result.get("result", {})

    def command_async(
        self, method: str, params: dict | None = None, session: str | None = None, callback=None
    ) -> None:
        with contextlib.suppress(CDPError):
            self._send(method, params, session, None, callback or (lambda result: None))

    def _loop(self) -> None:
        try:
            while True:
                message = json.loads(self.ws.recv())
                if "id" in message:
                    with self._lock:
                        entry = self._pending.get(message["id"])
                        if entry and entry[1] is not None:
                            self._pending.pop(message["id"], None)
                    if not entry:
                        continue
                    event, callback, box = entry
                    if callback is not None:
                        try:
                            callback(message)
                        except Exception as e:  # a bad body must not stop the recorder
                            self.error = f"{type(e).__name__}: {e}"
                    else:
                        box.append(message)
                        event.set()
                else:
                    try:
                        self._event(
                            message.get("method", ""), message.get("params", {}), message.get("sessionId")
                        )
                    except Exception as e:
                        self.error = f"{type(e).__name__}: {e}"
        except (WebSocketClosed, OSError) as e:
            self.error = self.error or str(e)
        finally:
            self.alive = False
            with self._lock:
                for event, _, box in self._pending.values():
                    if event:
                        box.append({"error": {"message": "browser disconnected"}})
                        event.set()
                self._pending.clear()

    def close(self) -> None:
        self.ws.close()

    # --- targets ----------------------------------------------------------

    def _maybe_attach(self, info: dict) -> None:
        # Only tabs: their frames and workers are attached through each tab's auto-attach. Service
        # workers are left out on purpose, since a request they serve would otherwise show up twice.
        if info.get("type") != "page":
            return
        if info.get("attached") or info["targetId"] in self._attaching:
            return
        if info.get("url", "").startswith(("devtools://", "chrome-extension://")):
            return
        self._attaching.add(info["targetId"])
        self.command_async("Target.attachToTarget", {"targetId": info["targetId"], "flatten": True})

    def _setup_session(self, session: str, info: dict) -> None:
        self.sessions[session] = info
        page = info.get("type") == "page"

        def enabled(message: dict) -> None:
            if page:
                self._ready.set()

        self.command_async("Network.enable", {"maxPostDataSize": MAX_POST_DATA}, session, enabled)
        if info.get("type") in ("page", "iframe"):
            self.command_async(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                session,
            )
        self.command_async("Runtime.runIfWaitingForDebugger", None, session)

    def wait_ready(self, timeout: float = 5) -> bool:
        """True once at least one tab is being recorded."""
        return self._ready.wait(timeout)

    def navigate(self, url: str) -> None:
        """Show `url` in the blank tab the browser started with, or in a new tab."""
        blank = [
            p
            for p in self.pages()
            if p.get("url") in ("about:blank", "", "chrome://newtab/", "chrome://new-tab-page/")
        ]
        if blank:
            self.command("Page.navigate", {"url": url}, blank[0]["session"])
        else:
            self.open(url)

    def pages(self) -> list[dict]:
        return [
            {"session": s, **info} for s, info in list(self.sessions.items()) if info.get("type") == "page"
        ]

    def open(self, url: str) -> None:
        self.command("Target.createTarget", {"url": url})

    def evaluate(self, expression: str, *, prefer_origin: str = "", timeout: float = 30) -> Any:
        """Run JavaScript in a tab (the one showing `prefer_origin`, if any) and return its value."""
        pages = [p for p in self.pages() if p.get("url", "").startswith("http")] or self.pages()
        if not pages:
            raise CDPError("no open tab to run in")
        pages.sort(key=lambda p: not p.get("url", "").startswith(prefer_origin) if prefer_origin else False)
        result = self.command(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            pages[0]["session"],
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise CDPError(result["exceptionDetails"].get("text") or "script failed")
        return result.get("result", {}).get("value")

    # --- events -----------------------------------------------------------

    def _event(self, method: str, params: dict, session: str | None) -> None:
        if method == "Target.targetCreated":
            self._maybe_attach(params["targetInfo"])
        elif method == "Target.attachedToTarget":
            self._setup_session(params["sessionId"], params["targetInfo"])
        elif method == "Target.targetInfoChanged":
            info = params["targetInfo"]
            for s, known in list(self.sessions.items()):
                if known.get("targetId") == info["targetId"]:
                    self.sessions[s] = info
        elif method == "Target.detachedFromTarget":
            self.sessions.pop(params.get("sessionId", ""), None)
        elif method.startswith("Network.") and session:
            self._network(method[8:], params, session)

    def _network(self, name: str, p: dict, session: str) -> None:
        key = (session, p.get("requestId", ""))
        if name == "requestWillBeSent":
            if "redirectResponse" in p and key in self._requests:
                previous = self._requests.pop(key)
                previous["response"] = p["redirectResponse"]
                previous["end"] = p.get("timestamp")
                self._finish(key, previous, None)
            request = p["request"]
            url = request.get("url", "")
            if not url.startswith(("http://", "https://")) or self.ignore(url) or not self.recording:
                return
            self._requests[key] = {
                "request": request,
                "type": p.get("type", ""),
                "wall": p.get("wallTime") or time.time(),
                "start": p.get("timestamp"),
                "page": p.get("documentURL", ""),
            }
        elif key not in self._requests:
            if name in ("requestWillBeSentExtraInfo", "responseReceivedExtraInfo"):
                self._extra.setdefault(key, {})[name] = p.get("headers", {})
                if len(self._extra) > 2000:
                    self._extra.pop(next(iter(self._extra)))
            return
        elif name in ("requestWillBeSentExtraInfo", "responseReceivedExtraInfo"):
            self._extra.setdefault(key, {})[name] = p.get("headers", {})
        elif name == "responseReceived":
            self._requests[key]["response"] = p.get("response", {})
            self._requests[key]["type"] = p.get("type") or self._requests[key]["type"]
        elif name == "loadingFailed":
            entry = self._requests.pop(key)
            entry["error"] = p.get("errorText") or ("blocked" if p.get("blockedReason") else "failed")
            if p.get("canceled"):
                entry["error"] = "canceled"
            entry["end"] = p.get("timestamp")
            self._finish(key, entry, None)
        elif name == "loadingFinished":
            entry = self._requests.pop(key)
            entry["end"] = p.get("timestamp")
            entry["size"] = int(p.get("encodedDataLength") or 0)
            self._collect(key, entry, session)

    def _collect(self, key: tuple[str, str], entry: dict, session: str) -> None:
        """Fetch the request's post data (if DevTools held it back) and the response body, then finish."""
        request_id = key[1]
        rtype = (entry.get("type") or "").lower()
        wants_body = rtype not in STATIC_TYPES and entry.get("response", {}).get("status", 0) not in (
            204,
            304,
        )
        state = {"body": None, "b64": False, "waiting": 0}

        def done() -> None:
            state["waiting"] -= 1
            if state["waiting"] <= 0:
                self._finish(key, entry, state)

        if entry["request"].get("hasPostData") and "postData" not in entry["request"]:
            state["waiting"] += 1

            def got_post(message: dict) -> None:
                entry["request"]["postData"] = message.get("result", {}).get("postData")
                done()

            self.command_async("Network.getRequestPostData", {"requestId": request_id}, session, got_post)
        if wants_body:
            state["waiting"] += 1

            def got_body(message: dict) -> None:
                result = message.get("result") or {}
                state["body"], state["b64"] = result.get("body"), bool(result.get("base64Encoded"))
                done()

            self.command_async("Network.getResponseBody", {"requestId": request_id}, session, got_body)
        if state["waiting"] == 0:
            self._finish(key, entry, state)

    def _finish(self, key: tuple[str, str], entry: dict, body: dict | None) -> None:
        request, response = entry["request"], entry.get("response") or {}
        extra = self._extra.pop(key, {})
        req_headers = {**request.get("headers", {}), **extra.get("requestWillBeSentExtraInfo", {})}
        res_headers = {**response.get("headers", {}), **extra.get("responseReceivedExtraInfo", {})}
        start, end = entry.get("start"), entry.get("end")
        call = make_call(
            id=f"{self._prefix}-{next(self._calls)}",
            ts=entry["wall"],
            method=request.get("method", "GET"),
            url=request.get("url", ""),
            request_headers=req_headers,
            request_text=request.get("postData"),
            status=int(response.get("status") or 0),
            response_headers=res_headers,
            response_text=(body or {}).get("body"),
            response_base64=bool((body or {}).get("b64")),
            mime=response.get("mimeType", ""),
            resource_type=entry.get("type", ""),
            page=entry.get("page", ""),
            duration_ms=int((end - start) * 1000) if start and end else 0,
            size=entry.get("size", 0),
            error=entry.get("error", ""),
        )
        self.on_call(call)
