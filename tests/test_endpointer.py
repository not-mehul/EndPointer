"""Endpointer: grouping, relevance, dependencies, drift, storage, HAR and the local server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from endpointer import classify, deps, drift, har, shapes
from endpointer.app import App, analyse
from endpointer.model import Call, Redactor, make_call, template_path
from endpointer.store import Store

TOKEN = "tok_9f8e7d6c5b4a39281706"


def call(
    id,
    method,
    url,
    *,
    ts,
    status=200,
    body=None,
    response=None,
    headers=None,
    rtype="fetch",
    page="https://app.example.com/",
):
    return make_call(
        id=id,
        ts=ts,
        method=method,
        url=url,
        request_headers={"content-type": "application/json", **(headers or {})},
        request_text=json.dumps(body) if body is not None else None,
        status=status,
        response_headers={"content-type": "application/json"},
        response_text=json.dumps(response) if response is not None else None,
        mime="application/json" if response is not None else "",
        resource_type=rtype,
        page=page,
    )


def session() -> list[Call]:
    auth = {"Authorization": f"Bearer {TOKEN}"}
    return [
        call(
            "1",
            "POST",
            "https://api.example.com/auth/login",
            ts=1,
            body={"email": "me@example.com", "password": "hunter2"},
            response={"access_token": TOKEN},
        ),
        call(
            "2",
            "GET",
            "https://api.example.com/v1/users?limit=20",
            ts=2,
            headers=auth,
            response={"users": [{"id": 48213, "name": "Ada"}]},
        ),
        call(
            "3",
            "GET",
            "https://api.example.com/v1/users/48213/orders",
            ts=3,
            headers=auth,
            response={"orders": [{"id": 7, "total": 12.5}]},
        ),
        call("4", "GET", "https://api.example.com/v1/orders/7", ts=4, headers=auth, response={"id": 7}),
        call("5", "POST", "https://www.google-analytics.com/g/collect?v=2", ts=5, rtype="ping"),
        call("6", "GET", "https://app.example.com/static/app.3f9a1c.js", ts=0.5, rtype="script"),
    ]


# ----------------------------------------------------------------------
# Grouping and redaction
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "template", "params"),
    [
        ("/v1/users/48213/orders", "/v1/users/{user_id}/orders", {"user_id": "48213"}),
        (
            "/api/items/3f2b8c1e-1d2c-4e5f-8a9b-0c1d2e3f4a5b",
            "/api/items/{item_id}",
            {"item_id": "3f2b8c1e-1d2c-4e5f-8a9b-0c1d2e3f4a5b"},
        ),
        (
            "/repos/5f8d0d55b54764421b7156c3/categories/12/12",
            "/repos/{repo_id}/categories/{category_id}/{category_id_2}",
            None,
        ),
        ("/v2/search", "/v2/search", {}),
        ("/", "/", {}),
    ],
)
def test_template_path(path, template, params):
    got, got_params = template_path(path)
    assert got == template
    if params is not None:
        assert got_params == params


def test_calls_group_by_template_and_graphql_operation():
    a = call("a", "GET", "https://x.io/api/users/1", ts=1, response={})
    b = call("b", "GET", "https://x.io/api/users/2", ts=2, response={})
    assert a.key == b.key == "GET x.io/api/users/{user_id}"
    q1 = call(
        "c",
        "POST",
        "https://x.io/graphql",
        ts=3,
        body={"operationName": "GetCart", "query": "query GetCart { cart { id } }"},
        response={},
    )
    q2 = call(
        "d", "POST", "https://x.io/graphql", ts=4, body={"query": "mutation AddItem { add }"}, response={}
    )
    assert (q1.key, q2.key) == ("POST x.io/graphql#GetCart", "POST x.io/graphql#AddItem")


def test_redaction_fingerprints_secrets_consistently():
    r = Redactor(b"salt")
    login, users = session()[:2]
    red_login, red_users = login.redacted(r), users.redacted(r)
    assert red_login.request_body["password"].startswith("<secret:")
    assert red_login.request_body["email"] == "me@example.com"
    fingerprint = red_login.response_body["access_token"]
    assert red_users.request_headers["authorization"] == f"Bearer {fingerprint}"
    assert TOKEN not in json.dumps(red_users.to_dict())
    assert Redactor(b"other").fingerprint(TOKEN) != fingerprint


# ----------------------------------------------------------------------
# Relevance
# ----------------------------------------------------------------------


def test_relevance_keeps_api_calls_and_drops_noise():
    a = analyse(session(), rules=classify.Rules(), saved=set())
    important = {c.id for c in a.calls if a.relevance[c.id].important}
    assert important == {"1", "2", "3", "4"}
    assert "analytics" in a.relevance["5"].reasons[0]
    assert "static" in a.relevance["6"].reasons[0]
    assert "same site as the page" in a.relevance["2"].reasons


def test_rules_override_relevance():
    noisy = session()[4]
    assert classify.score(noisy, rules=classify.Rules(pin_hosts=["*.google-analytics.com"])).important
    api = session()[1]
    assert not classify.score(api, rules=classify.Rules(hide_keys=[api.key])).important
    assert classify.score(api, saved={api.key}).reasons == ["in your library"]


def test_site_of():
    assert classify.site_of("api.eu.example.com") == "example.com"
    assert classify.site_of("shop.example.co.uk:8443") == "example.co.uk"
    assert classify.site_of("127.0.0.1:8080") == "127.0.0.1"


# ----------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------


def test_links_follow_ids_and_tokens():
    links = {
        (link.source.split(" ", 1)[1], link.target.split(" ", 1)[1]): link for link in deps.links(session())
    }
    login, users, orders = (
        "api.example.com/auth/login",
        "api.example.com/v1/users",
        "api.example.com/v1/users/{user_id}/orders",
    )
    assert ("response.access_token", "header.authorization") in links[(login, users)].fields
    assert ("response.users[].id", "path.{user_id}") in links[(users, orders)].fields
    # "id": 7 is too short to match anywhere, but fine for a path segment named after it.
    assert ("response.orders[].id", "path.{order_id}") in links[
        (orders, "api.example.com/v1/orders/{order_id}")
    ].fields
    assert deps.feeds(links.values())["POST " + login] == 3


def test_links_survive_redaction():
    r = Redactor(b"salt")
    redacted = [c.redacted(r) for c in session()]
    sources = {(link.source, link.target) for link in deps.links(redacted)}
    assert ("POST api.example.com/auth/login", "GET api.example.com/v1/users") in sources


def test_common_values_dont_link():
    a = call(
        "a", "GET", "https://x.io/api/settings", ts=1, response={"enabled": True, "count": 3, "lang": "en"}
    )
    b = call(
        "b",
        "POST",
        "https://x.io/api/save",
        ts=2,
        body={"enabled": True, "count": 3, "lang": "en"},
        response={},
    )
    assert deps.links([a, b]) == []


# ----------------------------------------------------------------------
# Drift
# ----------------------------------------------------------------------


def shape_of(*responses, status=200, method="GET", body=None):
    calls = [
        call(str(i), method, "https://x.io/api/users", ts=i, status=status, response=r, body=body)
        for i, r in enumerate(responses)
    ]
    return drift.shapes_of(calls)[f"{method} x.io/api/users"]


def test_drift_levels():
    base = shape_of({"users": [{"id": 1, "email": "a@b.c"}], "total": 1})
    assert (
        drift.compare(base, shape_of({"users": [{"id": 2, "email": "x@y.z"}], "total": 4})).level
        == drift.SAME
    )
    added = drift.compare(base, shape_of({"users": [{"id": 2, "email": "x", "role": "admin"}], "total": 4}))
    assert added.level == drift.MINOR and [str(d) for d in added.response] == ["new: $.users[].role"]
    missing = drift.compare(base, shape_of({"users": [{"id": 2}], "total": "4"}))
    assert missing.level == drift.CHANGED
    assert {str(d) for d in missing.response} == {
        "missing: $.users[].email",
        "type changed: $.total (was integer, now string)",
    }
    assert drift.compare(base, shape_of({"error": "gone"}, status=404)).level == drift.FAILING
    unsure = drift.compare(base, shape_of({"error": "login"}, status=401))
    assert unsure.level == drift.UNSURE and "log in again" in unsure.notes[0]


def test_drift_ignores_optional_fields_and_empty_lists():
    base = shape_of({"users": [{"id": 1, "nick": "a"}, {"id": 2}]})  # nick is optional
    assert drift.compare(base, shape_of({"users": [{"id": 3}]})).level == drift.SAME
    empty = drift.compare(base, shape_of({"users": []}))
    assert empty.level == drift.SAME and empty.response[0].kind == "unverified"


def test_drift_request_changes_and_compare_all():
    base = shape_of({}, method="POST", body={"name": "a"})
    later = shape_of({}, method="POST", body={"name": "a", "tags": ["x"]})
    assert drift.compare(base, later).level == drift.MINOR
    results = drift.compare_all({"A": base}, {"B": later})
    assert [(d.key, d.level) for d in results] == [("B", drift.NEW), ("A", drift.NOT_SEEN)]


def test_diff_schemas_map_vs_object():
    old = shapes.infer({"children": {"3f2b8c1e-1d2c-4e5f-8a9b-0c1d2e3f4a5b": {"n": 1}}})
    new = shapes.infer({"children": {"name": "x"}})
    assert [d.kind for d in shapes.diff_schemas(old, new)] == ["type"]


# ----------------------------------------------------------------------
# Storage, HAR, app
# ----------------------------------------------------------------------


def test_store_never_writes_secrets(tmp_path):
    store = Store(tmp_path)
    app = App(store)
    try:
        app.live.calls = session()
        app.live_dirty = True
        app.save_live("Morning")
        app.save_endpoints("live", [c.key for c in session()[:2]])
        everything = "".join(p.read_text() for p in tmp_path.rglob("*.json"))
        assert TOKEN not in everything and "hunter2" not in everything
        assert store.captures()[0]["name"] == "Morning"
        assert len(store.library()) == 2
        loaded = store.load_capture(app.live.id)
        assert [c.key for c in loaded.calls] == [c.key for c in session()]
    finally:
        app.shutdown()


def test_accept_keeps_history_and_drift_against_library(tmp_path):
    app = App(Store(tmp_path))
    try:
        app.live.calls = session()[:3]
        app.save_endpoints("live", [c.key for c in app.live.calls])
        later = session()[:3]
        later[1].response_body = {"users": [{"id": "48213"}]}  # id became a string, name is gone
        app.new_live()
        app.live.calls = later
        report = app.drift("live", "library")
        assert report["summary"] == {"changed": 1, "same": 2}
        changed = report["results"][0]
        assert changed["key"] == later[1].key
        assert {d["text"] for d in changed["response"]} == {
            "type changed: $.users[].id (was integer, now string)",
            "missing: $.users[].name",
        }
        entry = app.accept("live", later[1].key)
        assert len(entry["history"]) == 1
        assert app.drift("live", "library")["summary"] == {"same": 3}
    finally:
        app.shutdown()


def test_compare_two_captures(tmp_path):
    app = App(Store(tmp_path))
    try:
        app.live.calls = session()
        app.live_dirty = True
        first = app.save_live()["id"]
        app.new_live()
        app.live.calls = session()[:2]
        report = app.drift("live", first)
        assert report["summary"] == {"same": 2, "not seen": 2}
    finally:
        app.shutdown()


def test_har_round_trip():
    calls = session()
    back = har.calls_from_har(har.har_from_calls(calls))
    assert [(c.method, c.key, c.status) for c in back] == [
        (c.method, c.key, c.status) for c in sorted(calls, key=lambda c: c.ts)
    ]
    users = next(c for c in back if c.path == "/v1/users")
    assert users.response_body == {"users": [{"id": 48213, "name": "Ada"}]}
    assert users.query == {"limit": "20"}


# ----------------------------------------------------------------------
# Local server
# ----------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    from endpointer.server import Server

    app = App(Store(tmp_path))
    srv = Server(app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()
    app.shutdown()


def request(srv, path, *, body=None, token=True, host=None):
    req = urllib.request.Request(
        srv.origin + path, data=json.dumps(body).encode() if body is not None else None
    )
    if token:
        req.add_header("X-Endpointer-Token", srv.token)
    if host:
        req.add_header("Host", host)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_server_requires_token_and_local_host(server):
    status, page = request(server, "/", token=False)
    assert status == 200 and server.token.encode() in page
    assert request(server, "/api/status", token=False)[0] == 403
    assert request(server, "/api/status", host="evil.example:80")[0] == 403
    assert request(server, "/api/status")[0] == 200


def test_server_import_and_overview(server):
    status, raw = request(
        server, "/api/import/har", body={"har": har.har_from_calls(session()), "name": "Imported"}
    )
    assert status == 200
    capture = json.loads(raw)["id"]
    status, raw = request(server, f"/api/overview?capture={capture}")
    overview = json.loads(raw)
    assert overview["counts"]["calls"] == 6 and overview["counts"]["hidden"] == 2
    assert len(overview["endpoints"]) == 4 and len(overview["links"]) >= 4
    assert request(server, "/api/endpoint?capture=" + capture + "&key=nope")[0] == 404
