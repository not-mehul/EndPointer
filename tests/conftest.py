"""Fixtures: a scratch copy of the starter catalog and a fake Verkada server."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import responses

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from endpoint_checker import catalog as catalog_mod  # noqa: E402
from endpoint_checker.http import Transport  # noqa: E402
from endpoint_checker.matching import Matcher  # noqa: E402

STAMP = "T1"


@pytest.fixture
def catalog_dir(tmp_path) -> Path:
    target = tmp_path / "catalog"
    shutil.copytree(catalog_mod.STARTER, target, ignore=shutil.ignore_patterns("reports", "openapi.json"))
    return target


@pytest.fixture
def catalog(catalog_dir):
    return catalog_mod.load(catalog_dir)


class FakeVerkada:
    """Answers every call with the endpoint's stored example, unless overridden.

    overrides: endpoint id -> (status, body) or a callable(request) -> (status, body).
    """

    def __init__(self, rsps, catalog):
        self.catalog = catalog
        self.matcher = Matcher(catalog)
        self.api_keys: list[str] = []  # names of keys created through org.api_key.create
        self.overrides: dict = {
            "org.api_key.list": lambda request: (
                200,
                {"apiKeys": [{"apiKeyId": f"k{i}", "apiKeyName": name} for i, name in enumerate(self.api_keys)]},
            ),
        }
        self.calls: list[tuple[str | None, str, str, object]] = []
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            rsps.add_callback(method, re.compile(r"https://.*"), callback=self._answer)

    def ids(self) -> list[str | None]:
        return [c[0] for c in self.calls]

    def _answer(self, request):
        ids = self.matcher.match(request.method, request.url)
        key = ids[0] if ids else None
        body = json.loads(request.body) if request.body else None
        self.calls.append((key, request.method, request.url, body))
        if key == "org.api_key.create" and isinstance(body, dict):
            self.api_keys.append(body.get("api_key_name"))
        override = self.overrides.get(key)
        if callable(override):
            override = override(request)
        if override is not None:
            status, payload = override
        elif key is None:
            status, payload = 404, {"message": "unknown route"}
        else:
            example = self.catalog.endpoints[key].example
            status, payload = 200, (example if example is not None else {})
        return status, {"Content-Type": "application/json"}, json.dumps(payload)


@pytest.fixture
def fake(catalog):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield FakeVerkada(rsps, catalog)


@pytest.fixture
def transport(catalog) -> Transport:
    """A transport that is already 'logged in'."""
    t = Transport(catalog, stamp=STAMP)
    t.sleep = lambda seconds: None
    t.vars.update(
        email="admin@example.com",
        org_short_name="acme",
        region="api",
        shard="prod1",
        org_id="org1",
        user_id="me",
        csrf_token="csrf",
        user_token="ut",
    )
    return t
