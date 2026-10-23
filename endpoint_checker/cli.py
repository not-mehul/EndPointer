"""Command line: check a Verkada org against the catalog, and grow the catalog.

    endpoint-checker                         menu (logs in when an action needs it)
    endpoint-checker check [--scenario sites,groups | --all-writes]
    endpoint-checker call site.list          send one endpoint, show the answer
    endpoint-checker record [--har FILE]     add a new endpoint (typed in or from a HAR)
    endpoint-checker analyze FILE.har        judge recorded browser traffic (no login)
    endpoint-checker list | show ID | validate | export openapi
    endpoint-checker init DIR [--empty]      start a new catalog (its own git repo)
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import sys
from pathlib import Path

from endpoint_checker import catalog as catalog_mod
from endpoint_checker import har, report, shapes, templating, verkada
from endpoint_checker.catalog import Catalog, CatalogError, Endpoint
from endpoint_checker.http import Skip, Transport
from endpoint_checker.judge import CHANGED, judge
from endpoint_checker.report import paint
from endpoint_checker.runner import Run, StepOutcome, analyze

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
REGIONS = ("api", "api.eu", "api.au")
EFFECT_FOR_METHOD = {"GET": "read", "POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}


# ----------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------


def ask(prompt: str, default: str | None = None, *, required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if value or not required:
            return value
        print("  (required)")


def confirm(prompt: str, default: bool = False) -> bool:
    value = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not value else value in ("y", "yes")


def choose(prompt: str, options: list[str], default: str | None = None) -> str:
    while True:
        value = ask(f"{prompt} ({'/'.join(options)})", default)
        if value in options:
            return value
        print(f"  choose one of: {', '.join(options)}")


def ask_json(prompt: str):
    """Multi-line JSON, finished by an empty line. Empty → None."""
    print(f"{prompt} (paste JSON, end with an empty line; empty for none)")
    while True:
        lines = []
        while True:
            line = input()
            if not line.strip():
                break
            lines.append(line)
        if not lines:
            return None
        try:
            return json.loads("\n".join(lines))
        except ValueError as e:
            print(f"  Not valid JSON ({e}). Try again:")


def show_json(value, *, max_lines: int = 40, indent: str = "    ") -> None:
    if value is None:
        print(f"{indent}(no JSON body)")
        return
    lines = json.dumps(value, indent=2, ensure_ascii=False).splitlines()
    for line in lines[:max_lines]:
        print(indent + line)
    if len(lines) > max_lines:
        print(paint(f"{indent}… {len(lines) - max_lines} more lines", "grey"))


def parse_pairs(pairs: list[str] | None) -> dict:
    """k=v pairs; values that parse as JSON (numbers, lists, true) keep their type."""
    out = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"Expected name=value, got {pair!r}")
        try:
            out[key.strip()] = json.loads(value)
        except ValueError:
            out[key.strip()] = value
    return out


# ----------------------------------------------------------------------
# Login (lazy)
# ----------------------------------------------------------------------


def _profile_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "endpoint-checker" / "profile.json"


def _load_profile() -> dict:
    try:
        return json.loads(_profile_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_profile(profile: dict) -> None:
    with contextlib.suppress(OSError):
        path = _profile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile, indent=2), encoding="utf-8")


class Session:
    """The catalog plus a logged-in transport (created on first use)."""

    def __init__(self, catalog: Catalog, args: argparse.Namespace):
        self.catalog = catalog
        self.args = args
        self.transport: Transport | None = None

    def login(self) -> Transport:
        if self.transport is not None:
            return self.transport
        profile = _load_profile()
        print(paint(f"Log in to Verkada Command (catalog: {self.catalog.name})", "bold"))
        for _attempt in range(3):
            email = ask("Email", profile.get("email"))
            org = ask("Org short name", profile.get("org_short_name"))
            region = choose("Region", list(REGIONS), profile.get("region") or "api")
            shard = ask("Shard", profile.get("shard") or "prod1")
            password = getpass.getpass("Password: ")
            transport = Transport(self.catalog)

            def ask_code(contact: str | None, wrong: bool) -> str:
                if wrong:
                    print("  Incorrect code, try again.")
                where = f"SMS to the phone ending in {contact}" if contact else "your authenticator app"
                return ask(f"2FA code from {where} ('r' to resend the SMS)")

            try:
                verkada.login(
                    transport,
                    email=email,
                    password=password,
                    org_short_name=org,
                    shard=shard,
                    region=region,
                    ask_code=ask_code,
                )
            except (verkada.LoginError, templating.Unresolved, CatalogError) as e:
                print(paint(str(e), "red"))
                profile = {**profile, "email": email, "org_short_name": org, "region": region, "shard": shard}
                continue
            _save_profile({"email": email, "org_short_name": org, "region": region, "shard": shard})
            print(paint(f"Logged in as {email} on {org} ({region}).", "green"))
            self.transport = transport
            return transport
        raise SystemExit("Giving up after 3 login attempts.")


def _print_step(outcome: StepOutcome, state: dict) -> None:
    if outcome.scenario != state.get("scenario"):
        state["scenario"] = outcome.scenario
        print(paint(f"\n{outcome.scenario}", "bold"))
    symbol, color = {
        "ok": ("✔", "green"),
        "failed": ("✖", "red"),
        "skipped": ("–", "grey"),
        "cleanup": ("↺", "grey"),
        "leftover": ("!", "red"),
    }.get(outcome.state, ("·", "grey"))
    detail = f" — {outcome.detail}" if outcome.detail else ""
    if outcome.state in ("ok", "skipped", "cleanup"):
        detail = paint(detail, "grey")
    print(f"  {paint(symbol, color)} {outcome.step}{detail}")


def _reports_dir(session: Session) -> Path:
    out = getattr(session.args, "out", None)
    return Path(out) if out else session.catalog.reports_dir


# ----------------------------------------------------------------------
# check
# ----------------------------------------------------------------------


def _pick_scenarios(session: Session) -> list:
    catalog, args = session.catalog, session.args
    optional = [s for s in catalog.scenarios.values() if s.kind in ("write", "settings")]
    by_id = {s.id: s for s in optional}
    if args.all_writes:
        return [s for s in optional if s.kind == "write"]
    if args.scenario:
        chosen = []
        for sid in (x.strip() for x in args.scenario.split(",") if x.strip()):
            if sid not in by_id:
                raise SystemExit(f"No write/settings scenario {sid!r} (have: {', '.join(by_id) or 'none'})")
            chosen.append(by_id[sid])
        return chosen
    if not args.interactive or not optional:
        return []
    print()
    reads = catalog.scenarios_of_kind("read")
    print("Read-only checks always run" + (f" ({', '.join(s.title for s in reads)} + every other read endpoint)." if reads else "."))
    print("Optional scenarios — they change the org (use a lab org):")
    for i, s in enumerate(optional, 1):
        tag = paint(" [not undone]", "red") if s.kind == "settings" else ""
        print(f"  {i}. {paint(s.title, 'bold')} ({s.id}){tag}\n     {s.description}")
    answer = ask("Run which? (numbers or ids, 'all', or Enter for none)", "", required=False)
    if answer.lower() == "all":
        return list(optional)
    chosen = []
    for part in answer.replace(" ", "").split(","):
        if part.isdigit() and 1 <= int(part) <= len(optional):
            chosen.append(optional[int(part) - 1])
        elif part in by_id:
            chosen.append(by_id[part])
    return chosen


def run_check(session: Session) -> None:
    catalog, args = session.catalog, session.args
    scenarios = _pick_scenarios(session)
    inputs = parse_pairs(args.input)
    for scenario in scenarios:
        for name, spec in scenario.inputs.items():
            if not inputs.get(name):
                inputs[name] = ask(spec.get("prompt") or name, required=False)
    use_setups = not args.no_public
    if args.interactive and use_setups and any(api.get("setup") for api in catalog.apis.values()):
        use_setups = confirm("Include public-API checks (creates a temporary API key and deletes it again)?", True)

    transport = session.login()
    org = str(transport.vars.get("org_short_name", ""))
    if scenarios:
        print()
        print(paint(f"These scenarios change {org}:", "yellow"))
        for s in scenarios:
            print(f"  • {s.title}: {s.description}")
        typed = args.confirm or ask(f"Type the org short name ({org}) to confirm", required=False)
        if typed.lower() != org.lower():
            print("Not confirmed — running the read-only checks only.")
            scenarios = []

    state: dict = {}
    run = Run(catalog, transport, inputs=inputs, use_setups=use_setups, on_step=lambda o: _print_step(o, state))
    print(paint("\nRunning… (Ctrl-C stops after the current call; cleanup still runs)", "grey"))
    result = run.check(scenarios)
    report.print_summary(result)
    md, js = report.save(result, _reports_dir(session))
    print(f"\nReport: {md}\n        {js}")
    _offer_accept(session, result)


def _accept(catalog: Catalog, endpoint_id: str, observed: list) -> Path:
    endpoint = catalog.endpoint(endpoint_id)
    schema: dict = {}
    for body in observed:
        schema = shapes.merge(schema, shapes.infer(body))
    endpoint.schema = schema
    endpoint.example = shapes.sanitize(observed[-1])
    return catalog.save_endpoint(endpoint)


def _offer_accept(session: Session, result) -> None:
    candidates = [e for e in result.by_status(CHANGED) if e.observed and e.verdict and e.verdict.significant_diffs]
    if not candidates:
        return
    if session.args.accept:
        selected = candidates
    elif session.args.interactive:
        print()
        print(
            f"{len(candidates)} endpoint(s) answered successfully with a different shape. If the new shape is "
            "right, it can replace the stored schema and example (sanitised: no ids, names or emails)."
        )
        answer = ask("Update them? (a = all, s = one by one, n = no)", "n").lower()
        selected = candidates if answer.startswith("a") else [e for e in candidates if confirm(f"  Update {e.id}?")] if answer.startswith("s") else []
    else:
        selected = []
    for item in selected:
        path = _accept(session.catalog, item.id, item.observed)
        print(f"  Updated {item.id} ({_rel(session.catalog, path)})")


# ----------------------------------------------------------------------
# call
# ----------------------------------------------------------------------


def _fill_missing(endpoint: Endpoint, scope: dict, given: dict, catalog: Catalog) -> dict:
    """Prompt for variables the endpoint needs that aren't set yet."""
    values = dict(given)
    needed = endpoint.template_variables()
    for name in sorted(needed):
        if name in values or scope.get(name) not in (None, ""):
            continue
        spec = endpoint.params.get(name) or {}
        hint = spec.get("description") or ""
        example = spec.get("example")
        if example is None and spec.get("examples"):
            example = spec["examples"][0]
        label = name + (f" — {hint}" if hint else "")
        raw = ask(label, json.dumps(example) if isinstance(example, (dict, list)) else (str(example) if example is not None else None))
        try:
            values[name] = json.loads(raw)
        except ValueError:
            values[name] = raw
    return values


