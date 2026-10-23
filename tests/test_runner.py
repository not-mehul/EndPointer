"""Live checks against the fake Verkada: read checks, scenarios, cleanup, setup."""

from __future__ import annotations

import json

import pytest

from endpoint_checker.judge import CHANGED, FAILING, INCONCLUSIVE, NOT_EXERCISED, OK
from endpoint_checker.runner import Run


def with_ids(catalog, endpoint_id: str, mapping: dict) -> tuple[int, object]:
    """The endpoint's example with placeholders replaced (a consistent fake object)."""
    text = json.dumps(catalog.endpoints[endpoint_id].example)
    for placeholder, value in mapping.items():
        text = text.replace(f'"<{placeholder}>"', json.dumps(value))
    return 200, json.loads(text)


def test_read_only_check_is_all_ok_when_nothing_changed(catalog, transport, fake):
    result = Run(catalog, transport).check([])
    exercised = {e.id: e for e in result.endpoints.values() if e.exercised}
    assert {k: e.status for k, e in exercised.items() if e.status != OK} == {}
    # Every read endpoint is called, except one that needs a device serial.
    unread = {e.id: result.endpoints[e.id].reason_not_exercised for e in catalog.endpoints.values() if e.is_read and e.id not in exercised}
    assert unread == {"org.device_information.list": "needs {{serial_number}}"}
    assert result.leftovers == [] and result.unknown == []
    # Only reads and the temporary key's lifecycle were called.
    for endpoint_id in fake.ids():
        endpoint = catalog.endpoints[endpoint_id]
        assert endpoint.is_read or endpoint_id in ("org.api_key.create", "org.api_key.delete", "public.token"), endpoint_id
    assert fake.ids()[-1] == "org.api_key.delete"
    assert "public.cameras.list" in exercised
    # Nothing destructive, and the reasons for what wasn't called.
    assert result.endpoints["org.check_empty"].status == NOT_EXERCISED
    assert "Sites & buildings" in result.endpoints["site.create"].reason_not_exercised
    assert "call camera.delete" in result.endpoints["camera.delete"].reason_not_exercised
    # The setup's key and token are forgotten afterwards.
    assert "public_token" not in transport.vars and "api_key" not in transport.vars


def test_read_only_check_flags_changes(catalog, transport, fake):
    fake.overrides.update(
        {
            "site.list": (200, {"renamed": []}),
            "door.list": (404, {"message": "Not Found"}),
            "public.access_groups.list": (403, {"message": "forbidden"}),
            "user.list": (500, {"message": "boom"}),
        }
    )
    result = Run(catalog, transport).check([])
    assert result.endpoints["site.list"].status == CHANGED
    assert result.endpoints["door.list"].status == FAILING
    assert result.endpoints["public.access_groups.list"].status == INCONCLUSIVE
    # Reads are retried on 5xx: three attempts.
    assert result.endpoints["user.list"].calls == 3
    assert result.endpoints["user.list"].status == INCONCLUSIVE


def test_without_setups_public_endpoints_are_skipped(catalog, transport, fake):
    result = Run(catalog, transport, use_setups=False).check([])
    assert "org.api_key.create" not in fake.ids()
    cameras = result.endpoints["public.cameras.list"]
    assert cameras.status == NOT_EXERCISED and "skipped" in cameras.reason_not_exercised


def _sites_overrides(catalog):
    return {
        "site.create": with_ids(catalog, "site.create", {"site_id": "s1"}),
        "site.list": with_ids(catalog, "site.list", {"id": "s1"}),
        "building.create": with_ids(catalog, "building.create", {"building_id": "b1", "floor_id": "f1"}),
        "building.list": with_ids(catalog, "building.list", {"building_id": "b1"}),
        "floor.list": with_ids(catalog, "floor.list", {"floor_1_id": "f1"}),
    }


def test_write_scenario_creates_verifies_and_cleans_up_in_reverse(catalog, transport, fake):
    fake.overrides.update(_sites_overrides(catalog))
    result = Run(catalog, transport).check([catalog.scenarios["sites"]], auto_reads=False)
    sites = [c for c in fake.calls if c[0] in ("site.create", "site.rename", "building.create", "floor.delete", "building.delete", "site.delete")]
    assert [c[0] for c in sites] == ["site.create", "site.rename", "building.create", "floor.delete", "building.delete", "site.delete"]
    bodies = {c[0]: c[3] for c in sites}
    assert bodies["site.create"]["name"] == "Endpoint check T1"
    assert bodies["building.create"]["floors"] == ["G"]
    assert bodies["building.create"]["latitude"] == pytest.approx(37.566, abs=0.01)
    assert bodies["floor.delete"] == {"floorId": "f1"}
    assert bodies["building.delete"] == {"buildingId": "b1"}
    assert bodies["site.delete"] == {"cameraGroupId": "s1"}
    assert result.leftovers == []
    for endpoint_id in ("site.create", "site.list", "building.create", "floor.list", "site.delete"):
        assert result.endpoints[endpoint_id].status == OK, (endpoint_id, result.endpoints[endpoint_id].verdict)
    assert result.mode == "write"


