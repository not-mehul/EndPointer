"""The endpoint catalog: a directory of JSON files, one per endpoint.

    <catalog>/
      catalog.json                 APIs (base URL, auth headers), login, defaults
      endpoints/<area>/<id>.json   one endpoint each
      scenarios/<id>.json          ordered calls: read checks, create→verify→delete flows
      schema/*.schema.json         JSON Schemas for the three file types (editor validation)
      reports/                     check reports (git-ignored)

The format is described in README.md (and enforced by `validate`).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from endpoint_checker import templating

FORMAT = "endpoint-catalog/1"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
EFFECTS = ("read", "create", "update", "delete", "action")
KINDS = ("read", "write", "settings", "setup")
ID_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_\-]+)+$")
STARTER = Path(__file__).resolve().parents[1] / "catalog"


class CatalogError(Exception):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise CatalogError(f"{path}: invalid JSON ({e})") from e


def dumps(value: Any, indent: int = 0, width: int = 88) -> str:
    """JSON with 2-space indents; short lists of scalars stay on one line (readable diffs)."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f'{pad}  {json.dumps(k, ensure_ascii=False)}: {dumps(v, indent + 1, width)}' for k, v in value.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(not isinstance(v, (dict, list)) for v in value):
            inline = "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
            if len(inline) + len(pad) <= width:
                return inline
        items = [f"{pad}  {dumps(v, indent + 1, width)}" for v in value]
        return "[\n" + ",\n".join(items) + f"\n{pad}]"
    return json.dumps(value, ensure_ascii=False)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(data) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@dataclass
