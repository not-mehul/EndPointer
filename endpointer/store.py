"""The workspace on disk: settings, saved captures and your endpoint library.

    ~/.endpointer/                 (or $ENDPOINTER_HOME, or --home DIR)
      config.json                  your rules (hidden/pinned hosts, endpoints)
      .salt                        the key for secret fingerprints (git-ignored)
      captures/<id>.json           one browsing session's calls, secrets redacted
      library/<name>.json          one saved endpoint: its baseline shape, an example, history
      browser-profile/             the capture browser's profile (keeps your logins)

Everything is plain JSON, so the workspace can be a git repository.
Secrets (auth headers, cookies, password/token fields) are never written:
they are replaced by `<secret:…>` fingerprints keyed with a random per-workspace
salt, which keeps "this token came from that login" visible without the token.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from endpointer.classify import Rules
from endpointer.drift import Shape
from endpointer.model import Call, Redactor

HISTORY = 20  # baselines kept per endpoint


def default_home() -> Path:
    return Path(os.environ.get("ENDPOINTER_HOME") or Path.home() / ".endpointer")


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except ValueError:
        return default


def slug(key: str) -> str:
    """A file name for an endpoint key: readable part + short hash (keys can be long and full of /)."""
    readable = re.sub(r"[^a-zA-Z0-9]+", "-", key).strip("-").lower()[:80]
    return f"{readable}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"


@dataclass
class Capture:
    id: str
    name: str
    started: float
    ended: float = 0
    calls: list[Call] = field(default_factory=list)

    def to_dict(self, redactor: Redactor) -> dict:
        return {
            "format": "endpointer-capture/1",
            "id": self.id,
            "name": self.name,
            "started": self.started,
            "ended": self.ended,
            "calls": [c.redacted(redactor).to_dict() for c in self.calls],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Capture:
        return cls(
            id=data["id"],
            name=data.get("name") or data["id"],
            started=data.get("started") or 0,
            ended=data.get("ended") or 0,
            calls=[Call.from_dict(c) for c in data.get("calls", [])],
        )

    def meta(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "started": self.started,
            "ended": self.ended,
            "calls": len(self.calls),
        }


def new_capture_id(now: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time())) + "-" + secrets.token_hex(2)


class Store:
    def __init__(self, home: Path | str | None = None):
        self.home = Path(home) if home else default_home()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "captures").mkdir(exist_ok=True)
        (self.home / "library").mkdir(exist_ok=True)
        self._lock = threading.RLock()
        ignore = self.home / ".gitignore"
        if not ignore.exists():
            ignore.write_text(
                "# Endpointer: keep the browser profiles and the redaction key out of git\nbrowser-profile/\nui-profile/\n.salt\n*.tmp\n"
            )
        self.config = _read(self.home / "config.json", {}) or {}
        salt_file = self.home / ".salt"
        if not salt_file.exists():
            salt_file.write_text(secrets.token_hex(16) + "\n")
            with contextlib.suppress(OSError):
                salt_file.chmod(0o600)
        self.redactor = Redactor(bytes.fromhex(salt_file.read_text().strip()))

    @property
    def profile_dir(self) -> Path:
        return self.home / "browser-profile"

    # --- settings -----------------------------------------------------

    @property
    def rules(self) -> Rules:
        return Rules.from_dict(self.config.get("rules"))

    def set_rules(self, rules: Rules) -> None:
        with self._lock:
            self.config["rules"] = rules.to_dict()
            _write(self.home / "config.json", self.config)

    def setting(self, name: str, default: Any = None) -> Any:
        return self.config.get("settings", {}).get(name, default)

    def set_setting(self, name: str, value: Any) -> None:
        with self._lock:
            self.config.setdefault("settings", {})[name] = value
            _write(self.home / "config.json", self.config)

    # --- captures -----------------------------------------------------

    def _capture_path(self, capture_id: str) -> Path:
        if not re.match(r"^[\w.-]+$", capture_id):
            raise KeyError(capture_id)
        return self.home / "captures" / f"{capture_id}.json"

    def save_capture(self, capture: Capture) -> None:
        with self._lock:
            _write(self._capture_path(capture.id), capture.to_dict(self.redactor))

    def load_capture(self, capture_id: str) -> Capture:
        data = _read(self._capture_path(capture_id))
        if not data:
            raise KeyError(capture_id)
        return Capture.from_dict(data)

    def captures(self) -> list[dict]:
        out = []
        for path in (self.home / "captures").glob("*.json"):
            data = _read(path)
            if data and "id" in data:
                out.append(
                    {**Capture.from_dict({**data, "calls": []}).meta(), "calls": len(data.get("calls", []))}
                )
        return sorted(out, key=lambda m: m["started"], reverse=True)

    def rename_capture(self, capture_id: str, name: str) -> None:
        with self._lock:
            path = self._capture_path(capture_id)
            data = _read(path)
            if not data:
                raise KeyError(capture_id)
            data["name"] = name
            _write(path, data)

    def delete_capture(self, capture_id: str) -> None:
        with self._lock:
            self._capture_path(capture_id).unlink(missing_ok=True)

    # --- library ------------------------------------------------------

    def _entry_path(self, key: str) -> Path:
        return self.home / "library" / f"{slug(key)}.json"

    def library(self) -> list[dict]:
        out = []
        for path in (self.home / "library").glob("*.json"):
            data = _read(path)
            if data and "key" in data:
                out.append(data)
        return sorted(out, key=lambda e: (e.get("name") or e["key"]).lower())

    def entry(self, key: str) -> dict | None:
        return _read(self._entry_path(key))

    def baselines(self) -> dict[str, Shape]:
        return {e["key"]: Shape.from_dict(e["baseline"]) for e in self.library() if e.get("baseline")}

    def save_endpoint(
        self, shape: Shape, example: Call | None, *, name: str = "", capture_id: str = ""
    ) -> dict:
        """Add an endpoint to the library (or replace its baseline if it's already there)."""
        with self._lock:
            entry = self.entry(shape.key) or {
                "format": "endpointer-endpoint/1",
                "key": shape.key,
                "name": name or default_name(shape),
                "notes": "",
                "tags": [],
                "saved": time.time(),
                "history": [],
            }
            if entry.get("baseline"):
                entry["history"] = [
                    {
                        "replaced": time.time(),
                        "baseline": entry["baseline"],
                        "capture": entry.get("capture", ""),
                    },
                    *entry.get("history", []),
                ][:HISTORY]
            entry["baseline"] = shape.to_dict()
            entry["capture"] = capture_id
            entry["updated"] = time.time()
            if example is not None:
                entry["example"] = example.redacted(self.redactor).to_dict()
            _write(self._entry_path(shape.key), entry)
            return entry

    def update_endpoint(self, key: str, **fields: Any) -> dict:
        with self._lock:
            entry = self.entry(key)
            if entry is None:
                raise KeyError(key)
            for name in ("name", "notes", "tags"):
                if name in fields and fields[name] is not None:
                    entry[name] = fields[name]
            if "last_check" in fields:
                entry["last_check"] = fields["last_check"]
            _write(self._entry_path(key), entry)
            return entry

    def remove_endpoint(self, key: str) -> None:
        with self._lock:
            self._entry_path(key).unlink(missing_ok=True)


def default_name(shape: Shape) -> str:
    """`GET /api/users/{user_id}` → "Get user"; GraphQL → the operation name."""
    if shape.graphql_op:
        return shape.graphql_op
    words = [
        s
        for s in shape.template.split("/")
        if s and not s.startswith("{") and not re.match(r"^(api|v\d+|rest|_api)$", s, re.I)
    ]
    noun = words[-1] if words else shape.host
    noun = re.sub(r"[-_.]+", " ", noun)
    verb = {
        "GET": "Get",
        "POST": "Create" if not shape.template.endswith("}") else "Post",
        "PUT": "Update",
        "PATCH": "Update",
        "DELETE": "Delete",
    }.get(shape.method, shape.method.title())
    if shape.method == "GET" and not shape.template.endswith("}"):
        verb = "List"
    return f"{verb} {noun}"
