"""Small serialization, hashing and declaration-matching helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

JSONLike = dict[str, Any] | list[Any] | str | int | float | bool | None


def canonical_json(value: Any) -> str:
    """Return deterministic JSON used for hashes and proofs."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def sha256_text(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def short_hash(value: Any, length: int = 12) -> str:
    return sha256_text(value)[:length]


def normalize_scope(scope: Any) -> JSONLike:
    """Normalize a data scope.

    Supported forms are intentionally simple and JSON friendly:

    * ``"all"`` / ``"*"``;
    * a label string;
    * a list/tuple/set of labels;
    * a mapping of constraints.
    """

    if scope is None:
        return "default"
    if isinstance(scope, (list, tuple, set, frozenset)):
        labels = sorted(str(item) for item in scope)
        if labels == ["*"] or labels == ["all"]:
            return "*"
        return labels
    if isinstance(scope, Mapping):
        return {str(key): _normalize_scope_value(item) for key, item in sorted(scope.items())}
    return str(scope)


def _normalize_scope_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(str(item) for item in value)
    return value


def scope_supported(declared: Any, requested: Any) -> bool:
    """Return whether *requested* scope is covered by a provider declaration."""

    declared_norm = normalize_scope(declared if declared is not None else "*")
    requested_norm = normalize_scope(requested)

    if declared_norm == "*" or declared_norm == "all":
        return True
    if requested_norm == "*":
        return False

    if isinstance(declared_norm, dict):
        if not isinstance(requested_norm, dict):
            return False
        for key, allowed in declared_norm.items():
            if key not in requested_norm:
                return False
            wanted = requested_norm[key]
            if isinstance(allowed, list):
                allowed_values = {str(item) for item in allowed}
                if isinstance(wanted, list):
                    if not all(str(item) in allowed_values for item in wanted):
                        return False
                elif str(wanted) not in allowed_values:
                    return False
            elif wanted != allowed:
                return False
        return True

    declared_labels = _as_label_set(declared_norm)
    requested_labels = _as_label_set(requested_norm)
    return requested_labels.issubset(declared_labels)


def _as_label_set(scope: JSONLike) -> set[str]:
    if isinstance(scope, list):
        return {str(item) for item in scope}
    return {str(scope)}


def window_allows(window: Any, at: int | None) -> bool:
    """Check an availability/usage window.

    A window may be ``{"start": s, "end": e}``, ``[s, e]`` or ``None``.  End is
    exclusive.  Open ended windows can use ``None``.
    """

    if window is None:
        return True
    if at is None:
        return True

    start = end = None
    if isinstance(window, Mapping):
        start = window.get("start", window.get("from", window.get("available_from")))
        end = window.get("end", window.get("until", window.get("available_to")))
    elif isinstance(window, (list, tuple)) and len(window) == 2:
        start, end = window

    if start is not None and at < int(start):
        return False
    if end is not None and at >= int(end):
        return False
    return True


def redact(record: Mapping[str, Any], keys: set[str] | None = None) -> dict[str, Any]:
    """Return a copy without large/raw fields used in compact inspect views."""

    blocked = keys or {"raw"}
    return {key: value for key, value in record.items() if key not in blocked}
