# Endpoint catalog

A catalog of Verkada endpoints: how to call each one (method, URL, headers, body) and what it answers (status, response shape, example). It is plain JSON, one file per endpoint, so it diffs well in git and can live in its own repository.

The `endpoint-checker` command-line tool reads it to check a Verkada org for endpoints that changed, and writes to it when you record new endpoints. Editors that understand JSON Schema (VS Code, JetBrains) validate and autocomplete the files through their `$schema` line.

```
catalog.json                   APIs (base URL, auth headers/cookies), login, default values
endpoints/<area>/<id>.json     one endpoint each
scenarios/<id>.json            ordered calls: read checks, create → verify → delete flows
schema/*.schema.json           JSON Schemas for the three file types
reports/                       check reports (git-ignored)
```

## Endpoint files

```jsonc
{
  "$schema": "../../schema/endpoint.schema.json",
  "id": "site.create",                    // unique, "area.action"; file: endpoints/site/site.create.json
  "title": "Create a site",
  "api": "command",                        // a key of catalog.json "apis"
  "host": "vprovision",                    // fills {{host}} in the API's base URL
  "method": "POST",
  "path": "org/camera_group/create",       // under the base URL; may contain {{variables}}
  "effect": "create",                      // read | create | update | delete | action
  "params": {"site_name": {"description": "Name of the new site"}},
  "request": {
    "query": {},                           // optional
    "headers": {},                         // optional, added to the API's headers
    "body": {"organizationId": "{{org_id}}", "name": "{{site_name}}"}
  },
  "response": {
    "status": ["2xx"],                     // normal answers, e.g. ["2xx", 404]
    "schema": { ... },                     // JSON Schema of the body, inferred from real responses
    "example": {"cameraGroups": [{"cameraGroupId": "<site_id>"}]}   // sanitised
  },
  "tags": ["site"],
  "notes": "",
  "source": {"from": "HAR", "file": "session.har"},
  "updated": "2026-10-23"
}
```

Optional fields: `description`, `auth: "none"` (calls made before login or with their own credentials, e.g. the token exchange), `autocheck: false` (never call this read endpoint automatically), `warning` (shown before calling it by hand).

**Only `read` endpoints are ever called automatically.** Everything else runs only inside a scenario you choose, or when you call it by hand with `call`.

### Response schemas

`response.schema` is a subset of JSON Schema: `type` (one or several), `properties` + `required`, `items` for arrays, `additionalProperties` for maps keyed by ids (`{"<group_id>": {...}}`), and `{}` for "unknown". Comparing a live response:

- a **missing** required field or a **type change** means the endpoint *might have changed*;
- **new** fields are listed for information only;
- in a list of objects a field is missing only if *no* item has it (optional fields are normal);
- `null` is always accepted.

Example values like `"<site_id>"` stand for "any scalar". Examples are sanitised when stored: strings become `<field>` placeholders (ALL_CAPS enum values are kept), lists keep one element, floats become `0.0`.

## Variables

Any string can contain `{{name}}`. A string that is exactly one variable keeps the value's type (lists stay lists); `{{name|default}}` falls back to `default`.

