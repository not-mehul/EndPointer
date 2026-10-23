"""HAR import/analysis and the command line."""

from __future__ import annotations

import builtins
import json

import pytest

from endpoint_checker import catalog as catalog_mod
from endpoint_checker import cli, har
from endpoint_checker.judge import FAILING, OK
from endpoint_checker.runner import analyze

UUID = "5f1c2d3e-0000-4000-8000-000000000000"


def _har(tmp_path):
    def entry(method, url, status, request=None, response=None):
        return {
            "startedDateTime": "2026-01-01T00:00:00Z",
            "request": {
                "method": method,
                "url": url,
                "headers": [
                    {"name": "x-verkada-organization-id", "value": "org-123"},
                    {"name": "x-verkada-user-id", "value": "user-9"},
                ],
                **({"postData": {"text": json.dumps(request)}} if request is not None else {}),
            },
            "response": {"status": status, "content": {"text": json.dumps(response) if response is not None else ""}},
        }

    data = {
        "log": {
            "entries": [
                entry(
                    "POST",
                    f"https://vcerberus.command.verkada.com/__v/acme/org-123/door/{UUID}/unlock",
                    200,
                    {"userId": "user-9", "reason": "test"},
                    {"ok": True, "doorId": "d1"},
                ),
                entry("POST", "https://vprovision.command.verkada.com/__v/acme/org/site/list", 404, {"orgId": "org-123"}, {"message": "gone"}),
                entry("GET", "https://api.verkada.com/cameras/v1/devices?page_size=10", 200, None, {"cameras": [], "next_page_token": None}),
                entry("GET", "https://cdn.example.com/app.js", 200),
                entry("OPTIONS", "https://vprovision.command.verkada.com/__v/acme/org/site/list", 204),
            ]
        }
    }
    path = tmp_path / "session.har"
    path.write_text(json.dumps(data))
    return path


def test_har_candidates_template_ids_and_mark_known(catalog, tmp_path):
    candidates = har.candidates(catalog, str(_har(tmp_path)))
    assert len(candidates) == 3
    unlock = next(c for c in candidates if "unlock" in c.path)
    assert unlock.host == "vcerberus" and unlock.api == "command"
    assert unlock.path == "{{org_id}}/door/{{door_id}}/unlock"
    assert unlock.params == {"door_id": UUID}
    assert unlock.body == {"userId": "{{user_id}}", "reason": "test"}
    assert unlock.known == []
    assert har.suggest_id(unlock.method, unlock.path) == "door.unlock"
    site_list = next(c for c in candidates if "site/list" in c.path)
    assert site_list.known == ["site.list"]
    cameras = next(c for c in candidates if c.api == "public")
    assert cameras.known == ["public.cameras.list"] and cameras.query == {"page_size": "10"}


def test_analyze_har(catalog, tmp_path):
    exchanges = har.exchanges(str(_har(tmp_path)))
    assert len(exchanges) == 4  # preflight dropped, CDN kept as unmatched
    result = analyze(catalog, exchanges, source="session.har")
    assert result.endpoints["site.list"].status == FAILING
    assert "gone" in result.endpoints["site.list"].verdict.reason
    assert result.endpoints["public.cameras.list"].status == OK
    assert {e.url.split("/")[2] for e in result.unknown} == {"vcerberus.command.verkada.com", "cdn.example.com"}


def test_cli_offline_commands(catalog_dir, tmp_path, capsys):
    base = ["--catalog", str(catalog_dir)]
    assert cli.main([*base, "validate"]) == 0
    assert cli.main([*base, "list", "--search", "camera_group"]) == 0
    assert "site.create" in capsys.readouterr().out
    assert cli.main([*base, "show", "site.create"]) == 0
    assert "used by scenarios: alarms, sites, workplace" in capsys.readouterr().out
    out = tmp_path / "openapi.json"
    assert cli.main([*base, "export", "openapi", "-o", str(out)]) == 0
    assert json.loads(out.read_text())["openapi"] == "3.1.0"
    reports = tmp_path / "reports"
    assert cli.main([*base, "analyze", str(_har(tmp_path)), "--out", str(reports)]) == 0
    assert len(list(reports.iterdir())) == 2
    new = tmp_path / "mine"
    assert cli.main(["init", str(new), "--empty"]) == 0
    assert cli.main(["--catalog", str(new), "validate"]) == 0