def run_call(session: Session, endpoint_id: str) -> None:
    catalog, args = session.catalog, session.args
    endpoint = catalog.endpoint(endpoint_id)
    print(paint(f"{endpoint.method} {endpoint.where}", "bold") + f"  ({endpoint.effect}) {endpoint.title}")
    if endpoint.warning:
        print(paint(f"Warning: {endpoint.warning}", "red"))
    transport = session.login()
    values = parse_pairs(getattr(args, "set", None))
    if args.interactive:
        values = _fill_missing(endpoint, transport.scope(), values, catalog)
    if not endpoint.is_read and not args.yes:
        org = transport.vars.get("org_short_name")
        if not args.interactive or not confirm(f"This is a {endpoint.effect} call on {org}. Send it?"):
            print("Not sent.")
            return
    state: dict = {}
    run = Run(catalog, transport, on_step=lambda o: _print_step(o, state))
    try:
        exchange = transport.send(endpoint, values)
    except (Skip, templating.Unresolved) as e:
        print(paint(f"Not sent: {e}", "red"))
        return
    finally:
        run.finish()
    verdict = judge(exchange, endpoint)
    color = {"ok": "green", "changed": "yellow", "failing": "red"}.get(verdict.status, "grey")
    print(paint(f"HTTP {exchange.status or '—'} · {verdict.status}: {verdict.reason}", color))
    for d in verdict.diffs[:12]:
        print(f"  {d}")
    show_json(exchange.response_json if exchange.response_json is not None else (exchange.response_text or None))
    offer = exchange.ok and exchange.response_json is not None and args.interactive
    if (
        offer
        and (endpoint.schema is None or verdict.status == CHANGED)
        and confirm("Store this response's shape as the endpoint's schema and example?", default=endpoint.schema is None)
    ):
        path = _accept(catalog, endpoint.id, [exchange.response_json])
        print(f"  Updated {_rel(catalog, path)}")


