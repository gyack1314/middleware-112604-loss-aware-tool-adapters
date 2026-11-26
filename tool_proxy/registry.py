"""Versioned declarations for tool providers and transformation adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .util import normalize_scope, scope_supported, window_allows


@dataclass
class ToolDefinition:
    tool_id: str
    version: int
    request_rep: str
    output_rep: str
    at: int = 0
    provider: str | None = None
    endpoint: str | None = None
    available: bool = True
    data_scope: Any = "*"
    window: Any = None
    usage_window: Any = None
    active: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"tool:{self.tool_id}@v{self.version}"

    def available_at(self, at: int | None) -> bool:
        return self.active and self.available and window_allows(self.window, at)

    def supports_scope(self, scope: Any) -> bool:
        return scope_supported(self.data_scope, scope)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["uid"] = self.uid
        return data


@dataclass
class AdapterDefinition:
    edge_id: str
    version: int
    input_rep: str
    output_rep: str
    keeps: frozenset[str]
    cost: int
    at: int = 0
    provider: str | None = None
    endpoint: str | None = None
    active: bool = True
    data_scope: Any = "*"
    window: Any = None
    authorization: dict[str, Any] | None = None
    lawful_basis: str | None = None
    provable: bool = False
    revoked_at: int | None = None
    revocation_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.edge_id}@v{self.version}"

    def available_at(self, at: int | None) -> bool:
        if not self.active or self.revoked_at is not None:
            return False
        return window_allows(self.window, at)

    def supports_scope(self, scope: Any) -> bool:
        return scope_supported(self.data_scope, scope)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["keeps"] = sorted(self.keeps)
        data["uid"] = self.uid
        return data


class DefinitionRegistry:
    """Append-only versioned definition registry.

    Re-registering an identifier creates a new version instead of mutating the
    old declaration.  Revocation likewise records history, which is necessary
    when deciding whether already completed transformations were lawful.
    """

    def __init__(self) -> None:
        self.tools: dict[str, list[ToolDefinition]] = {}
        self.adapters: dict[str, list[AdapterDefinition]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register_tool(self, record: dict[str, Any], at: int = 0) -> ToolDefinition:
        tool_id = str(record.get("tool") or record.get("id"))
        if not tool_id:
            raise ValueError("tool 声明缺少 tool")
        request_rep = str(record.get("request") or record.get("source") or record.get("input"))
        output_rep = str(record.get("output") or record.get("result") or record.get("output_rep"))
        if not request_rep or not output_rep:
            raise ValueError(f"tool {tool_id} 缺少 request/source 或 output/result")

        version = _optional_int(record.get("version")) or len(self.tools.get(tool_id, [])) + 1
        definition = ToolDefinition(
            tool_id=tool_id,
            version=version,
            request_rep=request_rep,
            output_rep=output_rep,
            at=at,
            provider=record.get("provider") or record.get("service"),
            endpoint=record.get("endpoint"),
            available=bool(record.get("available", True)),
            data_scope=normalize_scope(
                record.get("data_scope", record.get("scopes", record.get("scope", "*")))
            ),
            window=record.get("window", record.get("available_window")),
            usage_window=record.get("usage_window"),
            active=bool(record.get("active", True)),
            raw=dict(record),
        )
        self.tools.setdefault(tool_id, []).append(definition)
        return definition

    def register_adapter(self, record: dict[str, Any], at: int = 0) -> AdapterDefinition:
        edge_id = str(record.get("edge") or record.get("id"))
        if not edge_id:
            raise ValueError("adapter 声明缺少 edge")
        input_rep = str(record.get("input"))
        output_rep = str(record.get("output"))
        if not input_rep or not output_rep:
            raise ValueError(f"adapter {edge_id} 缺少 input/output")
        if "cost" not in record:
            raise ValueError(f"adapter {edge_id} 缺少 cost")
        cost = int(record["cost"])
        if cost < 0:
            raise ValueError(f"adapter {edge_id} cost 不能为负")

        keeps = frozenset(str(item) for item in record.get("keeps", []))
        version = _optional_int(record.get("version")) or len(self.adapters.get(edge_id, [])) + 1

        authorization = record.get("authorization") or record.get("permission") or record.get("license")
        if not isinstance(authorization, dict) and authorization is not None:
            authorization = {"grant_id": str(authorization)}
        lawful_basis = record.get("lawful_basis") or record.get("basis")
        provable = record.get("provable")
        if provable is None:
            provable = bool(authorization or lawful_basis)
        else:
            provable = bool(provable)

        definition = AdapterDefinition(
            edge_id=edge_id,
            version=version,
            input_rep=input_rep,
            output_rep=output_rep,
            keeps=keeps,
            cost=cost,
            at=at,
            provider=record.get("provider") or record.get("service") or edge_id,
            endpoint=record.get("endpoint"),
            active=bool(record.get("active", True)),
            data_scope=normalize_scope(
                record.get("data_scope", record.get("scopes", record.get("scope", "*")))
            ),
            window=record.get("window", record.get("available_window")),
            authorization=authorization,
            lawful_basis=lawful_basis,
            provable=provable,
            raw=dict(record),
        )
        self.adapters.setdefault(edge_id, []).append(definition)
        return definition

    # ------------------------------------------------------------------
    # Lookup and revocation
    # ------------------------------------------------------------------
    def tool_versions(self, tool_id: str) -> list[ToolDefinition]:
        return list(self.tools.get(tool_id, []))

    def latest_tool(self, tool_id: str) -> ToolDefinition | None:
        versions = self.tools.get(tool_id)
        return versions[-1] if versions else None

    def adapter_versions(self, edge_id: str) -> list[AdapterDefinition]:
        return list(self.adapters.get(edge_id, []))

    def get_adapter(self, edge_id: str, version: int | None = None) -> AdapterDefinition | None:
        versions = self.adapters.get(edge_id, [])
        if not versions:
            return None
        if version is None:
            return versions[-1]
        for item in versions:
            if item.version == version:
                return item
        return None

    def get_adapter_uid(self, uid: str) -> AdapterDefinition | None:
        if "@v" not in uid:
            return self.get_adapter(uid)
        edge_id, raw_version = uid.rsplit("@v", 1)
        try:
            version = int(raw_version)
        except ValueError:
            return None
        return self.get_adapter(edge_id, version)

    def active_adapters(self, at: int | None = None, scope: Any = None) -> list[AdapterDefinition]:
        result: list[AdapterDefinition] = []
        for versions in self.adapters.values():
            for definition in versions:
                if not definition.available_at(at):
                    continue
                if scope is not None and not definition.supports_scope(scope):
                    continue
                result.append(definition)
        return result

    def matching_tools(
        self,
        request_rep: str,
        at: int | None = None,
        scope: Any = None,
        tool_id: str | None = None,
    ) -> tuple[list[ToolDefinition], list[dict[str, Any]]]:
        candidates: list[ToolDefinition] = []
        diagnostics: list[dict[str, Any]] = []
        all_tools: list[ToolDefinition] = []
        for versions in self.tools.values():
            all_tools.extend(versions)

        for tool in all_tools:
            if tool_id is not None and tool.tool_id != tool_id:
                continue
            if tool.request_rep != request_rep:
                continue
            reasons: list[str] = []
            if not tool.active or not tool.available:
                reasons.append("inactive")
            if not window_allows(tool.window, at):
                reasons.append("outside_available_window")
            if scope is not None and not tool.supports_scope(scope):
                reasons.append("data_scope_not_supported")
            if reasons:
                diagnostics.append(
                    {
                        "kind": "endpoint",
                        "tool": tool.tool_id,
                        "version": tool.version,
                        "reasons": reasons,
                        "detail": f"工具 {tool.tool_id} 端点当前不可用：{','.join(reasons)}",
                    }
                )
            else:
                candidates.append(tool)
        return candidates, diagnostics

    def withdraw_adapter(
        self, edge_id: str, at: int, version: int | None = None, reason: str | None = None
    ) -> list[AdapterDefinition]:
        versions = self.adapters.get(edge_id, [])
        changed: list[AdapterDefinition] = []
        for definition in versions:
            if version is not None and definition.version != version:
                continue
            if definition.revoked_at is None:
                definition.active = False
                definition.revoked_at = at
                definition.revocation_reason = reason or "permission_withdrawn"
                changed.append(definition)
        return changed

    def withdraw_tool(
        self, tool_id: str, at: int, version: int | None = None, reason: str | None = None
    ) -> list[ToolDefinition]:
        changed: list[ToolDefinition] = []
        for definition in self.tools.get(tool_id, []):
            if version is not None and definition.version != version:
                continue
            if definition.active:
                definition.active = False
                definition.raw.setdefault("revoked_at", at)
                definition.raw["revocation_reason"] = reason or "permission_withdrawn"
                changed.append(definition)
        return changed

    def snapshot(self) -> dict[str, Any]:
        return {
            "tools": [tool.to_dict() for versions in self.tools.values() for tool in versions],
            "adapters": [
                adapter.to_dict()
                for versions in self.adapters.values()
                for adapter in versions
            ],
        }


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
