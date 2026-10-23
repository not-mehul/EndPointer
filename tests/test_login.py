"""Command login and 2FA, driven by catalog.json's login section."""

from __future__ import annotations

import json

import pytest

from endpoint_checker import verkada
from endpoint_checker.http import Transport


def _login(transport, codes=()):
    answers = iter(codes)
    asked = []

    def ask_code(contact, wrong):
        asked.append((contact, wrong))
        return next(answers)

    verkada.login(
        transport,
        email="a@example.com",
        password="secret-pw",
        org_short_name="acme",
        shard="prod1",
        region="api",
        ask_code=ask_code,
    )
    return asked


def test_login_saves_session_variables_and_redacts_password(catalog, fake):
    transport = Transport(catalog)
    _login(transport)
    assert transport.vars["csrf_token"] == "<csrf>"
    assert transport.vars["org_id"] == "<org_id>"
    assert transport.vars["org_short_name"] == "acme"
    body = fake.calls[0][3]
    assert body["password"] == "secret-pw" and body["orgShortName"] == "acme" and body["shard"] == "prod1"
    recorded = json.dumps([e.to_dict() for e in transport.exchanges])
    assert "secret-pw" not in recorded and "<csrf>" not in recorded


def test_login_with_sms_2fa_resend_and_wrong_code(catalog, fake):
    example = catalog.endpoints["auth.login"].example

    def login(request):
        body = json.loads(request.body)
        if "otp" not in body:
            return 200, {"message": "2FA invalid", "data": {"smsEnabled": True}}
        if body["otp"] == "111111":
            return 200, {"message": "2FA invalid"}
        return 200, example

    fake.overrides["auth.login"] = login
    fake.overrides["auth.twofactor.sms.new"] = (200, {"smsSent": "7647"})
    transport = Transport(catalog)
    asked = _login(transport, codes=["r", "111111", "222222"])
    assert asked == [("7647", False), ("7647", False), ("7647", True)]
    assert fake.ids().count("auth.twofactor.sms.new") == 2
    assert fake.calls[-1][3]["otp"] == "222222"
    assert transport.vars["user_id"] == "<user_id>"


def test_login_failure_and_changed_response(catalog, fake):
    fake.overrides["auth.login"] = (200, {"message": "Invalid email or password"})
    with pytest.raises(verkada.LoginError, match="Invalid email or password"):
        _login(Transport(catalog))
    fake.overrides["auth.login"] = (200, {"loggedIn": True, "organizationId": "o", "userId": "u", "userToken": "t"})
    with pytest.raises(verkada.LoginError, match="csrfToken"):
        _login(Transport(catalog))
