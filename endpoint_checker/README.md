# endpoint-checker

A small command-line helper for Verkada Command's endpoints. It is separate from vCommander: it imports nothing from vCommander and vCommander doesn't use it.

- **Check:** log into Verkada, make the same calls vCommander makes, and see which endpoints *might have changed*: moved routes, rejected payloads, missing fields, changed types.
- **Record:** log new endpoints (method, URL, payload, response) as you find them, typed in or imported from your browser's network log.
- **Keep a repository of endpoints:** everything lives in an [endpoint catalog](../catalog/README.md), a folder of JSON files (one per endpoint) that can be its own git repository. It exports to OpenAPI for Swagger UI, Postman, Insomnia or Bruno.

## Install

Python 3.10+.

```bash
cd endpoint-checker
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .                                       # installs the `endpoint-checker` command
endpoint-checker                                       # or: python -m endpoint_checker
```

The only runtime dependency is `requests`. For development: `pip install -e ".[dev]"`, then `pytest` and `ruff check .`.

## Your own endpoint repository

The tool ships with a starter catalog (`catalog/`): 159 endpoints and the check scenarios, generated from what vCommander does today. To keep your own copy under version control:

```bash
endpoint-checker init ~/verkada-endpoints          # copy of the starter (or --empty: just login + API key)
cd ~/verkada-endpoints && git init && git add . && git commit -m "Endpoint catalog"
endpoint-checker                                   # run inside the folder: it's picked up automatically
```

The catalog is found from `--catalog DIR`, then `$ENDPOINT_CATALOG`, then the current directory (or a parent) containing `catalog.json`, and finally the bundled starter. The file format is documented in [catalog/README.md](../catalog/README.md), which `init` copies into the new repository.

## Commands

| Command | Login | What it does |
|---|---|---|
| `endpoint-checker` | when needed | Menu |
| `check [--scenario sites,groups] [--all-writes] [--input NAME=VALUE] [--confirm ORG] [--no-public] [--accept] [--out DIR]` | yes | Run the checks; reports go to `<catalog>/reports/`. `--confirm` skips the type-the-org prompt for scripts |
| `call ID [--set NAME=VALUE] [--yes]` | yes | Send one endpoint, show the response and how it compares |
| `record [--har FILE]` | if you send | Add a new endpoint, typed in or from a HAR |
| `analyze FILE.har [--out DIR]` | no | Judge recorded browser traffic against the catalog |
| `list [--search TEXT] [--effect read]` · `show ID` | no | Browse the catalog |
| `validate` | no | Check the catalog files (also checked by the JSON Schemas in your editor) |
| `export openapi [-o FILE]` | no | Write an OpenAPI 3.1 document |
| `init DIR [--empty]` | no | Start a new catalog |

Login asks for email, org short name, region (`api`, `api.eu`, `api.au`), shard and password, then a 2FA code if needed (`r` resends the SMS). The password is never stored. Email, org, region and shard are remembered in `~/.config/endpoint-checker/profile.json`.

## Checking endpoints

A check runs, in order:

1. **Read scenarios** (always): calls that need ids from other calls, e.g. the org's first alarm system, then every alarm device list for it.
2. **Every other read endpoint** whose variables can be filled. Endpoints you record are included automatically.
3. **Write scenarios you pick** (opt-in, for a lab org). You confirm by typing the org short name:

   | Scenario | What it does |
   |---|---|
   | `sites` | Creates a site and a building with one floor, checks they're listed, renames the site, deletes them |
   | `groups` | Security group plus internal and public access groups; adds you to each, checks they're listed, deletes them |
   | `alarms` | Test site, alarm site (starts the alarm trial for that site), self-monitoring, alarm system with keycode, partition; deletes them |
   | `workplace` | Test site, guest site (starts the Guest trial), mailroom site; deletes them |
   | `users` | Invites a throwaway address you give, checks it's listed, deletes the user |
   | `org-settings` | Org-wide settings as vCommander's Commission/Decommission change them. **Not undone.** |

Public-API calls need a token. The first one creates a temporary API key (valid for 1 hour) and deletes it at the end of the run; `--no-public` skips them.

Created objects are named `Endpoint check <timestamp>`. Cleanup runs last-in-first-out after each scenario, even when a step fails or you press Ctrl-C. Anything that couldn't be removed is listed under **Cleanup problems**.

Every response is judged against the catalog:

| Result | Meaning |
|---|---|
| ✔ ok | Normal status, and the response still has the fields and types in `response.schema` |
| ⚠ might have changed | A success whose shape lost a field or changed a type, an unexpected status, a value a scenario needed that wasn't there, or "created but not listed" |
| ✖ failing | 404/405/410 (route or method moved) or 400/409/415/422 (payload no longer accepted) |
| ? inconclusive | 401/403, 429 or 5xx: permissions or server trouble, not evidence of a change |
| – not exercised | With the reason: needs a value, in a scenario you didn't run, not a read endpoint, … |

New fields are listed for information. Calls that match no catalog endpoint are listed too; they're often new endpoints worth recording. When a shape changed and the new one is right, the checker can store it as the endpoint's new schema and example (`--accept` does that for all of them).

Endpoints that need real devices (cameras, controllers, alarm hardware) or that delete existing data are never called by a check. To check them, use `call` on a lab org, or do the action in Command with DevTools open and `analyze` the HAR.

## Recording new endpoints

**From your browser (recommended).** Open Command, open DevTools → Network, do the action, then right-click the request list → *Save all as HAR with content*. Then:

```bash
endpoint-checker record --har ~/Downloads/command.har
```

It lists the calls to the catalog's APIs that aren't in the catalog yet, with their payload and response. Your org and user ids become `{{org_id}}` and `{{user_id}}`, and ids in the path become `{{door_id}}`-style parameters. You pick the calls to keep, name them (`door.unlock`), set the effect, and they're saved as endpoint files with a schema and a sanitised example. Literal values in a captured body are kept as they were; replace any that vary with `{{variables}}` in the file.

> HAR files contain your session cookies and tokens. Don't commit or share them.

**Typed in.** `record` asks for the API, host, method, path, query and JSON body, and can send the call once to capture the response. It asks before sending anything but a GET.

## Developing

```bash
pip install -e ".[dev]"
pytest              # runs against a fake Verkada built from the catalog's own examples
ruff check .
```

The code lives in `endpoint_checker/`: `catalog.py` (format, validation, init), `templating.py` (variables and response paths), `shapes.py` (schemas), `judge.py` (verdicts), `http.py` (requests, retries, redaction), `verkada.py` (login), `runner.py` (scenarios, cleanup, read checks), `har.py`, `openapi.py`, `report.py`, `cli.py`.
