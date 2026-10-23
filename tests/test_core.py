"""Templating, response paths, shapes and verdicts."""

from __future__ import annotations

import pytest

from endpoint_checker import shapes, templating
from endpoint_checker.catalog import Endpoint
from endpoint_checker.http import Exchange, redact
from endpoint_checker.judge import CHANGED, FAILING, INCONCLUSIVE, OK, Verdict, combine, judge

# ----------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------


def test_render_keeps_type_of_whole_variable_and_inserts_text_otherwise():
    scope = {"ids": ["a", "b"], "n": 3, "name": "HQ", "flag": True}
    assert templating.render({"x": "{{ids}}", "y": "{{n}}"}, scope) == {"x": ["a", "b"], "y": 3}
    assert templating.render("site {{name}} #{{n}} {{flag}}", scope) == "site HQ #3 true"
    assert templating.render(["{{name}}", 5, None], scope) == ["HQ", 5, None]


def test_render_defaults_and_missing_variables():
    assert templating.render("{{shard|prod1}}", {}) == "prod1"
    with pytest.raises(templating.Unresolved) as e:
        templating.render({"a": "{{x}}", "b": "{{y}} and {{x}}"}, {})
    assert e.value.names == ["x", "y"]
    assert "{{x}}" in str(e.value)


def test_generated_values():
    assert templating.render("{{$stamp}}", {}, stamp="S") == "S"
    now = templating.render("{{$now}}", {})
    assert isinstance(now, int)
    assert templating.render("{{$now+3600}}", {}) - now in (3600, 3601)
    digits = templating.render("{{$digits:6}}", {})
    assert len(digits) == 6 and digits.isdigit()
    assert len(templating.render("{{$uuid}}", {})) == 36
    with pytest.raises(templating.Unresolved):
        templating.render("{{$nope}}", {})


def test_variables_ignores_generated_and_defaulted():
    assert templating.variables({"a": "{{x}}", "b": ["{{y}}", "{{$now}}", "{{z|1}}"]}) == {"x", "y"}


BODY = {
    "sites": [{"siteId": "s1", "name": "HQ"}, {"siteId": "s2", "name": "Lab"}],
    "apiKeys": [{"apiKeyId": "k1", "apiKeyName": "endpoint-checker T1"}],
    "children": {"g1": {"name": "x"}},
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("sites[0].siteId", ["s1"]),
        ("sites[-1].name", ["Lab"]),
        ("sites[*].siteId", ["s1", "s2"]),
        ("sites[?name==Lab].siteId", ["s2"]),
        ("sites[?name!=Lab].siteId", ["s1"]),
        ("apiKeys[?apiKeyName==endpoint-checker T1].apiKeyId", ["k1"]),
        ("children.g1.name", ["x"]),
        ("$.sites[1].siteId", ["s2"]),
        ("missing.path", []),
        ("sites[5].siteId", []),
    ],
)
def test_query(path, expected):
    assert templating.query(BODY, path) == expected


def test_query_top_level_list():
    assert templating.query([{"id": 1}, {"id": 2}], "[*].id") == [1, 2]
    assert templating.query({"a": 1}, "$") == [{"a": 1}]


def test_extract_required_optional_and_lists():
    assert templating.extract(BODY, "sites[0].siteId") == "s1"
    assert templating.extract(BODY, "sites[*].siteId") == ["s1", "s2"]
    assert templating.extract(BODY, "nothing[0].id?") is None
    with pytest.raises(templating.PathError):
        templating.extract(BODY, "nothing[0].id")


# ----------------------------------------------------------------------
# Shapes
# ----------------------------------------------------------------------


def test_infer_and_compare_round_trip():
    value = {"id": "a", "n": 1, "items": [{"x": 1.5, "tags": ["t"]}], "flag": False}
    schema = shapes.infer(value)
    assert shapes.compare(schema, value) == []


def test_compare_reports_missing_type_added_and_unverified():
    schema = shapes.infer({"id": "a", "count": 1, "items": [{"name": "n"}], "rows": [{"a": 1}]})
    actual = {"count": "1", "items": [{"name": "a", "extra": True}], "new": 1, "rows": []}
    diffs = {(d.kind, d.path) for d in shapes.compare(schema, actual)}
    assert ("missing", "$.id") in diffs
    assert ("type", "$.count") in diffs
    assert ("added", "$.items[].extra") in diffs
    assert ("added", "$.new") in diffs
    assert ("unverified", "$.rows[]") in diffs


