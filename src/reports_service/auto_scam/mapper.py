"""Field mapper: connect keys of the analytics JSON the model returned to the conversation's own fields.

Each mapping says: take `source` (a key, or a dotted path like "result.disposition", in the analytics JSON) and
treat it as the new value of the conversation field `field` (STAGE_CODE, DISPOSITION, INTEREST_CLASSIFICATION, ...),
shown as a column called `name`. The "previous" value is simply what the conversation had in `field`.

Mapped values are computed when results are read, from the stored analytics, so changing the mapper takes effect on
existing results without re-running anything.
"""

from typing import Any


def lookup(data: Any, path: str) -> tuple[bool, Any]:
    """(found, value) for `path` in `data`: an exact key first (keys may contain dots), else dotted dict/list steps."""
    if isinstance(data, dict) and path in data:
        return True, data[path]
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


def same(a: Any, b: Any) -> bool:
    if a == b:
        return True
    return a is not None and b is not None and str(a).strip() == str(b).strip()


def apply_mapper(mapper: list, analytics: dict, record: dict) -> list[dict]:
    """One entry per mapping: name, source, field, previous (the record's value), updated, found, changed."""
    out = []
    for m in mapper:
        found, updated = lookup(analytics, m.source)
        previous = record.get(m.field)
        out.append({
            "name": m.name, "source": m.source, "field": m.field, "previous": previous,
            "updated": updated if found else None, "found": found, "changed": found and not same(previous, updated),
        })
    return out


def stage_codes(mapper: list, analytics: dict, record: dict) -> tuple[Any, Any]:
    """(previous, updated) STAGE_CODE: the mapping that targets STAGE_CODE if any, else an analytics `STAGE_CODE` key."""
    previous = record.get("STAGE_CODE")
    for m in mapper:
        if m.field == "STAGE_CODE":
            found, value = lookup(analytics, m.source)
            if found and value not in (None, ""):
                return previous, value
    value = analytics.get("STAGE_CODE")
    return previous, value if value not in (None, "") else previous


def analytics_paths(samples: list[dict], max_depth: int = 4, limit: int = 200) -> list[dict]:
    """Paths (dotted) of the values found in a few analytics JSONs, each with an example, for the mapper's dropdown."""
    found: dict[str, Any] = {}

    def walk(node: Any, prefix: str, depth: int) -> None:
        if isinstance(node, dict) and depth < max_depth:
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k), depth + 1)
        elif prefix and prefix not in found:
            found[prefix] = node

    for s in samples:
        walk(s, "", 0)
        if len(found) >= limit:
            break
    return [{"path": p, "sample": v if not isinstance(v, (dict, list)) else None} for p, v in found.items()][:limit]
