"""Match a raw URL (e.g. from a browser HAR) to catalog endpoints."""

from __future__ import annotations

import re
from dataclasses import dataclass

from endpoint_checker.catalog import Catalog

_VAR = re.compile(r"\\\{\\\{\s*([^{}]+?)\s*\\\}\\\}")


def _base_regex(base_url: str) -> re.Pattern:
    escaped = re.escape(base_url.rstrip("/"))

    def group(m: re.Match) -> str:
        name = m.group(1).split("|")[0].strip()
        return r"(?P<host>[^./]+)" if name == "host" else r"[^/]+?"

    return re.compile("^" + _VAR.sub(group, escaped) + "/")


def _path_regex(path: str) -> re.Pattern:
    return re.compile("^" + _VAR.sub(r"[^/]+", re.escape(path.lstrip("/"))) + "/?$")


@dataclass
class Location:
    api: str
    host: str | None
    path: str  # relative to the API's base URL


class Matcher:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.bases = {name: _base_regex(api["base_url"]) for name, api in catalog.apis.items()}
        self.paths = [(e, _path_regex(e.path)) for e in catalog.endpoints.values()]

    def locate(self, url: str) -> list[Location]:
        """Which API(s) the URL belongs to, and the path under each base URL."""
        url = url.split("?", 1)[0].split("#", 1)[0]
        out = []
        for name, regex in self.bases.items():
            m = regex.match(url)
            if m:
                out.append(Location(name, m.groupdict().get("host"), url[m.end() :]))
        return out

    def match(self, method: str, url: str) -> list[str]:
        """Endpoint ids for a request (several when endpoints share a request)."""
        method = method.upper()
        ids = []
        for loc in self.locate(url):
            for endpoint, regex in self.paths:
                if endpoint.api != loc.api or endpoint.method != method:
                    continue
                if endpoint.host and loc.host and endpoint.host != loc.host:
                    continue
                if regex.match(loc.path):
                    ids.append(endpoint.id)
        return ids
