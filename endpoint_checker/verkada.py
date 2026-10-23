"""Command login (password + SMS/authenticator 2FA), driven by catalog.json.

The catalog's `login` section names the endpoints and response fields, so a
changed field name is fixed in the catalog rather than in code:

    "login": {
      "endpoint": "auth.login",
      "success": {"path": "loggedIn", "equals": true},
      "save": {"csrf_token": "csrfToken", "org_id": "organizationId", ...},
      "mfa": {
        "when_message_contains": "2FA invalid",
        "otp_field": "otp",
        "sms_enabled": "data.smsEnabled?",
        "sms_endpoint": "auth.twofactor.sms.new",
        "sms_contact": "smsSent?"
      }
    }
"""

from __future__ import annotations

from collections.abc import Callable

from endpoint_checker import templating
from endpoint_checker.http import Exchange, Transport

MAX_CODE_ATTEMPTS = 5


class LoginError(Exception):
    pass


def _succeeded(exchange: Exchange, config: dict) -> bool:
    if not exchange.ok:
        return False
    rule = config.get("success") or {}
    if not rule:
        return True
    values = templating.query(exchange.raw_json, rule["path"])
    return bool(values) and values[0] == rule.get("equals", True)


def _save(transport: Transport, exchange: Exchange, config: dict) -> None:
    for var, path in (config.get("save") or {}).items():
        try:
            transport.vars[var] = templating.extract(exchange.raw_json, path)
        except templating.PathError as e:
            raise LoginError(f"logged in, but the login response changed: {e}") from None


def login(
    transport: Transport,
    *,
    email: str,
    password: str,
    org_short_name: str,
    shard: str = "prod1",
    region: str = "api",
    ask_code: Callable[[str | None, bool], str],
) -> None:
    """Log in; on success the session variables (org_id, user_id, …) are set.

    `ask_code(sms_contact, wrong)` is called for the 2FA code (`wrong` after
    an incorrect one); it may return "r" to resend the SMS.
    """
    catalog = transport.catalog
    config = catalog.login
    endpoint = catalog.endpoint(config["endpoint"])
    transport.vars.update(email=email, org_short_name=org_short_name, shard=shard, region=region)
    secrets = {"password": password}

    exchange = transport.send(endpoint, secrets, retries=0)
    if _succeeded(exchange, config):
        _save(transport, exchange, config)
        return

    mfa = config.get("mfa") or {}
    marker = mfa.get("when_message_contains")
    if not (marker and marker in _message(exchange)):
        raise LoginError(f"Login failed: {_message(exchange) or f'HTTP {exchange.status}'}")

    sms_contact = None
    sms_endpoint = catalog.endpoints.get(mfa.get("sms_endpoint", ""))

    def send_sms() -> str | None:
        if sms_endpoint is None:
            return None
        sent = transport.send(sms_endpoint, secrets, retries=0)
        if not sent.ok:
            return None
        return templating.extract(sent.raw_json, mfa.get("sms_contact") or "smsSent?")

    sms_enabled = templating.extract(exchange.raw_json, mfa.get("sms_enabled") or "data.smsEnabled?")
    if sms_enabled:
        sms_contact = send_sms()

    wrong = False
    for _ in range(MAX_CODE_ATTEMPTS):
        code = ask_code(sms_contact, wrong).strip()
        while code.lower() == "r":
            sms_contact = send_sms() or sms_contact
            code = ask_code(sms_contact, False).strip()
        exchange = transport.send(endpoint, secrets, body_update={mfa.get("otp_field", "otp"): code}, retries=0)
        if _succeeded(exchange, config):
            _save(transport, exchange, config)
            return
        if marker not in _message(exchange):
            raise LoginError(f"2FA verification failed: {_message(exchange) or f'HTTP {exchange.status}'}")
        wrong = True
    raise LoginError("Too many incorrect 2FA codes.")


def _message(exchange: Exchange) -> str:
    body = exchange.raw_json
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return exchange.message()