# ----------------------------------------------------------------------
# record
# ----------------------------------------------------------------------


def _provided(catalog: Catalog) -> set[str]:
    names = set(templating.SESSION_VARS) | set(catalog.defaults) | set((catalog.login or {}).get("save", {}))
    for api in catalog.apis.values():
        setup = catalog.scenarios.get(api.get("setup", ""))
        if setup:
            names |= {k for s in setup.steps for k in s.save}
    return names


def _save_new(session: Session, endpoint: Endpoint, response, status: int | None) -> None:
    catalog = session.catalog
    from endpoint_checker.har import suggest_id

    suggested = suggest_id(endpoint.method, endpoint.path)
    while True:
        endpoint.id = ask("Endpoint id (area.action)", suggested)
        if not catalog_mod.ID_RE.match(endpoint.id):
            print("  Use lowercase words separated by dots, e.g. door.unlock")
            continue
        if endpoint.id in catalog.endpoints and not confirm(f"  {endpoint.id} exists. Replace it?"):
            continue
        break
    endpoint.title = ask("Title (what it does)", endpoint.title or None, required=False)
    endpoint.effect = choose("Effect", list(catalog_mod.EFFECTS), endpoint.effect or EFFECT_FOR_METHOD.get(endpoint.method, "action"))
    endpoint.notes = ask("Notes (where you saw it, caveats)", endpoint.notes or "", required=False)
    endpoint.tags = [endpoint.area]
    params = sorted(endpoint.template_variables() - _provided(catalog))
    endpoint.params = {name: endpoint.params.get(name, {}) for name in params}
    if response is not None and status and 200 <= status < 300:
        endpoint.schema = shapes.infer(response)
        endpoint.example = shapes.sanitize(response)
    endpoint.file = None
    path = catalog.save_endpoint(endpoint)
    print(paint(f"Saved {endpoint.id} → {_rel(catalog, path)}", "green"))
    if endpoint.params:
        print(f"  Parameters: {', '.join(endpoint.params)} (describe them in the file if useful)")
    if endpoint.is_read:
        print("  Read endpoints are checked automatically on every run (when their parameters can be filled).")
    else:
        print(f"  Try it with `call {endpoint.id}`, or add it to a scenario to check it on every run.")


