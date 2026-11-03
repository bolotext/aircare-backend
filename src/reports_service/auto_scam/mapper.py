"""Field mapper: choose what to show, as extra columns, in the Conversations and Results tables.

Each mapping is ONE column: a `label` (its title), an `origin` and a `key` (a key, or a dotted path like "result.reason"):
  analytics     a key of the (final) analytics JSON, e.g. INTEREST_CLASSIFICATION or REASON
  model         the same keys as the model first returned them, BEFORE the rules changed anything
  rules         what the rules did: key is one of changes / rules / conditions / changed / details
  conversation  a field of the uploaded record, e.g. a customer name

Values are looked up when results are read, from the stored analytics and the record, so changing the mapper
takes effect on existing results without re-running anything.
"""

import json
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


RULE_KEYS = ("changes", "rules", "conditions", "changed", "details")
_OP_TEXT = {"eq": "equals", "ne": "does not equal", "lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "in": "is one of",
            "not_in": "is not one of", "contains": "contains", "is_empty": "is empty", "not_empty": "is not empty"}


def _vt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else str(v)


def condition_text(c: dict) -> str:
    """'setupTime < 35', 'STAGE_CODE is one of DSCN, BUSY', 'DISPOSITION is empty'."""
    value = "" if c["op"] in ("is_empty", "not_empty") else f" {_vt(c['value'])}"
    return f"{c['field']} {_OP_TEXT.get(c['op'], c['op'])}{value}"


def change_text(ch: dict) -> str:
    """'INTEREST_CLASSIFICATION: HOT \u2192 WARM'."""
    return f"{ch['field']}: {_vt(ch['from']) or '(empty)'} \u2192 {_vt(ch['to'])}"


def rule_value(key: str, trace: list[dict]) -> str:
    """The text a 'Rule changes' column shows: what changed, which rules applied, the conditions that were met, or Yes/No."""
    if key == "changes":
        return "; ".join(change_text(ch) for e in trace for ch in e["changes"]) or "No rule applied"
    if key == "rules":
        return "; ".join(e["rule"] for e in trace)
    if key == "conditions":
        return "; ".join(f"{condition_text(c)} (was {_vt(c['actual']) or 'empty'})" for e in trace for c in e["conditions"] if c["met"])
    if key == "details":  # the whole "Changed by rules" explanation: per rule, what it changed and why
        parts = []
        for e in trace:
            why = ", ".join(f"{condition_text(c)} (was {_vt(c['actual']) or 'empty'})" for c in e["conditions"] if c["met"]) or "always applies"
            parts.append(f"Rule {e['index']} {e['rule']}: " + "; ".join(change_text(ch) for ch in e["changes"]) + f" because {why}")
        return " | ".join(parts) or "No rule applied"
    return "Yes" if trace else "No"  # "changed"


def apply_mapper(mapper: list, analytics: dict, record: dict, llm: dict | None = None, trace: list[dict] | None = None,
                 recorded: bool = False) -> list[dict]:
    """One entry per mapped column: label, origin, key, value, found. `origin` says where the value comes from:
    "analytics" (the final analytics JSON, after the rules), "model" (what the model said BEFORE the rules, same keys),
    "rules" (what the rules did: changes / rules / conditions / changed / details) or "conversation" (a field of the uploaded record).
    `model` and `rules` need the stored history, so they are not found for results saved before it was kept (`recorded`)."""
    out = []
    for m in mapper:
        if m.origin == "rules":
            found, value = recorded, (rule_value(m.key, trace or []) if recorded else None)
        elif m.origin == "model":
            found, value = lookup(llm or {}, m.key) if recorded else (False, None)
        else:
            found, value = lookup(analytics if m.origin == "analytics" else record, m.key)
        out.append({"label": m.label, "origin": m.origin, "key": m.key, "value": value if found else None, "found": found})
    return out


def changed_fields(analytics: dict, record: dict) -> list[str]:
    """Analytics keys that the conversation also has (same name) with a different value, e.g. INTEREST_CLASSIFICATION."""
    return [k for k, v in analytics.items() if k in record and not same(record[k], v)]


def stage_codes(analytics: dict, record: dict) -> tuple[Any, Any]:
    """(previous, updated) STAGE_CODE: the record's, and the analytics' `STAGE_CODE` key if a rule/the model set one."""
    previous = record.get("STAGE_CODE")
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
