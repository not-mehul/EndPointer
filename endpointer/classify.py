"""Which captured calls are API calls you care about, and which are noise.

Every call gets a score and the reasons behind it. Static files (scripts,
images, fonts, styles), pre-flight requests and analytics/telemetry beacons
are noise. JSON responses, calls to the site you're browsing, calls that
change something (POST/PUT/PATCH/DELETE), endpoints you saved and endpoints
whose responses feed other calls score higher. Your own rules win: a host or
endpoint you hide is always noise, one you pin is always shown.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

from endpointer.model import Call

THRESHOLD = 3

STATIC_TYPES = {
    "script",
    "stylesheet",
    "image",
    "media",
    "font",
    "texttrack",
    "manifest",
    "cspviolationreport",
    "preflight",
    "prefetch",
}
STATIC_EXT = re.compile(
    r"\.(js|mjs|css|map|png|jpe?g|gif|webp|avif|svg|ico|bmp|woff2?|ttf|otf|eot|mp4|webm|mp3|wav|ogg|pdf|zip|wasm)$",
    re.I,
)
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Analytics, ads, error reporting, session replay, feature-flag telemetry.
NOISE_HOSTS = [
    "*google-analytics.com",
    "*googletagmanager.com",
    "*doubleclick.net",
    "*googlesyndication.com",
    "*googleadservices.com",
    "*analytics.google.com",
    "*facebook.com",
    "*facebook.net",
    "*segment.io",
    "*segment.com",
    "*sentry.io",
    "*ingest.sentry.io",
    "*datadoghq.com",
    "*datadoghq.eu",
    "*browser-intake-*",
    "*nr-data.net",
    "*newrelic.com",
    "*hotjar.com",
    "*hotjar.io",
    "*mixpanel.com",
    "*amplitude.com",
    "*fullstory.com",
    "*clarity.ms",
    "*bing.com",
    "*linkedin.com",
    "*ads-twitter.com",
    "*intercom.io",
    "*intercomcdn.com",
    "*heapanalytics.com",
    "*pendo.io",
    "*logrocket.io",
    "*lr-ingest.io",
    "*launchdarkly.com",
    "*statsig.com",
    "*optimizely.com",
    "*branch.io",
    "*bugsnag.com",
    "*rollbar.com",
    "*cloudflareinsights.com",
    "*gstatic.com",
    "*fonts.googleapis.com",
    "*recaptcha.net",
    "*hcaptcha.com",
    "*stripe.network",
    "*cookielaw.org",
    "*onetrust.com",
]
NOISE_PATHS = re.compile(
    r"/(collect|beacon|track|tracking|telemetry|metrics|rum|log|logs|events?/batch|pixel|analytics|i/v\d+/event|ping|heartbeat)(/|$)",
    re.I,
)


@dataclass
class Rules:
    """Your overrides, stored in the workspace."""

    hide_hosts: list[str] = field(default_factory=list)  # glob patterns, e.g. "*.cdn.example.com"
    hide_keys: list[str] = field(default_factory=list)  # endpoint keys
    pin_hosts: list[str] = field(default_factory=list)
    pin_keys: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict | None) -> Rules:
        data = data or {}
        return cls(
            **{k: list(data.get(k) or []) for k in ("hide_hosts", "hide_keys", "pin_hosts", "pin_keys")}
        )

    def to_dict(self) -> dict:
        return {
            "hide_hosts": self.hide_hosts,
            "hide_keys": self.hide_keys,
            "pin_hosts": self.pin_hosts,
            "pin_keys": self.pin_keys,
        }


@dataclass
class Relevance:
    score: int
    important: bool
    reasons: list[str]


def _host_matches(host: str, patterns: list[str]) -> bool:
    host = host.lower().split(":")[0]
    return any(fnmatch.fnmatch(host, p.lower()) or host == p.lower().lstrip("*.") for p in patterns)


def site_of(host: str) -> str:
    """Rough registrable domain: `api.eu.example.com` → `example.com` (good enough to tell first party)."""
    host = host.lower().split(":")[0]
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", host) or host in ("localhost", "[::1]"):
        return host
    parts = host.split(".")
    if (
        len(parts) >= 3
        and len(parts[-1]) == 2
        and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "edu")
    ):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_static(call: Call) -> bool:
    return (
        call.resource_type in STATIC_TYPES
        or bool(STATIC_EXT.search(call.path))
        or call.method in ("OPTIONS", "HEAD")
    )


def score(
    call: Call,
    *,
    rules: Rules | None = None,
    sites: set[str] | None = None,
    saved: set[str] | None = None,
    feeds: int = 0,
) -> Relevance:
    """How likely `call` is an API call worth keeping.

    `sites` are the registrable domains of the pages being browsed, `saved` the
    endpoint keys in your library, `feeds` how many other endpoints use values
    from this one's responses.
    """
    rules = rules or Rules()
    reasons: list[str] = []
    key = call.key
    if key in rules.hide_keys or _host_matches(call.host, rules.hide_hosts):
        return Relevance(-10, False, ["hidden by you"])
    if key in rules.pin_keys or _host_matches(call.host, rules.pin_hosts):
        return Relevance(10, True, ["pinned by you"])
    if saved and key in saved:
        return Relevance(9, True, ["in your library"])

    points = 0
    if call.resource_type == "document":
        points -= 2
        reasons.append("page load")
    if is_static(call):
        return Relevance(-5, False, ["static file or pre-flight"])
    if _host_matches(call.host, NOISE_HOSTS):
        return Relevance(-5, False, ["analytics / telemetry host"])
    if NOISE_PATHS.search(call.path):
        points -= 3
        reasons.append("looks like tracking")

    if call.response_kind == "json":
        points += 3
        reasons.append("JSON response")
    elif call.response_kind in ("text",) and "xml" in call.mime:
        points += 1
        reasons.append("XML response")
    if call.request_kind == "json":
        points += 1
        reasons.append("JSON request")
    if call.resource_type in ("xhr", "fetch", "eventsource"):
        points += 1
        reasons.append(call.resource_type)
    if call.method in MUTATING:
        points += 2
        reasons.append("changes data")
    if call.graphql_op:
        points += 2
        reasons.append("GraphQL operation")
    if sites and site_of(call.host) in sites:
        points += 2
        reasons.append("same site as the page")
    if re.search(r"/(api|graphql|rest|v\d+|rpc|_api|ajax)(/|$)", call.path, re.I) or call.host.startswith(
        "api."
    ):
        points += 1
        reasons.append("API-style URL")
    if feeds:
        points += min(feeds, 3)
        reasons.append(f"feeds {feeds} other call{'s' if feeds != 1 else ''}")
    if call.status == 204 and call.method == "POST" and not call.request_body:
        points -= 2
        reasons.append("empty POST (beacon?)")
    return Relevance(points, points >= THRESHOLD, reasons)