def test_failed_expectation_marks_the_endpoint_changed(catalog, transport, fake):
    fake.overrides.update(_sites_overrides(catalog))
    fake.overrides["site.list"] = with_ids(catalog, "site.list", {"id": "someone-else"})
    result = Run(catalog, transport).check([catalog.scenarios["sites"]], auto_reads=False)
    site_list = result.endpoints["site.list"]
    assert site_list.status == CHANGED
    assert "expected 's1' among sites[*].siteId" in site_list.notes[0]
    assert "site.delete" in fake.ids()  # still cleaned up


def test_failed_cleanup_uses_fallback_and_reports_leftover(catalog, transport, fake):
    fake.overrides.update(_sites_overrides(catalog))
    fake.overrides["site.delete"] = (500, {"message": "site not empty"})
    result = Run(catalog, transport).check([catalog.scenarios["sites"]], auto_reads=False)
    assert fake.ids().count("site.delete") == 1  # writes aren't retried
    renames = [c[3]["name"] for c in fake.calls if c[0] == "site.rename"]
    assert renames[-1] == "Endpoint check T1 (delete me)"
    assert result.leftovers == ["Sites & buildings: Delete the site failed; did 'Rename the site that couldn't be deleted' instead"]
    assert [s.state for s in result.steps if s.step == "Delete the site"] == ["leftover"]


def test_cleanup_still_runs_after_ctrl_c(catalog, transport, fake):
    fake.overrides.update(_sites_overrides(catalog))

    def interrupt(request):
        raise KeyboardInterrupt

    fake.overrides["site.rename"] = interrupt
    result = Run(catalog, transport).check([catalog.scenarios["sites"]], auto_reads=False)
    assert result.interrupted
    ids = fake.ids()
    assert ids.index("site.delete") > ids.index("site.create")
    assert "building.create" not in ids


def test_scenario_inputs_and_lookup_cleanup(catalog, transport, fake):
    users = catalog.scenarios["users"]
    result = Run(catalog, transport).check([users], auto_reads=False)
    assert "user.create" not in fake.ids()
    assert "needs input invite_email" in result.endpoints["user.create"].reason_not_exercised

    fake.calls.clear()
    fake.overrides["user.list"] = (200, {"users": [{"email": "t@example.com", "userId": "u9", "firstName": "E", "lastName": "C", "isOrganizationAdmin": False}]})
    result = Run(catalog, transport, inputs={"invite_email": "t@example.com"}).check([users], auto_reads=False)
    invite = next(c[3] for c in fake.calls if c[0] == "user.create")
    assert invite["email"] == "t@example.com" and invite["firstName"] == "Endpoint"
    delete = next(c[3] for c in fake.calls if c[0] == "user.delete")
    assert delete["userIds"] == ["u9"]
    assert result.leftovers == []


def test_public_api_write_uses_setup_once(catalog, transport, fake):
    fake.overrides.update(
        {
            "group.create": with_ids(catalog, "group.create", {"group_id": "g1"}),
            "group.list": with_ids(catalog, "group.list", {"group_id": "g1"}),
            "access_group.create": (200, {"groupId": "ag1"}),
            "access_group.list": (200, {"children": {"ag1": {"name": "x"}}}),
            "public.access_group.create": (200, {"group_id": "pg1", "name": "x"}),
            "public.access_groups.list": (200, {"access_groups": [{"group_id": "pg1", "name": "x"}]}),
        }
    )
    result = Run(catalog, transport).check([catalog.scenarios["groups"]], auto_reads=False)
    ids = fake.ids()
    assert ids.count("org.api_key.create") == 1 and ids.count("public.token") == 1
    # Public group deleted (scenario cleanup) before the key (run cleanup).
    assert ids.index("public.access_group.delete") < ids.index("org.api_key.delete")
    add = next(c for c in fake.calls if c[0] == "public.access_group.add_user")
    assert "group_id=pg1" in add[2] and add[3] == {"user_id": "me"}
    token_call = next(c for c in fake.calls if c[0] == "public.access_groups.list")
    assert token_call is not None
    assert result.leftovers == []
    statuses = {i: (result.endpoints[i].status, result.endpoints[i].verdict.reason) for i in ("group.list", "access_group.list", "public.access_groups.list")}
    assert {i: s for i, s in statuses.items() if s[0] != OK} == {}


def test_auth_headers_and_cookies(catalog, transport):
    request = transport.build(catalog.endpoints["site.list"])
    assert request["url"] == "https://vprovision.command.verkada.com/__v/acme/org/site/list"
    assert request["headers"]["x-verkada-token"] == "csrf"
    assert request["cookies"] == {"auth": "ut", "org": "org1", "usr": "me", "token": "csrf"}
    assert request["json"] == {"orgId": "org1"}
    login = transport.build(catalog.endpoints["auth.login"], {"password": "pw"})
    assert "x-verkada-token" not in login["headers"] and login["cookies"] is None