def _answers(monkeypatch, answers):
    queue = list(answers)
    monkeypatch.setattr(builtins, "input", lambda prompt="": queue.pop(0))
    return queue


def test_record_from_har_writes_an_endpoint_file(catalog_dir, tmp_path, monkeypatch):
    catalog = catalog_mod.load(catalog_dir)
    args = cli.build_parser().parse_args(["--catalog", str(catalog_dir), "record", "--har", str(_har(tmp_path))])
    args.interactive = True
    session = cli.Session(catalog, args)
    queue = _answers(
        monkeypatch,
        [
            "n",  # show known ones too? (default: no, there are new ones)
            "1",  # pick the unlock call
            "",  # keep the path template
            "",  # accept suggested id door.unlock
            "Unlock a door",  # title
            "action",  # effect
            "Seen in Access → Doors",  # notes
        ],
    )
    cli.run_record(session)
    assert queue == []
    saved = json.loads((catalog_dir / "endpoints" / "door" / "door.unlock.json").read_text())
    assert saved["host"] == "vcerberus" and saved["method"] == "POST" and saved["effect"] == "action"
    assert saved["path"] == "{{org_id}}/door/{{door_id}}/unlock"
    assert saved["params"] == {"door_id": {"description": "id seen in the HAR"}}
    assert saved["request"]["body"] == {"userId": "{{user_id}}", "reason": "test"}
    assert saved["response"]["example"] == {"ok": True, "doorId": "<doorId>"}
    assert saved["source"] == {"from": "HAR", "file": "session.har"}
    assert catalog_mod.validate(catalog_mod.load(catalog_dir))[0] == []


def test_cli_call_and_check(catalog, catalog_dir, transport, fake, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli.Session, "login", lambda self: transport)
    assert cli.main(["--catalog", str(catalog_dir), "call", "site.list"]) == 0
    assert "HTTP 200 · ok" in capsys.readouterr().out
    # Non-read calls need --yes when not interactive.
    assert cli.main(["--catalog", str(catalog_dir), "call", "site.delete", "--set", "site_id=s1"]) == 0
    assert "Not sent." in capsys.readouterr().out
    assert "site.delete" not in fake.ids()

    reports = tmp_path / "reports"
    args = ["--catalog", str(catalog_dir), "check", "--scenario", "sites", "--no-public", "--out", str(reports)]
    _answers(monkeypatch, ["wrong-org"])  # typed confirmation doesn't match: read-only only
    assert cli.main(args) == 0
    assert "site.create" not in fake.ids()
    code = cli.main([*args, "--confirm", "acme"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Sites & buildings" in out and "Report:" in out
    assert "site.create" in fake.ids() and "site.delete" in fake.ids()
    assert len(list(reports.iterdir())) >= 2


def test_check_rejects_unknown_scenario(catalog_dir, transport, monkeypatch):
    monkeypatch.setattr(cli.Session, "login", lambda self: transport)
    with pytest.raises(SystemExit, match="No write/settings scenario 'nope'"):
        cli.main(["--catalog", str(catalog_dir), "check", "--scenario", "nope"])


def test_accept_updates_schema_and_example(catalog, catalog_dir):
    path = cli._accept(catalog, "site.list", [{"sites": [{"siteId": "s1", "new": 1}]}])
    saved = json.loads(path.read_text())
    assert saved["response"]["example"] == {"sites": [{"siteId": "<siteId>", "new": 1}]}
    assert set(saved["response"]["schema"]["properties"]["sites"]["items"]["properties"]) == {"siteId", "new"}