def test_compare_tolerates_null_optional_list_fields_and_number_kinds():
    schema = shapes.infer({"a": {"b": 1}, "rows": [{"x": 1, "y": "z"}], "f": 1.5})
    actual = {"a": None, "rows": [{"x": 1}, {"x": 2, "y": "q"}], "f": 2}
    assert [d for d in shapes.compare(schema, actual) if d.significant] == []


def test_placeholders_match_any_scalar_but_not_containers():
    schema = shapes.infer({"id": "<site_id>"})
    assert shapes.compare(schema, {"id": 42}) == []
    assert shapes.compare(schema, {"id": "abc"}) == []
    assert [d.kind for d in shapes.compare(schema, {"id": {"nested": 1}})] == ["type"]


def test_merge_keeps_fields_required_only_when_always_present():
    merged = shapes.merge(shapes.infer({"a": 1, "b": "x"}), shapes.infer({"a": 2.5, "c": None}))
    assert merged["required"] == ["a"]
    assert merged["properties"]["a"]["type"] == "number"
    assert set(merged["properties"]) == {"a", "b", "c"}
    assert shapes.infer(None) == {}
    assert shapes.merge({}, {"type": "string"}) == {"type": "string"}


def test_sanitize_keeps_structure_and_enums():
    value = {
        "id": "abc",
        "role": "ORG_ADMIN",
        "on": "True",
        "n": 3,
        "lat": 1.5,
        "items": [{"email": "a@b.c"}, {"email": "d@e.f"}],
        "keep": "<site_id>",
    }
    assert shapes.sanitize(value) == {
        "id": "<id>",
        "role": "ORG_ADMIN",
        "on": "True",
        "n": 3,
        "lat": 0.0,
        "items": [{"email": "<email>"}],
        "keep": "<site_id>",
    }


# ----------------------------------------------------------------------
# Verdicts
# ----------------------------------------------------------------------


def _endpoint(**kw) -> Endpoint:
    base = {"id": "site.list", "api": "command", "method": "POST", "path": "org/site/list", "effect": "read", "host": "vprovision"}
    return Endpoint(**{**base, **kw})


def _exchange(status, body=None) -> Exchange:
    return Exchange("site.list", "POST", "https://x", status, response_json=body, raw_json=body)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, OK),
        (404, FAILING),
        (405, FAILING),
        (400, FAILING),
        (422, FAILING),
        (401, INCONCLUSIVE),
        (403, INCONCLUSIVE),
        (429, INCONCLUSIVE),
        (503, INCONCLUSIVE),
        (0, INCONCLUSIVE),
        (302, CHANGED),
    ],
)
def test_judge_by_status(status, expected):
    endpoint = _endpoint(schema=shapes.infer({"sites": [{"siteId": "s"}]}))
    assert judge(_exchange(status, {"sites": [{"siteId": "x"}]}), endpoint).status == expected


def test_judge_shape_change_and_endpoint_specific_statuses():
    endpoint = _endpoint(schema=shapes.infer({"sites": []}), status=["2xx", 404])
    verdict = judge(_exchange(200, {"renamed": []}), endpoint)
    assert verdict.status == CHANGED and "missing: $.sites" in verdict.reason
    assert judge(_exchange(404, {"message": "no"}), endpoint).status == OK


def test_combine_prefers_success_over_retry_noise():
    combined = combine([Verdict(INCONCLUSIVE, "503"), Verdict(OK, "200")])
    assert combined.status == OK
    assert combine([Verdict(OK, "200"), Verdict(FAILING, "404")]).status == FAILING


def test_redact():
    value = {"password": "pw", "nested": [{"apiKey": "k", "x-api-key": "k2"}], "name": "keep", "token": None}
    assert redact(value) == {"password": "***", "nested": [{"apiKey": "***", "x-api-key": "***"}], "name": "keep", "token": None}


def test_maps_keyed_by_ids():
    schema = shapes.infer({"children": {"<access_group_id>": {"name": "<name>"}}})
    children = schema["properties"]["children"]
    assert children == {"type": "object", "additionalProperties": {"type": "object", "properties": {"name": {"type": ["string", "number", "boolean"]}}, "required": ["name"]}}
    real = {"children": {"5f1c2d3e-0000-4000-8000-000000000000": {"name": "Lab"}, "6a1c2d3e-0000-4000-8000-000000000000": {"name": "HQ"}}}
    assert shapes.compare(schema, real) == []
    assert [d.kind for d in shapes.compare(schema, {"children": {"5f1c2d3e-0000-4000-8000-000000000000": {"label": "x"}}})] == ["missing", "added"]
    assert shapes.compare(schema, {"children": {}})[0].kind == "unverified"
    assert shapes.sanitize(real) == {"children": {"<children_key>": {"name": "<name>"}}}
    assert shapes.compare(shapes.infer(real), real) == []
