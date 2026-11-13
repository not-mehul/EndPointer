"""Response shapes as (a subset of) JSON Schema.

Each endpoint stores `response.schema`, inferred from observed responses:

    {"type": "object", "properties": {...}, "required": [...]}
    {"type": "array", "items": {...}}
    {"type": "string"} / "number" / "integer" / "boolean"
    {"type": ["string", "number"]}      several observed types
    {"type": "object", "additionalProperties": {...}}   a map keyed by ids
    {}                                  unknown (only null seen) — anything goes

Comparing a live response against the schema reports:

- **missing**: a required field is absent (for a list of objects: absent from
  every sampled item — fields that are only sometimes present are fine);
- **type**: a value has a type the schema doesn't allow;
- **added**: a field the schema doesn't know (informational);
- **unverified**: a list was empty, so its items couldn't be checked.

`null` is always accepted: many fields are simply unset on some objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SCALAR = ["string", "number", "boolean"]
_PLACEHOLDER = re.compile(r"^<[^<>]+>$")
# Object keys that are ids (the object is a map, e.g. {"children": {"<group_id>": {...}}}).
_ID_KEY = re.compile(r"^(<[^<>]+>|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{6,})$", re.I)


def is_map(value: dict) -> bool:
    return bool(value) and all(_ID_KEY.match(str(k)) for k in value)


@dataclass
class Diff:
    kind: str  # "missing" | "type" | "added" | "unverified"
    path: str
    detail: str = ""

    @property
    def significant(self) -> bool:
        return self.kind in ("missing", "type")

    def __str__(self) -> str:
        label = {"missing": "missing", "type": "type changed", "added": "new", "unverified": "unverified"}
        return f"{label[self.kind]}: {self.path}" + (f" ({self.detail})" if self.detail else "")


def type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# ----------------------------------------------------------------------
# Inference
# ----------------------------------------------------------------------


def infer(value: Any) -> dict:
    """Schema for one observed value. "<placeholder>" strings mean "any scalar"."""
    if value is None:
        return {}
    if isinstance(value, dict):
        if is_map(value):
            merged: dict = {}
            for item in list(value.values())[:50]:
                merged = merge(merged, infer(item), in_list=True)
            return {"type": "object", "additionalProperties": merged}
        return {
            "type": "object",
            "properties": {k: infer(v) for k, v in value.items()},
            "required": sorted(value),
        }
    if isinstance(value, list):
        schema: dict = {"type": "array"}
        items = [infer(v) for v in value[:50]]
        if items:
            merged = items[0]
            for other in items[1:]:
                merged = merge(merged, other, in_list=True)
            schema["items"] = merged
        return schema
    if isinstance(value, str) and _PLACEHOLDER.match(value):
        return {"type": list(SCALAR)}
    return {"type": type_of(value)}


def _types(schema: dict) -> list[str]:
    t = schema.get("type")
    if t is None:
        return []
    return list(t) if isinstance(t, list) else [t]


def merge(a: dict, b: dict, *, in_list: bool = False) -> dict:
    """Combine two schemas. In lists, a field is required only if always present."""
    if not a:
        return dict(b)
    if not b:
        return dict(a)
    types = sorted(set(_types(a)) | set(_types(b)), key=_type_order)
    if "integer" in types and "number" in types:
        types.remove("integer")
    out: dict = {"type": types[0] if len(types) == 1 else types}
    if "object" in types:
        pa, pb = a.get("properties", {}), b.get("properties", {})
        props = {}
        for key in list(pa) + [k for k in pb if k not in pa]:
            if key in pa and key in pb:
                props[key] = merge(pa[key], pb[key])
            else:
                props[key] = dict(pa.get(key) or pb.get(key))
        out["properties"] = props
        ra, rb = set(a.get("required", [])), set(b.get("required", []))
        out["required"] = sorted(ra & rb if in_list or ("object" in _types(a) and "object" in _types(b)) else ra | rb)
    if "object" in types and ("additionalProperties" in a or "additionalProperties" in b):
        va, vb = a.get("additionalProperties"), b.get("additionalProperties")
        out.pop("properties", None)
        out.pop("required", None)
        out["additionalProperties"] = merge(va, vb, in_list=True) if va and vb else dict(va or vb)
    if "array" in types:
        ia, ib = a.get("items"), b.get("items")
        if ia and ib:
            out["items"] = merge(ia, ib, in_list=True)
        elif ia or ib:
            out["items"] = dict(ia or ib)
    return out


def _type_order(t: str) -> int:
    order = ["object", "array", "string", "integer", "number", "boolean", "null"]
    return order.index(t) if t in order else len(order)


# ----------------------------------------------------------------------
# Comparison
# ----------------------------------------------------------------------


def _allows(schema: dict, value: Any) -> bool:
    allowed = _types(schema)
    if not allowed or value is None:
        return True
    actual = type_of(value)
    if actual == "integer" and "number" in allowed:
        return True
    return actual in allowed


def compare(schema: dict | None, value: Any, path: str = "$", *, sample: int = 20) -> list[Diff]:
    """Differences between a stored schema and a live value."""
    if not schema or value is None:
        return []
    if not _allows(schema, value):
        return [Diff("type", path, f"expected {'/'.join(_types(schema))}, got {type_of(value)}")]
    if isinstance(value, dict) and "properties" in schema:
        props = schema["properties"]
        diffs: list[Diff] = []
        for key in schema.get("required", []):
            if key not in value:
                diffs.append(Diff("missing", f"{path}.{key}"))
        for key, sub in props.items():
            if key in value:
                diffs += compare(sub, value[key], f"{path}.{key}", sample=sample)
        diffs += [Diff("added", f"{path}.{key}") for key in value if key not in props]
        return diffs
    if isinstance(value, dict) and "additionalProperties" in schema and "properties" not in schema:
        values = list(value.values())[:sample]
        sub = schema["additionalProperties"]
        if not values:
            return [Diff("unverified", f"{path}.*", "empty map on this org")] if sub else []
        if "properties" in sub and all(isinstance(v, dict) for v in values):
            return _compare_objects(sub, values, f"{path}.*", sample)
        return compare(sub, values[0], f"{path}.*", sample=sample)
    if isinstance(value, list) and schema.get("items"):
        items_schema = schema["items"]
        if not value:
            return [Diff("unverified", f"{path}[]", "empty list on this org")]
        items = value[:sample]
        if "properties" in items_schema and all(isinstance(i, dict) for i in items):
            return _compare_objects(items_schema, items, f"{path}[]", sample)
        diffs = []
        seen = set()
        for item in items:
            for d in compare(items_schema, item, f"{path}[]", sample=sample):
                if (d.kind, d.path) not in seen:
                    seen.add((d.kind, d.path))
                    diffs.append(d)
        return diffs
    return []


def _compare_objects(schema: dict, items: list[dict], path: str, sample: int) -> list[Diff]:
    props = schema["properties"]
    diffs: list[Diff] = []
    for key in schema.get("required", []):
        if not any(key in i for i in items):
            diffs.append(Diff("missing", f"{path}.{key}"))
    for key, sub in props.items():
        holders = [i[key] for i in items if key in i and i[key] is not None]
        if holders:
            diffs += compare(sub, holders[0], f"{path}.{key}", sample=sample)
    seen = {k for i in items for k in i}
    diffs += [Diff("added", f"{path}.{k}") for k in sorted(seen - props.keys())]
    return diffs


# ----------------------------------------------------------------------
# Sanitising observed bodies before storing them as examples
# ----------------------------------------------------------------------

_ENUM = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")
_BOOL_TEXT = {"true", "false", "True", "False"}


def sanitize(value: Any, key: str | None = None) -> Any:
    """Keep the structure, drop the data: ids/names/emails become "<key>".

    ALL_CAPS enum-like strings and "true"/"false" strings are kept (they
    document allowed values); lists keep one example element; floats (e.g.
    coordinates) become 0.0; integers are kept.
    """
    if isinstance(value, dict):
        if is_map(value):
            first = next(iter(value.values()))
            return {f"<{key or 'map'}_key>": sanitize(first, key)}
        return {k: sanitize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(value[0], key)] if value else []
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return 0.0
    if isinstance(value, str):
        if _ENUM.match(value) or value in _BOOL_TEXT or _PLACEHOLDER.match(value):
            return value
        return f"<{key}>" if key else "<value>"
    return f"<{key}>" if key else "<value>"


def diff_schemas(old: dict | None, new: dict | None, path: str = "$") -> list[Diff]:
    """Differences between two inferred schemas (an earlier and a later look at the same endpoint).

    Same rules as `compare`: a field is missing when it was required before and
    no longer appears at all, a type changed when the new types aren't allowed
    by the old ones, new fields are informational and an empty list or map
    can't be checked.
    """
    if not old or not new:
        return []
    old_types, new_types = set(_types(old)), set(_types(new))
    if old_types and new_types:
        allowed = old_types | ({"integer"} if "number" in old_types else set())
        extra = new_types - allowed
        if extra:
            return [
                Diff("type", path, f"was {'/'.join(sorted(old_types))}, now {'/'.join(sorted(new_types))}")
            ]
    diffs: list[Diff] = []
    if "properties" in old and "properties" in new:
        po, pn = old["properties"], new["properties"]
        for key in old.get("required", []):
            if key not in pn:
                diffs.append(Diff("missing", f"{path}.{key}"))
        for key, sub in po.items():
            if key in pn:
                diffs += diff_schemas(sub, pn[key], f"{path}.{key}")
        diffs += [Diff("added", f"{path}.{key}") for key in pn if key not in po]
    elif "additionalProperties" in old and "additionalProperties" in new:
        diffs += diff_schemas(old["additionalProperties"], new["additionalProperties"], f"{path}.*")
    elif "properties" in old and "additionalProperties" in new and old["properties"]:
        diffs.append(Diff("type", path, "was an object with fixed fields, now a map keyed by ids"))
    elif "additionalProperties" in old and "properties" in new and new["properties"]:
        diffs.append(Diff("type", path, "was a map keyed by ids, now an object with fixed fields"))
    if old.get("items") and "array" in new_types:
        if new.get("items"):
            diffs += diff_schemas(old["items"], new["items"], f"{path}[]")
        else:
            diffs.append(Diff("unverified", f"{path}[]", "empty list this time"))
    return diffs