class Endpoint:
    id: str
    api: str
    method: str
    path: str
    effect: str
    title: str = ""
    description: str = ""
    host: str | None = None
    auth: str | None = None  # "none" for pre-auth calls (login, token exchange)
    autocheck: bool | None = None  # read endpoints are auto-checked unless False
    warning: str = ""
    params: dict = field(default_factory=dict)
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    body: Any = None
    status: list = field(default_factory=lambda: ["2xx"])
    schema: dict | None = None
    example: Any = None
    tags: list = field(default_factory=list)
    notes: str = ""
    source: dict = field(default_factory=dict)
    updated: str = ""
    file: Path | None = None

    @property
    def area(self) -> str:
        return self.id.split(".", 1)[0]

    @property
    def is_read(self) -> bool:
        return self.effect == "read"

    @property
    def where(self) -> str:
        return f"{self.host} · {self.path}" if self.host else self.path

    def status_ok(self, status: int) -> bool:
        for spec in self.status:
            if isinstance(spec, int) and spec == status:
                return True
            if isinstance(spec, str) and len(spec) == 3 and spec.endswith("xx") and str(status)[0] == spec[0]:
                return True
        return False

    def template_variables(self) -> set[str]:
        return templating.variables([self.path, self.query, self.headers, self.body])

    @classmethod
    def from_dict(cls, data: dict, file: Path | None = None) -> Endpoint:
        request = data.get("request") or {}
        response = data.get("response") or {}
        return cls(
            id=data["id"],
            api=data["api"],
            method=data["method"].upper(),
            path=data["path"],
            effect=data["effect"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            host=data.get("host"),
            auth=data.get("auth"),
            autocheck=data.get("autocheck"),
            warning=data.get("warning", ""),
            params=data.get("params") or {},
            query=request.get("query") or {},
            headers=request.get("headers") or {},
            body=request.get("body"),
            status=response.get("status") or ["2xx"],
            schema=response.get("schema"),
            example=response.get("example"),
            tags=data.get("tags") or [],
            notes=data.get("notes", ""),
            source=data.get("source") or {},
            updated=data.get("updated", ""),
            file=file,
        )

    def to_dict(self) -> dict:
        out: dict = {"$schema": "../../schema/endpoint.schema.json", "id": self.id}
        if self.title:
            out["title"] = self.title
        if self.description:
            out["description"] = self.description
        out["api"] = self.api
        if self.host:
            out["host"] = self.host
        out["method"] = self.method
        out["path"] = self.path
        out["effect"] = self.effect
        if self.auth:
            out["auth"] = self.auth
        if self.autocheck is not None:
            out["autocheck"] = self.autocheck
        if self.warning:
            out["warning"] = self.warning
        if self.params:
            out["params"] = self.params
        request: dict = {}
        if self.query:
            request["query"] = self.query
        if self.headers:
            request["headers"] = self.headers
        if self.body is not None:
            request["body"] = self.body
        if request:
            out["request"] = request
        response: dict = {"status": self.status}
        if self.schema is not None:
            response["schema"] = self.schema
        if self.example is not None:
            response["example"] = self.example
        out["response"] = response
        if self.tags:
            out["tags"] = self.tags
        if self.notes:
            out["notes"] = self.notes
        if self.source:
            out["source"] = self.source
        if self.updated:
            out["updated"] = self.updated
        return out


# ----------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------


@dataclass
class Step:
    name: str
    call: str
    with_: dict = field(default_factory=dict)
    save: dict = field(default_factory=dict)
    expect: dict = field(default_factory=dict)
    cleanup: list[Step] = field(default_factory=list)
    otherwise: Step | None = None  # cleanup steps: what to try if this one fails

    @classmethod
    def from_dict(cls, data: dict) -> Step:
        cleanup = data.get("cleanup") or []
        if isinstance(cleanup, dict):
            cleanup = [cleanup]
        return cls(
            name=data.get("name") or data["call"],
            call=data["call"],
            with_=data.get("with") or {},
            save=data.get("save") or {},
            expect=data.get("expect") or {},
            cleanup=[cls.from_dict(c) for c in cleanup],
            otherwise=cls.from_dict(data["otherwise"]) if data.get("otherwise") else None,
        )

    def calls(self) -> list[str]:
        out = [self.call]
        for c in self.cleanup:
            out += c.calls()
        if self.otherwise:
            out += self.otherwise.calls()
        return out


@dataclass
class Scenario:
    id: str
    title: str
    kind: str
    description: str = ""
    inputs: dict = field(default_factory=dict)  # name -> {"prompt": ..., "example": ...}
    vars: dict = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    file: Path | None = None

    @classmethod
    def from_dict(cls, data: dict, file: Path | None = None) -> Scenario:
        return cls(
            id=data["id"],
            title=data.get("title") or data["id"],
            kind=data["kind"],
            description=data.get("description", ""),
            inputs=data.get("inputs") or {},
            vars=data.get("vars") or {},
            steps=[Step.from_dict(s) for s in data.get("steps") or []],
            file=file,
        )

    def calls(self) -> set[str]:
        return {c for s in self.steps for c in s.calls()}


# ----------------------------------------------------------------------
# Catalog
# ----------------------------------------------------------------------


@dataclass
class Catalog:
    root: Path
    name: str
    apis: dict
    login: dict
    defaults: dict
    endpoints: dict[str, Endpoint]
    scenarios: dict[str, Scenario]
    description: str = ""

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    def endpoint(self, endpoint_id: str) -> Endpoint:
        try:
            return self.endpoints[endpoint_id]
        except KeyError:
            raise CatalogError(f"no endpoint {endpoint_id!r} in {self.root}") from None

    def endpoint_path(self, endpoint_id: str) -> Path:
        return self.root / "endpoints" / endpoint_id.split(".", 1)[0] / f"{endpoint_id}.json"

    def save_endpoint(self, endpoint: Endpoint, *, touch: bool = True) -> Path:
        if not ID_RE.match(endpoint.id):
            raise CatalogError(f"endpoint id {endpoint.id!r} should look like 'area.action' (lowercase, dots)")
        if touch:
            endpoint.updated = date.today().isoformat()
        path = endpoint.file or self.endpoint_path(endpoint.id)
        write_json(path, endpoint.to_dict())
        endpoint.file = path
        self.endpoints[endpoint.id] = endpoint
        return path

    def scenarios_of_kind(self, *kinds: str) -> list[Scenario]:
        return [s for s in self.scenarios.values() if s.kind in kinds]

    def scenarios_calling(self, endpoint_id: str) -> list[Scenario]:
        return [s for s in self.scenarios.values() if endpoint_id in s.calls()]


def load(root: str | Path) -> Catalog:
    root = Path(root).resolve()
    meta_path = root / "catalog.json"
    if not meta_path.exists():
        raise CatalogError(f"{root} is not an endpoint catalog (no catalog.json). Create one with `init`.")
    meta = read_json(meta_path)
    if meta.get("format") != FORMAT:
        raise CatalogError(f"{meta_path}: unsupported format {meta.get('format')!r} (expected {FORMAT!r})")
    endpoints: dict[str, Endpoint] = {}
    for path in sorted((root / "endpoints").rglob("*.json")):
        data = read_json(path)
        try:
            endpoint = Endpoint.from_dict(data, path)
        except KeyError as e:
            raise CatalogError(f"{path}: missing field {e}") from None
        if endpoint.id in endpoints:
            raise CatalogError(f"{path}: duplicate endpoint id {endpoint.id!r} (also in {endpoints[endpoint.id].file})")
        endpoints[endpoint.id] = endpoint
    scenarios: dict[str, Scenario] = {}
    for path in sorted((root / "scenarios").glob("*.json")):
        data = read_json(path)
        try:
            scenario = Scenario.from_dict(data, path)
        except KeyError as e:
            raise CatalogError(f"{path}: missing field {e}") from None
        scenarios[scenario.id] = scenario
    return Catalog(
        root=root,
        name=meta.get("name", root.name),
        description=meta.get("description", ""),
        apis=meta.get("apis") or {},
        login=meta.get("login") or {},
        defaults=meta.get("defaults") or {},
        endpoints=endpoints,
        scenarios=scenarios,
    )


def find(explicit: str | None = None, env: str | None = None, cwd: Path | None = None) -> Path:
    """Locate the catalog: --catalog, $ENDPOINT_CATALOG, the current directory
    (or a parent), else the starter catalog shipped with the tool."""
    for candidate in (explicit, env):
        if candidate:
            return Path(candidate).expanduser()
    here = (cwd or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if (directory / "catalog.json").exists():
            return directory
    return STARTER


# ----------------------------------------------------------------------
# init
# ----------------------------------------------------------------------

GITIGNORE = "reports/\n"


def init(target: str | Path, *, empty: bool = False) -> Path:
    """Create a new catalog at `target` (a copy of the starter, or empty)."""
    target = Path(target).expanduser().resolve()
    if (target / "catalog.json").exists():
        raise CatalogError(f"{target} already has a catalog.json")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(STARTER / "schema", target / "schema", dirs_exist_ok=True)
    if empty:
        meta = read_json(STARTER / "catalog.json")
        meta["name"] = target.name
        write_json(target / "catalog.json", meta)
        (target / "endpoints").mkdir(exist_ok=True)
        (target / "scenarios").mkdir(exist_ok=True)
        for name in ("public-api-access.json",):
            shutil.copy(STARTER / "scenarios" / name, target / "scenarios" / name)
        for endpoint_id in _auth_endpoint_ids(meta):
            src = STARTER / "endpoints" / endpoint_id.split(".", 1)[0] / f"{endpoint_id}.json"
            if src.exists():
                dst = target / "endpoints" / src.parent.name / src.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dst)
    else:
        shutil.copy(STARTER / "catalog.json", target / "catalog.json")
        shutil.copytree(STARTER / "endpoints", target / "endpoints", dirs_exist_ok=True)
        shutil.copytree(STARTER / "scenarios", target / "scenarios", dirs_exist_ok=True)
    if (STARTER / "README.md").exists():
        shutil.copy(STARTER / "README.md", target / "README.md")
    (target / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    return target


def _auth_endpoint_ids(meta: dict) -> list[str]:
    """Endpoints the login and public-API setup need (kept in an empty catalog)."""
    login = meta.get("login") or {}
    ids = [login.get("endpoint"), (login.get("mfa") or {}).get("sms_endpoint")]
    setup = read_json(STARTER / "scenarios" / "public-api-access.json")
    ids += list(Scenario.from_dict(setup).calls())
    return [i for i in ids if i]


# ----------------------------------------------------------------------
# validate
# ----------------------------------------------------------------------


def validate(catalog: Catalog) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the whole catalog."""
    errors: list[str] = []
    warnings: list[str] = []
    provided = set(templating.SESSION_VARS) | set(catalog.defaults)
    provided |= set((catalog.login or {}).get("save", {}))
    for api_name, api in catalog.apis.items():
        if "base_url" not in api:
            errors.append(f"catalog.json: api {api_name!r} has no base_url")
        setup = api.get("setup")
        if setup:
            scenario = catalog.scenarios.get(setup)
            if scenario is None:
                errors.append(f"catalog.json: api {api_name!r} setup scenario {setup!r} doesn't exist")
            elif scenario.kind != "setup":
                errors.append(f"catalog.json: api {api_name!r} setup scenario {setup!r} must have kind 'setup'")
            else:
                provided |= {k for s in scenario.steps for k in s.save}
    login = catalog.login or {}
    for key in (login.get("endpoint"), (login.get("mfa") or {}).get("sms_endpoint")):
        if key and key not in catalog.endpoints:
            errors.append(f"catalog.json: login uses endpoint {key!r}, which doesn't exist")

    for endpoint in catalog.endpoints.values():
        where = _rel(catalog, endpoint.file)
        if not ID_RE.match(endpoint.id):
            errors.append(f"{where}: id {endpoint.id!r} should look like 'area.action'")
        if endpoint.file and endpoint.file.stem != endpoint.id:
            errors.append(f"{where}: file name should be {endpoint.id}.json")
        api = catalog.apis.get(endpoint.api)
        if api is None:
            errors.append(f"{where}: unknown api {endpoint.api!r}")
        elif "{{host}}" in api.get("base_url", "") and not endpoint.host:
            errors.append(f"{where}: api {endpoint.api!r} needs a host")
        if endpoint.method not in METHODS:
            errors.append(f"{where}: method {endpoint.method!r} isn't one of {', '.join(METHODS)}")
        if endpoint.effect not in EFFECTS:
            errors.append(f"{where}: effect {endpoint.effect!r} isn't one of {', '.join(EFFECTS)}")
        for spec in endpoint.status:
            if not (isinstance(spec, int) or (isinstance(spec, str) and re.fullmatch(r"[1-5]xx", spec))):
                errors.append(f"{where}: response status {spec!r} should be a number or like '2xx'")
        undocumented = endpoint.template_variables() - provided - set(endpoint.params)
        if undocumented:
            warnings.append(f"{where}: variables not listed in params: {', '.join(sorted(undocumented))}")

    for scenario in catalog.scenarios.values():
        where = _rel(catalog, scenario.file)
        if scenario.kind not in KINDS:
            errors.append(f"{where}: kind {scenario.kind!r} isn't one of {', '.join(KINDS)}")
        if scenario.file and scenario.file.stem != scenario.id:
            errors.append(f"{where}: file name should be {scenario.id}.json")
        for step in _all_steps(scenario.steps):
            endpoint = catalog.endpoints.get(step.call)
            if endpoint is None:
                errors.append(f"{where}: step {step.name!r} calls unknown endpoint {step.call!r}")
                continue
            if scenario.kind == "read" and not endpoint.is_read:
                errors.append(f"{where}: read scenario calls {step.call} ({endpoint.effect})")
            paths = list(step.save.values()) + list(step.expect.get("exists", []))
            paths += [p for k in ("contains", "equals") for p in step.expect.get(k, {})]
            for path in paths:
                try:
                    templating.query({}, templating.VAR.sub("x", path))
                except ValueError as e:
                    errors.append(f"{where}: step {step.name!r}: {e}")
        if scenario.kind == "read" and any(s.cleanup for s in scenario.steps):
            errors.append(f"{where}: read scenarios can't have cleanup steps")
    return errors, warnings


def _all_steps(steps: list[Step]):
    for step in steps:
        yield step
        yield from _all_steps(step.cleanup)
        if step.otherwise:
            yield from _all_steps([step.otherwise])


def _rel(catalog: Catalog, path: Path | None) -> str:
    if path is None:
        return "?"
    try:
        return str(path.relative_to(catalog.root))
    except ValueError:
        return str(path)