def record_manual(session: Session) -> None:
    catalog = session.catalog
    print(paint("\nRecord a new endpoint", "bold"))
    for name, api in catalog.apis.items():
        print(f"  {name}: {api.get('title', '')} — {api['base_url']}")
    api_name = choose("API", list(catalog.apis), next(iter(catalog.apis)))
    base = catalog.apis[api_name]["base_url"]
    host = None
    if "{{host}}" in base:
        hosts: dict[str, int] = {}
        for e in catalog.endpoints.values():
            if e.api == api_name and e.host:
                hosts[e.host] = hosts.get(e.host, 0) + 1
        common = ", ".join(h for h, _ in sorted(hosts.items(), key=lambda x: -x[1])[:8])
        host = ask(f"Host (known: {common})" if common else "Host")
    method = choose("Method", list(METHODS), "GET")
    path = ask("Path under the base URL ({{org_id}}, {{user_id}} … are filled in)").lstrip("/")
    query = parse_pairs([p for p in ask("Query (k=v&k2=v2, optional)", "", required=False).split("&") if p])
    body = ask_json("Request body") if method != "GET" else None
    endpoint = Endpoint(
        id="draft.endpoint",
        api=api_name,
        host=host,
        method=method,
        path=path,
        effect=EFFECT_FOR_METHOD.get(method, "action"),
        query=query,
        body=body,
        source={"from": "typed in"},
    )
    response, status = None, None
    default_send = method == "GET"
    if not default_send:
        print(paint("Sending a write request changes the org.", "yellow"))
    if confirm(f"Send {method} {path} now to capture the response?", default=default_send):
        transport = session.login()
        values = _fill_missing(endpoint, transport.scope(), {}, catalog)
        run = Run(catalog, transport)
        try:
            exchange = transport.send(endpoint, values, retries=0)
        except (Skip, templating.Unresolved) as e:
            print(paint(f"Not sent: {e}", "red"))
            return
        finally:
            run.finish()
        status = exchange.status
        response = exchange.response_json
        print(paint(f"  HTTP {status or '—'}", "green" if exchange.ok else "red"))
        show_json(response if response is not None else (exchange.response_text or exchange.error or None))
        if not exchange.ok and not confirm("The call didn't succeed. Record it anyway?"):
            return
    _save_new(session, endpoint, response, status)


