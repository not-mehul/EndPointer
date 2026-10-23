"""End to end: a real Chrome/Chromium browses the demo site while Endpointer records it.

Skipped when no browser is installed (set ENDPOINTER_BROWSER to point at one).
"""

from __future__ import annotations

import time

import pytest

from endpointer import cdp
from endpointer.app import App
from endpointer.store import Store

from .demo_site import TOKEN, DemoSite

BROWSER = cdp.find_browser()
pytestmark = pytest.mark.skipif(not BROWSER, reason="no Chrome/Chromium found (set ENDPOINTER_BROWSER)")


@pytest.fixture
def setup(tmp_path):
    site = DemoSite()
    app = App(Store(tmp_path), browser=BROWSER, headless=True)
    yield site, app, tmp_path
    app.shutdown()
    site.close()


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_capture_graph_and_drift(setup):
    site, app, home = setup
    app.launch_browser(site.url)
    assert wait_for(lambda: any(c.path == "/api/v1/cart" for c in app.live.calls))

    overview = app.overview("live")
    paths = {e["template"] for e in overview["endpoints"]}
    assert paths == {"/api/login", "/api/v1/users", "/api/v1/users/{user_id}/orders", "/api/v1/cart"}
    hidden = {c.path for c in app.live.calls} - {c["path"] for c in overview["calls"]}
    assert {"/", "/logo.png", "/collect"} <= hidden  # page, image and beacon are noise

    login = next(c for c in app.live.calls if c.path == "/api/login")
    assert login.request_body == {
        "email": "me@example.com",
        "password": "hunter2",
    }  # post data and body both captured
    links = {
        (link["source"].split("/", 1)[1], link["target"].split("/", 1)[1], link["fields"][0]["from"])
        for link in overview["links"]
    }
    assert ("api/login", "api/v1/users", "response.access_token") in links
    assert ("api/v1/users", "api/v1/users/{user_id}/orders", "response.users[].id") in links

    app.save_endpoints("live", [e["key"] for e in overview["endpoints"]])
    site.version = 2
    report = app.check_library()
    assert {r["key"].split(" ", 1)[1].split("/", 1)[1]: r["level"] for r in report["results"]} == {
        "api/v1/users": "changed",
        "api/v1/users/{user_id}/orders": "changed",
        "api/login": "not seen",
        "api/v1/cart": "not seen",
    }
    assert all(r["result"] == 200 for r in report["replayed"])  # replays used the live session's token

    app.stop_browser()
    stored = "".join(
        p.read_text() for folder in ("captures", "library") for p in (home / folder).glob("*.json")
    )
    assert TOKEN not in stored
    assert stored.count("hunter2") == stored.count(
        "password: 'hunter2'"
    )  # only in the demo page's own source
