"""The catalog format: loading, saving, validation, init, OpenAPI export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from endpoint_checker import catalog as catalog_mod
from endpoint_checker import openapi
from endpoint_checker.catalog import CatalogError, Endpoint


def test_starter_catalog_is_valid(catalog):
    errors, warnings = catalog_mod.validate(catalog)
    assert errors == []
    assert warnings == []
    assert len(catalog.endpoints) > 150
    assert {"read-alarms", "read-guest", "public-api-access", "sites", "users"} <= set(catalog.scenarios)


def test_files_match_their_json_schemas(catalog_dir):
    jsonschema = pytest.importorskip("jsonschema")
    for kind, pattern in (("endpoint", "endpoints/*/*.json"), ("scenario", "scenarios/*.json"), ("catalog", "catalog.json")):
        validator = jsonschema.Draft202012Validator(json.loads((catalog_dir / "schema" / f"{kind}.schema.json").read_text()))
        for path in catalog_dir.glob(pattern):
            errors = [e.message for e in validator.iter_errors(json.loads(path.read_text()))]
            assert errors == [], (path, errors)


def test_save_round_trips_files_byte_for_byte(catalog):
    for endpoint in list(catalog.endpoints.values())[:40]:
        before = endpoint.file.read_text()
        catalog.save_endpoint(endpoint, touch=False)
        assert endpoint.file.read_text() == before, endpoint.id


def test_no_user_targeting_endpoint_defaults_to_the_logged_in_user(catalog):
    for endpoint_id in ("user.delete", "public.user.delete", "user.add_license_plate", "access_group.add_user"):
        assert "user_id" not in catalog.endpoints[endpoint_id].template_variables(), endpoint_id


def test_dumps_keeps_short_scalar_lists_inline():
    text = catalog_mod.dumps({"a": ["2xx", 404], "b": [{"x": 1}], "c": {}})
    assert '"a": ["2xx", 404]' in text
    assert '"c": {}' in text
    assert json.loads(text) == {"a": ["2xx", 404], "b": [{"x": 1}], "c": {}}


def test_status_ok():
    endpoint = Endpoint("a.b", "command", "GET", "x", "read", status=["2xx", 404])
    assert endpoint.status_ok(204) and endpoint.status_ok(404)
    assert not endpoint.status_ok(400)


def test_validate_reports_problems(catalog, catalog_dir):
    catalog.endpoints["site.list"].effect = "explode"
    catalog.endpoints["site.create"].host = None
    scenario = catalog.scenarios["read-alarms"]
    scenario.steps[0].call = "no.such"
    scenario.steps[1].call = "site.delete"
    errors, _ = catalog_mod.validate(catalog)
    text = "\n".join(errors)
    assert "effect 'explode'" in text
    assert "needs a host" in text
    assert "unknown endpoint 'no.such'" in text
    assert "read scenario calls site.delete" in text


def test_save_rejects_bad_ids(catalog):
    with pytest.raises(CatalogError):
        catalog.save_endpoint(Endpoint("Bad Id", "command", "GET", "x", "read", host="h"))


def test_load_errors(tmp_path):
    with pytest.raises(CatalogError, match=r"no catalog\.json"):
        catalog_mod.load(tmp_path)
    (tmp_path / "catalog.json").write_text('{"format": "other/9"}')
    with pytest.raises(CatalogError, match="unsupported format"):
        catalog_mod.load(tmp_path)


def test_init_full_and_empty(tmp_path):
    full = catalog_mod.init(tmp_path / "full")
    assert (full / ".gitignore").read_text() == "reports/\n"
    loaded = catalog_mod.load(full)
    assert len(loaded.endpoints) > 150
    empty = catalog_mod.load(catalog_mod.init(tmp_path / "empty", empty=True))
    assert set(empty.endpoints) == {
        "auth.login",
        "auth.twofactor.sms.new",
        "org.api_key.create",
        "org.api_key.list",
        "org.api_key.delete",
        "public.token",
    }
    assert catalog_mod.validate(empty)[0] == []
    with pytest.raises(CatalogError, match="already has"):
        catalog_mod.init(tmp_path / "full")


def test_find_prefers_flag_env_then_cwd(tmp_path):
    assert catalog_mod.find("x", "y") == Path("x")
    assert catalog_mod.find(None, "y") == Path("y")
    (tmp_path / "catalog.json").write_text("{}")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert catalog_mod.find(None, None, cwd=nested) == tmp_path
    assert catalog_mod.find(None, None, cwd=Path("/")) == catalog_mod.STARTER


def test_openapi_export_is_valid(catalog):
    doc = openapi.export(catalog)
    assert doc["openapi"] == "3.1.0"
    op = doc["paths"]["/org/camera_group/create"]["post"]
    assert op["operationId"] == "site.create"
    assert op["servers"][0]["url"] == "https://vprovision.command.verkada.com/__v/{org_short_name}"
    validator = pytest.importorskip("openapi_spec_validator")
    validator.validate(doc)
