"""Post-processing rules, editable from the UI (Settings tab) -- a safe replacement for hard-coded Python.

After the model returns its analytics, the rules run top to bottom. Each rule has conditions on any field of the
conversation (STAGE_CODE, setupTime, DISPOSITION, ...) or on the analytics produced so far (INTEREST_CLASSIFICATION,
...), and actions that SET an analytics output field (e.g. INTEREST_CLASSIFICATION, DISPOSITION). Later rules see the
changes of earlier ones. There is no code evaluation anywhere: conditions use a fixed operator list.

The default rules are exactly the former hard-coded `_normalize_analytics` logic in scam.py.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Operator = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "contains", "is_empty", "not_empty"]
NUMERIC = {"lt", "lte", "gt", "gte"}
MAX_RULES = 50


def _field_name(v: str) -> str:
    v = v.strip()
    if not v or len(v) > 100 or v.startswith("$"):
        raise ValueError("field must be a non-empty name (max 100 chars, not starting with '$')")
    return v


class Condition(BaseModel):
    field: str
    op: Operator = "eq"
    value: Any = None  # a string/number; a list (or comma-separated string) for in / not_in; unused for is_empty

    _check_field = field_validator("field")(_field_name)


class Action(BaseModel):
    field: str
    value: Any = None  # the value to set the field to

    _check_field = field_validator("field")(_field_name)


class Rule(BaseModel):
    name: str = Field("", max_length=120)
    enabled: bool = True
    match: Literal["all", "any"] = "all"  # AND / OR over `when`; an empty `when` always applies
    when: list[Condition] = Field(default_factory=list, max_length=10)
    then: list[Action] = Field(min_length=1, max_length=10)


def default_rules() -> list[Rule]:
    """The former scam.py `_normalize_analytics`, as rules."""
    lead = "INTEREST_CLASSIFICATION"
    return [
        Rule(name="First message delivered only -> COLD",
             when=[Condition(field="STAGE_CODE", op="eq", value="INITIAL_MESSAGE_DELIVERED")],
             then=[Action(field=lead, value="COLD")]),
        Rule(name="Interested, but call shorter than 35s: HOT -> WARM",
             when=[Condition(field="setupTime", op="lt", value=35),
                   Condition(field="STAGE_CODE", op="eq", value="INTERESTED"),
                   Condition(field=lead, op="eq", value="HOT")],
             then=[Action(field=lead, value="WARM")]),
        Rule(name="No real conversation (DSCN / VOICEMAIL / BUSY / NO_ANSWER) -> COLD",
             when=[Condition(field="STAGE_CODE", op="in", value=["DSCN", "VOICEMAIL", "BUSY", "NO_ANSWER"])],
             then=[Action(field=lead, value="COLD")]),
        Rule(name="Missing or invalid score -> COLD",
             when=[Condition(field=lead, op="not_in", value=["WARM", "COLD", "HOT"])],
             then=[Action(field=lead, value="COLD")]),
    ]


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_list(v: Any) -> list[str]:
    items = v if isinstance(v, list) else str(v if v is not None else "").split(",")
    return [str(i).strip() for i in items if str(i).strip()]


def _same(a: Any, b: Any) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    return str(a).strip() == str(b).strip()


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, (str, list, dict)) and len(v) == 0) or (isinstance(v, str) and not v.strip())


def check(cond: Condition, actual: Any) -> bool:
    op, expected = cond.op, cond.value
    if op == "is_empty":
        return _empty(actual)
    if op == "not_empty":
        return not _empty(actual)
    if op in NUMERIC:
        a, e = _num(actual if actual is not None else 0), _num(expected)  # a missing number counts as 0
        if a is None or e is None:
            return False
        return {"lt": a < e, "lte": a <= e, "gt": a > e, "gte": a >= e}[op]
    if op == "contains":
        return not _empty(actual) and str(expected).strip().lower() in str(actual).lower()
    if op in ("in", "not_in"):
        hit = not _empty(actual) and any(_same(actual, item) for item in _as_list(expected))
        return hit if op == "in" else not hit
    if op == "eq":
        return not _empty(actual) and _same(actual, expected)
    return _empty(actual) or not _same(actual, expected)  # ne


def run_rules(rules: list[Rule], conversation: dict, analytics: dict) -> tuple[dict, list[str], list[dict]]:
    """(analytics after the rules, names of every rule that fired, trace of the rules that actually CHANGED something).

    A trace entry says why and what: the conditions that were checked (with the value each one saw and whether it was
    met) and the changes made (field, from, to). A rule that fires but sets a value the field already had changes
    nothing, so it is in `fired` but not in the trace."""
    out = dict(analytics)
    fired: list[str] = []
    trace: list[dict] = []
    for index, rule in enumerate(rules, 1):
        if not rule.enabled:
            continue
        checked = []
        for c in rule.when:
            actual = out[c.field] if c.field in out else conversation.get(c.field)
            checked.append({"field": c.field, "op": c.op, "value": c.value, "actual": actual, "met": check(c, actual)})
        met = [c["met"] for c in checked]
        applies = (any(met) if rule.match == "any" else all(met)) if met else True
        if not applies:
            continue
        name = rule.name or f"Rule {index}"
        fired.append(name)
        changes = []
        for action in rule.then:
            before = out.get(action.field)
            out[action.field] = action.value
            changed = action.value is not None if before is None else not _same(before, action.value)
            if changed:
                changes.append({"field": action.field, "from": before, "to": action.value})
        if changes:
            trace.append({"index": index, "rule": name, "match": rule.match, "conditions": checked, "changes": changes})
    return out, fired, trace


def apply_rules(rules: list[Rule], conversation: dict, analytics: dict) -> tuple[dict, list[str]]:
    """Returns (analytics after the rules, names of the rules that fired). Never raises on bad data."""
    out, fired, _ = run_rules(rules, conversation, analytics)
    return out, fired


def apply_rules_traced(rules: list[Rule], conversation: dict, analytics: dict) -> tuple[dict, list[dict]]:
    """Returns (analytics after the rules, trace of what the rules changed and why)."""
    out, _, trace = run_rules(rules, conversation, analytics)
    return out, trace