def record_from_har(session: Session, har_path: str) -> None:
    catalog = session.catalog
    identity = {}
    if session.transport is not None:
        for var in ("org_id", "user_id", "email"):
            value = session.transport.vars.get(var)
            if value:
                identity[str(value)] = var
    candidates = har.candidates(catalog, har_path, extra_identity=identity)
    new = [c for c in candidates if not c.known]
    print(f"\n{len(candidates)} distinct call(s) to the catalog's APIs; {len(new)} not in the catalog yet.")
    pool = new
    if candidates and confirm("Show the known ones too?", default=not new):
        pool = candidates
    if not pool:
        return
    for i, c in enumerate(pool, 1):
        known = paint(f"  (= {', '.join(c.known)})", "grey") if c.known else ""
        print(f"  {i:3}. {c.method:6} {c.where} → {c.status}{known}")
    answer = ask("Record which? (numbers, comma-separated; Enter for none)", "", required=False)
    picks = [int(p) for p in answer.replace(" ", "").split(",") if p.isdigit() and 1 <= int(p) <= len(pool)]
    for index in picks:
        c = pool[index - 1]
        print(paint(f"\n{c.method} {c.where}", "bold"))
        if c.query:
            print(f"  Query: {c.query}")
        print("  Request body (as captured, your org/user ids replaced):")
        show_json(c.body, max_lines=25)
        print(f"  Response (HTTP {c.status}, shown sanitised):")
        show_json(shapes.sanitize(c.response) if c.response is not None else None, max_lines=25)
        path = ask("Path template (replace object ids with {{name}})", c.path)
        endpoint = Endpoint(
            id="draft.endpoint",
            api=c.api,
            host=c.host,
            method=c.method,
            path=path,
            effect=EFFECT_FOR_METHOD.get(c.method, "action"),
            query=c.query,
            body=c.body,
            params={name: {"description": "id seen in the HAR"} for name in c.params},
            source={"from": "HAR", "file": os.path.basename(har_path)},
        )
        print(paint("  Literal values in the body are kept as captured — replace them with {{variables}} in the file if they vary.", "grey"))
        _save_new(session, endpoint, c.response, c.status)


def run_record(session: Session) -> None:
    if session.args.har:
        record_from_har(session, session.args.har)
        return
    source = choose("Record from (1) typing it in or (2) a browser HAR file", ["1", "2"], "1")
    if source == "2":
        record_from_har(session, ask("Path to the .har file"))
    else:
        record_manual(session)


# ----------------------------------------------------------------------
# analyze / list / show / validate / export / init
# ----------------------------------------------------------------------