| Source | Variables |
|---|---|
| Login | `email`, `org_short_name`, `region`, `shard`, and what `catalog.json` → `login.save` extracts (`org_id`, `user_id`, `csrf_token`, `user_token`) |
| `catalog.json` → `defaults` | e.g. the lab address used when creating sites |
| API setup scenarios | e.g. `public_token` from `public-api-access` |
| Scenario `vars`, `inputs`, `save` | per scenario |
| Step `with` | per call (the endpoint's `params`) |
| Generated | `{{$stamp}}` (run timestamp), `{{$now}}`, `{{$now+3600}}`, `{{$now_ms}}`, `{{$uuid}}`, `{{$digits:6}}` |

A call whose variables can't all be filled is skipped and reported as "needs {{name}}", never sent half-filled.

Endpoints that act on another user take `{{target_user_id}}`, never the session's `{{user_id}}`, so they can't default to your own account.

## Response paths

Used by `save` and `expect` in scenarios, and by `login.save`:

| Path | Selects |
|---|---|
| `cameraGroups[0].cameraGroupId` | the first group's id |
| `sites[*].siteId` | every site id (a list) |
| `apiKeys[?apiKeyName=={{api_key_name}}].apiKeyId` | filter a list by a field (`!=` too) |
| `[*].buildingId` | items of a top-level list |
| `children.{{group_id}}` | a key of a map |
| `sites[0].siteId?` | trailing `?`: optional (no error when absent) |

## Scenarios

```jsonc
{
  "$schema": "../schema/scenario.schema.json",
  "id": "sites",
  "title": "Sites & buildings",
  "kind": "write",                     // read | write | settings | setup
  "description": "Creates a site …",  // shown before it runs
  "inputs": {"invite_email": {"prompt": "Throwaway email"}},   // asked before running
  "vars": {"name": "Endpoint check {{$stamp}}"},
  "steps": [
    {
      "name": "Create a site",
      "call": "site.create",
      "with": {"site_name": "{{name}}"},
      "save": {"site_id": "cameraGroups[0].cameraGroupId"},
      "cleanup": {
        "name": "Delete the site",
        "call": "site.delete",
        "otherwise": {"name": "Rename it instead", "call": "site.rename", "with": {"new_name": "{{name}} (delete me)"}}
      }
    },
    {"name": "Find the site", "call": "site.list", "expect": {"contains": {"sites[*].siteId": "{{site_id}}"}}}
  ]
}
```

- **Kinds.** `read` scenarios always run and may only call read endpoints. `write` scenarios are opt-in and clean up after themselves. `settings` scenarios are opt-in and their changes are *not* undone. A `setup` scenario runs when an API first needs it (`catalog.json` → `apis.<name>.setup`); its saved variables are available to every call, and its cleanups run at the end of the run.
- **Steps** run in order; a failed step doesn't stop the scenario. Later steps that need a variable it didn't save are skipped.
- **`save`** stores response values; a required path that matches nothing marks the endpoint *might have changed*.
- **`expect`**: `exists: [paths]`, `contains: {path: value}`, `equals: {path: value}`. A failed expectation marks the endpoint *might have changed* (e.g. "created, but not listed").
- **`cleanup`** is registered as soon as the step's call succeeds and runs at the end of the scenario, last created first removed, even if a later step fails or the run is interrupted. A cleanup step can have nested `cleanup` steps (a chain, e.g. find the id, then delete) and an `otherwise` fallback. If a cleanup fails, the object is reported under *Cleanup problems*, even when its fallback ran.

## catalog.json

```jsonc
{
  "format": "endpoint-catalog/1",
  "name": "Verkada Command",
  "apis": {
    "command": {
      "base_url": "https://{{host}}.command.verkada.com/__v/{{org_short_name}}/",
      "headers": {"x-verkada-token": "{{csrf_token}}", ...},
      "cookies": {"auth": "{{user_token}}", ...}
    },
    "public": {
      "base_url": "https://{{region}}.verkada.com/",
      "headers": {"x-verkada-auth": "{{public_token}}"},
      "setup": "public-api-access"      // creates a temporary API key + token, deletes the key afterwards
    }
  },
  "login": {
    "endpoint": "auth.login",
    "success": {"path": "loggedIn", "equals": true},
    "save": {"csrf_token": "csrfToken", "user_token": "userToken", "org_id": "organizationId", "user_id": "userId"},
    "mfa": {"when_message_contains": "2FA invalid", "otp_field": "otp", "sms_enabled": "data.smsEnabled?",
            "sms_endpoint": "auth.twofactor.sms.new", "sms_contact": "smsSent?"}
  },
  "defaults": {"region": "api", "shard": "prod1", "address_label": "…"}
}
```

If Verkada renames a login field, change `login.save`. If a header changes, change `apis.*.headers`. Neither needs a code change.

## Where the starter content came from

The starter set was generated from vCommander's API client: each request is exactly what vCommander sends, with values replaced by `{{variables}}`. Responses were inferred from vCommander's recorded examples. `source.methods` names the vCommander method behind each endpoint. Endpoints that send an identical request (e.g. the alarm device lists) say so in `notes`; their schemas only require the fields they all share.
