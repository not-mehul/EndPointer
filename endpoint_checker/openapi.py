"""Export the catalog as an OpenAPI 3.1 document.

Useful for browsing (Swagger UI, Redoc) and for importing the endpoints
into Postman, Insomnia or Bruno. The catalog stays the source of truth;
the export is regenerated whenever needed.
"""

from __future__ import annotations

import re
from datetime import date

from endpoint_checker.catalog import Catalog, Endpoint

_VAR = re.compile(r"\{\{\s*([^{}|]+?)\s*(?:\|[^{}]*)?\}\}")


def _openapi_template(text: str) -> str:
    return _VAR.sub(lambda m: "{" + m.group(1) + "}", text)


def _server(catalog: Catalog, api_name: str, host: str | None) -> dict:
    api = catalog.apis[api_name]
    url = _openapi_template(api["base_url"]).rstrip("/")
    variables = {}
    for name in re.findall(r"\{([^{}]+)\}", url):
        if name == "host" and host:
            url = url.replace("{host}", host)
            continue
        default = catalog.defaults.get(name) or {"org_short_name": "your-org", "region": "api"}.get(name, name)
        variables[name] = {"default": str(default)}
        if name == "region":
            variables[name]["enum"] = ["api", "api.eu", "api.au"]
    server: dict = {"url": url}
    if variables:
        server["variables"] = variables
    if api.get("title"):
        server["description"] = api["title"]
    return server


def _operation(catalog: Catalog, endpoint: Endpoint, also: list[Endpoint]) -> dict:
    description = []
    if endpoint.description:
        description.append(endpoint.description)
    if endpoint.warning:
        description.append(f"**Warning:** {endpoint.warning}")
    if endpoint.notes:
        description.append(endpoint.notes)
    if also:
        description.append(
            "This path and method also serve: " + ", ".join(f"`{e.id}` ({e.title or e.effect})" for e in also)
        )
    op: dict = {
        "operationId": endpoint.id,
        "summary": endpoint.title or endpoint.id,
        "tags": [endpoint.area],
        "x-effect": endpoint.effect,
    }
    if description:
        op["description"] = "\n\n".join(description)
    if endpoint.host:
        op["servers"] = [_server(catalog, endpoint.api, endpoint.host)]
    params = []
    for name in re.findall(r"\{\{\s*([^{}|]+?)\s*\}\}", endpoint.path):
        params.append({"name": name, "in": "path", "required": True, "schema": {"type": "string"}})
    for name, value in endpoint.query.items():
        param = {"name": name, "in": "query", "schema": {"type": "string"}}
        if isinstance(value, str) and "{{" not in value:
            param["example"] = value
        params.append(param)
    for name, value in endpoint.headers.items():
        params.append({"name": name, "in": "header", "schema": {"type": "string"}, "example": value})
    if params:
        op["parameters"] = params
    bodies = {e.id: e.body for e in [endpoint, *also] if e.body is not None}
    if len(bodies) == 1:
        op["requestBody"] = {"content": {"application/json": {"example": next(iter(bodies.values()))}}}
    elif bodies:
        examples = {key: {"summary": key, "value": body} for key, body in bodies.items()}
        op["requestBody"] = {"content": {"application/json": {"examples": examples}}}
    content: dict = {}
    if endpoint.schema:
        content["schema"] = endpoint.schema
    if endpoint.example is not None:
        content["example"] = endpoint.example
    responses: dict = {"200": {"description": "Success"}}
    if content:
        responses["200"]["content"] = {"application/json": content}
    for status in endpoint.status:
        if isinstance(status, int) and not 200 <= status < 300:
            responses[str(status)] = {"description": "Normal answer for this endpoint (see notes)"}
    op["responses"] = responses
    if endpoint.auth == "none":
        op["security"] = []
    else:
        op["security"] = [{f"{endpoint.api}Auth": []}]
    return op


def export(catalog: Catalog) -> dict:
    doc: dict = {
        "openapi": "3.1.0",
        "info": {
            "title": catalog.name,
            "version": date.today().isoformat(),
            "description": catalog.description
            + "\n\nGenerated from an endpoint catalog (endpoint-catalog/1). Paths use {variables}; "
            "request bodies are templates with {{variables}}.",
        },
        "servers": [_server(catalog, name, None) for name in catalog.apis],
        "tags": [{"name": area} for area in sorted({e.area for e in catalog.endpoints.values()})],
        "paths": {},
        "components": {"securitySchemes": {}},
    }
    for name, api in catalog.apis.items():
        headers = list((api.get("headers") or {}).items())
        token_header = next((h for h, v in headers if "token" in v or "auth" in h.lower()), headers[0][0] if headers else "Authorization")
        doc["components"]["securitySchemes"][f"{name}Auth"] = {
            "type": "apiKey",
            "in": "header",
            "name": token_header,
            "description": "Headers: " + ", ".join(h for h, _ in headers)
            + (("; cookies: " + ", ".join(api["cookies"])) if api.get("cookies") else ""),
        }

    # Endpoints that send the identical request share one operation.
    groups: dict[tuple, list[Endpoint]] = {}
    for endpoint in sorted(catalog.endpoints.values(), key=lambda e: e.id):
        key = (endpoint.api, endpoint.host, endpoint.method, endpoint.path)
        groups.setdefault(key, []).append(endpoint)
    for (api, host, method, path), endpoints in groups.items():
        url_path = "/" + _openapi_template(path).lstrip("/")
        item = doc["paths"].setdefault(url_path, {})
        if method.lower() in item:
            # Same path and method on another host: keep both by host-qualifying the path.
            url_path = f"/{host or api}{url_path}"
            item = doc["paths"].setdefault(url_path, {})
        first, *rest = endpoints
        item[method.lower()] = _operation(catalog, first, rest)
    return doc