def run_analyze(session: Session, path: str) -> None:
    exchanges = har.exchanges(path)
    if not exchanges:
        raise SystemExit("No requests found in that HAR file.")
    result = analyze(session.catalog, exchanges, source=os.path.basename(path))
    report.print_summary(result)
    md, js = report.save(result, _reports_dir(session))
    print(f"\nReport: {md}\n        {js}")


def run_list(catalog: Catalog, args) -> None:
    items = sorted(catalog.endpoints.values(), key=lambda e: e.id)
    if getattr(args, "effect", None):
        items = [e for e in items if e.effect == args.effect]
    if getattr(args, "search", None):
        needle = args.search.lower()
        items = [e for e in items if needle in f"{e.id} {e.title} {e.path} {e.host or ''}".lower()]
    for e in items:
        print(f"  {e.id:44} {e.method:6} {e.effect:7} {e.where}")
        if e.title:
            print(paint(f"  {'':44} {e.title}", "grey"))
    print(f"\n{len(items)} endpoint(s) · {len(catalog.scenarios)} scenario(s) in {catalog.root}")


def run_show(catalog: Catalog, endpoint_id: str) -> None:
    endpoint = catalog.endpoint(endpoint_id)
    print(paint(f"{endpoint.id} — {endpoint.title}", "bold"))
    print(f"  {endpoint.method} {endpoint.where}  ({endpoint.effect}, api {endpoint.api})")
    print(f"  file: {_rel(catalog, endpoint.file)}")
    users = catalog.scenarios_calling(endpoint.id)
    if users:
        print(f"  used by scenarios: {', '.join(s.id for s in users)}")
    show_json(endpoint.to_dict(), max_lines=200, indent="  ")


def run_validate(catalog: Catalog) -> int:
    errors, warnings = catalog_mod.validate(catalog)
    for w in warnings:
        print(paint(f"  warning: {w}", "yellow"))
    for e in errors:
        print(paint(f"  error: {e}", "red"))
    print(f"{len(catalog.endpoints)} endpoints, {len(catalog.scenarios)} scenarios: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


def run_export(catalog: Catalog, args) -> None:
    from endpoint_checker import openapi

    doc = openapi.export(catalog)
    target = args.output or str(catalog.root / "openapi.json")
    if target == "-":
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return
    Path(target).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {target} ({sum(len(v) for v in doc['paths'].values())} operations)")


def _rel(catalog: Catalog, path: Path | None) -> str:
    if path is None:
        return "?"
    try:
        return str(Path(path).resolve().relative_to(catalog.root))
    except ValueError:
        return str(path)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="endpoint-checker",
        description="Check Verkada endpoints against a catalog for changes, and record new ones.",
    )
    parser.add_argument("--catalog", help="catalog directory (default: $ENDPOINT_CATALOG, the current directory, or the bundled one)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", help="directory for reports (default: <catalog>/reports)")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", parents=[common], help="log in and check endpoints")
    check.add_argument("--scenario", help="write/settings scenarios to run, comma-separated")
    check.add_argument("--all-writes", action="store_true", help="run every 'write' scenario (not 'settings')")
    check.add_argument("--input", action="append", metavar="NAME=VALUE", help="scenario input, e.g. invite_email=a@b.c")
    check.add_argument("--no-public", action="store_true", help="skip APIs that need a setup (public API: no temporary key)")
    check.add_argument("--accept", action="store_true", help="store changed response shapes as the new schema/example")
    check.add_argument("--confirm", metavar="ORG", help="org short name, to run write scenarios without the prompt")

    call = sub.add_parser("call", help="send one endpoint and show the response")
    call.add_argument("endpoint")
    call.add_argument("--set", action="append", metavar="NAME=VALUE", help="variable value (repeatable)")
    call.add_argument("--yes", action="store_true", help="don't ask before sending a non-read call")

    record = sub.add_parser("record", help="add a new endpoint (method, payload, response)")
    record.add_argument("--har", help="import from a browser HAR file")

    analyze_p = sub.add_parser("analyze", parents=[common], help="judge a browser HAR against the catalog (no login)")
    analyze_p.add_argument("file")

    list_p = sub.add_parser("list", help="list endpoints (no login)")
    list_p.add_argument("--effect", choices=catalog_mod.EFFECTS)
    list_p.add_argument("--search")
    show = sub.add_parser("show", help="show one endpoint (no login)")
    show.add_argument("endpoint")
    sub.add_parser("validate", help="check the catalog files (no login)")
    export = sub.add_parser("export", help="export the catalog (no login)")
    export.add_argument("format", choices=["openapi"])
    export.add_argument("-o", "--output", help="file to write ('-' for stdout; default <catalog>/openapi.json)")
    init = sub.add_parser("init", help="create a new catalog directory")
    init.add_argument("directory")
    init.add_argument("--empty", action="store_true", help="only the login/API-key endpoints, no Verkada starter set")
    return parser


