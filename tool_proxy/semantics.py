"""Business-semantic helpers.

``absent`` ("没有找到") and ``unknown`` ("暂不能确定") are deliberately separate.
Legacy adapters that do not retain a distinction project it to ``empty``, which
is what their users historically saw.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

ABSENT = "absent"
UNKNOWN = "unknown"
EMPTY = "empty"
PRESENT = "present"

# The distinctions which matter for review/proofreading in this domain.
CORE_DISTINCTIONS: tuple[str, ...] = (ABSENT, UNKNOWN)
KNOWN_DISTINCTIONS: frozenset[str] = frozenset({ABSENT, UNKNOWN, PRESENT})

FORMAL_PURPOSE_VALUES = {
    "formal",
    "proofread",
    "proofreading",
    "citation",
    "cite",
    "official",
    "review",
    "正式",
    "正式校对",
    "校对",
    "引用",
}
SUMMARY_PURPOSE_VALUES = {
    "summary",
    "browse",
    "browsing",
    "overview",
    "preview",
    "摘要",
    "浏览",
}


def normalize_distinctions(values: Iterable[str] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return sorted({str(value) for value in values})


def purpose_is_formal(purpose: Any, required: Iterable[str] | None = None) -> bool:
    """Infer whether a request is formal proofreading/citation use."""

    required_set = set(normalize_distinctions(required))
    if purpose is not None:
        value = str(purpose).strip().lower()
        if value in FORMAL_PURPOSE_VALUES:
            return True
        if value in SUMMARY_PURPOSE_VALUES:
            return False
    # Backward-compatible inference for the supplied drills: asking for both
    # core distinctions is the formal proofreading requirement.
    return set(CORE_DISTINCTIONS).issubset(required_set)


def requirements_for(purpose: Any, need: Iterable[str] | str | None) -> tuple[bool, list[str]]:
    requested = set(normalize_distinctions(need))
    formal = purpose_is_formal(purpose, requested)
    if formal:
        requested.update(CORE_DISTINCTIONS)
    return formal, sorted(requested)


def body_distinctions(body: Any) -> set[str]:
    """Return semantic distinctions carried by a synthetic tool body."""

    if not isinstance(body, dict):
        return set()

    result: set[str] = set()
    status = body.get("status")
    if isinstance(status, str) and status in KNOWN_DISTINCTIONS:
        result.add(status)

    explicit = body.get("distinctions")
    if isinstance(explicit, (list, tuple, set, frozenset)):
        result.update(str(item) for item in explicit if str(item) in KNOWN_DISTINCTIONS)

    # Convenience encodings for synthetic tools.
    if body.get("found") is False:
        result.add(ABSENT)
    if body.get("certain") is False or body.get("determinable") is False:
        result.add(UNKNOWN)
    return result


def project_body(body: Any, keeps: Iterable[str]) -> tuple[Any, dict[str, Any]]:
    """Project a body through an adapter.

    Returns the projected body and a semantic report.  The raw body is never
    mutated because original tool replies are independent facts.
    """

    keeps_set = set(normalize_distinctions(keeps))
    before = body_distinctions(body)
    lost = sorted(before - keeps_set)
    preserved = sorted(before & keeps_set)

    projected: Any
    if isinstance(body, dict):
        projected = deepcopy(body)
        original_status = projected.get("status")
        if isinstance(original_status, str) and original_status in before and original_status not in keeps_set:
            projected["status"] = EMPTY
            projected["legacy_display"] = EMPTY
        if "distinctions" in projected and isinstance(projected["distinctions"], list):
            projected["distinctions"] = [item for item in projected["distinctions"] if item in keeps_set]
        if lost:
            projected.setdefault("discarded_distinctions", lost)
        projected["preserved_distinctions"] = preserved
    else:
        projected = body

    report = {
        "before_distinctions": sorted(before),
        "preserved_distinctions": preserved,
        "lost_distinctions": lost,
        "legacy_display": EMPTY if lost else None,
    }
    return projected, report


def owner_follow_up_required(body: Any, required: Iterable[str] | None = None) -> bool:
    """Whether the surviving semantics justify asking the data owner again."""

    distinctions = body_distinctions(body)
    required_set = set(normalize_distinctions(required))
    if UNKNOWN in distinctions and (not required_set or UNKNOWN in required_set | distinctions):
        return True
    if isinstance(body, dict) and body.get("status") == UNKNOWN:
        return True
    return False