def menu(session: Session) -> None:
    actions = [
        ("Check endpoints (read-only, optional write scenarios)", lambda: run_check(session)),
        ("Call one endpoint", lambda: run_call(session, _pick_endpoint(session.catalog))),
        ("Record a new endpoint (typed in or from a HAR)", lambda: run_record(session)),
        ("Analyze a browser HAR file", lambda: run_analyze(session, ask("Path to the .har file"))),
        ("List endpoints", lambda: run_list(session.catalog, argparse.Namespace(effect=None, search=ask("Filter (optional)", "", required=False)))),
        ("Validate the catalog", lambda: run_validate(session.catalog)),
    ]
    while True:
        print(paint(f"\n{session.catalog.name} — {len(session.catalog.endpoints)} endpoints ({session.catalog.root})", "bold"))
        for i, (label, _) in enumerate(actions, 1):
            print(f"  {i}. {label}")
        print("  q. Quit")
        choice = ask("Choice", "1").lower()
        if choice in ("q", "quit", "exit"):
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(actions):
            continue
        try:
            actions[int(choice) - 1][1]()
        except KeyboardInterrupt:
            print("\nCancelled.")
        except (CatalogError, OSError, ValueError) as e:
            print(paint(f"Error: {e}", "red"))
        except SystemExit as e:
            if e.code not in (0, None):
                print(paint(f"{e.code}", "red"))


def _pick_endpoint(catalog: Catalog) -> str:
    while True:
        text = ask("Endpoint id (or part of it)")
        if text in catalog.endpoints:
            return text
        matches = [e for e in sorted(catalog.endpoints) if text.lower() in e]
        if len(matches) == 1:
            return matches[0]
        for m in matches[:20]:
            print(f"  {m}")
        if not matches:
            print("  no match")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name, default in (
        ("scenario", None),
        ("all_writes", False),
        ("input", None),
        ("no_public", False),
        ("accept", False),
        ("har", None),
        ("yes", False),
        ("set", None),
        ("out", None),
        ("confirm", None),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    args.interactive = sys.stdin.isatty()

    if args.command == "init":
        target = catalog_mod.init(args.directory, empty=args.empty)
        print(f"Created a catalog in {target}")
        print(f"  git init {target}   # to version it on its own")
        print(f"  endpoint-checker --catalog {target} validate")
        return 0

    root = catalog_mod.find(args.catalog, os.environ.get("ENDPOINT_CATALOG"))
    try:
        catalog = catalog_mod.load(root)
    except CatalogError as e:
        raise SystemExit(str(e)) from None
    session = Session(catalog, args)
    try:
        if args.command == "check":
            run_check(session)
        elif args.command == "call":
            run_call(session, args.endpoint)
        elif args.command == "record":
            run_record(session)
        elif args.command == "analyze":
            run_analyze(session, args.file)
        elif args.command == "list":
            run_list(catalog, args)
        elif args.command == "show":
            run_show(catalog, args.endpoint)
        elif args.command == "validate":
            return run_validate(catalog)
        elif args.command == "export":
            run_export(catalog, args)
        else:
            menu(session)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except CatalogError as e:
        raise SystemExit(str(e)) from None
    return 0
