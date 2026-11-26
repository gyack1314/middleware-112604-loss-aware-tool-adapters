"""Purpose-aware service and transformation path selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import AdapterDefinition, DefinitionRegistry, ToolDefinition


MAX_PATH_HOPS = 8
MAX_DIAGNOSTIC_PATHS = 5


@dataclass
class PlanBlocker:
    kind: str  # semantic | budget | endpoint
    code: str
    message: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {"kind": self.kind, "code": self.code, "message": self.message}
        if self.details:
            result.update(self.details)
        return result


class PathPlanner:
    def __init__(self, registry: DefinitionRegistry) -> None:
        self.registry = registry

    def plan(
        self,
        request_rep: str,
        target_rep: str,
        required: list[str] | set[str] | tuple[str, ...],
        budget: int,
        at: int,
        scope: Any = "*",
        tool_id: str | None = None,
    ) -> dict[str, Any]:
        required_set = set(required)
        tools, tool_diagnostics = self.registry.matching_tools(
            request_rep, at=at, scope=scope, tool_id=tool_id
        )

        routes: list[dict[str, Any]] = []
        for tool in tools:
            for adapters in self._enumerate_adapter_paths(tool.output_rep, target_rep, at, scope):
                missing_by_hop: list[list[str]] = []
                missing_union: set[str] = set()
                for adapter in adapters:
                    missing = sorted(required_set - adapter.keeps)
                    missing_by_hop.append(missing)
                    missing_union.update(missing)
                cost = sum(item.cost for item in adapters)
                routes.append(
                    {
                        "tool": tool,
                        "adapters": adapters,
                        "cost": cost,
                        "semantic_valid": not missing_union,
                        "missing": sorted(missing_union),
                        "missing_by_hop": missing_by_hop,
                    }
                )

        feasible = [route for route in routes if route["semantic_valid"] and route["cost"] <= budget]
        feasible.sort(key=lambda route: (route["cost"], _route_key(route)))
        if feasible:
            return self._plan_dict(feasible[0], required_set, budget, at, scope, True, request_rep, target_rep)

        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        semantic_routes = [route for route in routes if route["semantic_valid"]]
        semantic_routes.sort(key=lambda route: (route["cost"], _route_key(route)))
        if semantic_routes:
            cheapest = semantic_routes[0]
            blockers.append(
                PlanBlocker(
                    kind="budget",
                    code="conversion_budget_exceeded",
                    message=(
                        f"符合全部语义要求的最低转换成本为 {cheapest['cost']}，"
                        f"超过本次预算 {budget}"
                    ),
                    details={
                        "cheapest_cost": cheapest["cost"],
                        "budget": budget,
                        "over_by": max(0, cheapest["cost"] - budget),
                        "path": _route_uids(cheapest),
                    },
                ).to_dict()
            )

        lossy_routes = [route for route in routes if not route["semantic_valid"]]
        lossy_routes.sort(key=lambda route: (len(route["missing"]), route["cost"], _route_key(route)))
        if lossy_routes and not semantic_routes:
            best = lossy_routes[0]
            edge_details = []
            for adapter, missing in zip(best["adapters"], best["missing_by_hop"]):
                if missing:
                    edge_details.append(
                        {
                            "edge": adapter.edge_id,
                            "version": adapter.version,
                            "uid": adapter.uid,
                            "input": adapter.input_rep,
                            "output": adapter.output_rep,
                            "missing": missing,
                            "keeps": sorted(adapter.keeps),
                        }
                    )
            blockers.append(
                PlanBlocker(
                    kind="semantic",
                    code="missing_required_distinctions",
                    message=(
                        "没有任何转换路径在每一段后都保留必需区分；缺少："
                        + ", ".join(best["missing"])
                    ),
                    details={
                        "missing": best["missing"],
                        "first_blocking_edges": edge_details,
                        "example_path": _route_uids(best),
                        "example_cost": best["cost"],
                    },
                ).to_dict()
            )
        elif lossy_routes and semantic_routes:
            cheapest_lossy = lossy_routes[0]
            warnings.append(
                {
                    "code": "lossy_alternative_ignored",
                    "message": "存在更便宜但会丢失业务区分的路径，不能用于当前用途",
                    "missing": cheapest_lossy["missing"],
                    "path": _route_uids(cheapest_lossy),
                    "cost": cheapest_lossy["cost"],
                }
            )

        if not routes:
            if tool_diagnostics:
                blockers.extend(tool_diagnostics)
            else:
                endpoint = self._endpoint_diagnostic(
                    request_rep, target_rep, at, scope, tool_id, tools
                )
                blockers.append(endpoint)

        # If there are routes but all are lossy, semantic blocker already exists.
        # Preserve structured no-route details when there are zero routes.
        result = self._plan_dict(None, required_set, budget, at, scope, False, request_rep, target_rep)
        result["blockers"] = blockers
        result["semantic_warnings"] = warnings
        result["considered_routes"] = [
            {
                "path": _route_uids(route),
                "cost": route["cost"],
                "semantic_valid": route["semantic_valid"],
                "missing": route["missing"],
            }
            for route in routes[:MAX_DIAGNOSTIC_PATHS]
        ]
        return result

    def plan_adapter_route(
        self,
        start_rep: str,
        target_rep: str,
        required: list[str] | set[str] | tuple[str, ...],
        budget: int,
        at: int,
        scope: Any = "*",
    ) -> dict[str, Any]:
        """Plan only adapter hops from a representation already produced."""

        required_set = set(required)
        routes: list[dict[str, Any]] = []
        for adapters in self._enumerate_adapter_paths(start_rep, target_rep, at, scope):
            missing_union: set[str] = set()
            missing_by_hop: list[list[str]] = []
            for adapter in adapters:
                missing = sorted(required_set - adapter.keeps)
                missing_by_hop.append(missing)
                missing_union.update(missing)
            routes.append(
                {
                    "tool": None,
                    "adapters": adapters,
                    "cost": sum(item.cost for item in adapters),
                    "semantic_valid": not missing_union,
                    "missing": sorted(missing_union),
                    "missing_by_hop": missing_by_hop,
                }
            )

        feasible = [route for route in routes if route["semantic_valid"] and route["cost"] <= budget]
        feasible.sort(key=lambda route: (route["cost"], _route_key(route)))
        if feasible:
            return self._plan_dict(feasible[0], required_set, budget, at, scope, True, start_rep, target_rep)

        blockers: list[dict[str, Any]] = []
        semantic_routes = sorted(
            (route for route in routes if route["semantic_valid"]),
            key=lambda route: (route["cost"], _route_key(route)),
        )
        if semantic_routes:
            cheapest = semantic_routes[0]
            blockers.append(
                PlanBlocker(
                    "budget",
                    "remaining_conversion_budget_exceeded",
                    f"保留已完成转换后，剩余路径最低成本 {cheapest['cost']} 超过可用预算 {budget}",
                    {
                        "cheapest_cost": cheapest["cost"],
                        "remaining_budget": budget,
                        "over_by": max(0, cheapest["cost"] - budget),
                        "path": _route_uids(cheapest),
                    },
                ).to_dict()
            )
        else:
            lossy_routes = sorted(
                (route for route in routes if not route["semantic_valid"]),
                key=lambda route: (len(route["missing"]), route["cost"], _route_key(route)),
            )
            if lossy_routes:
                best = lossy_routes[0]
                blockers.append(
                    PlanBlocker(
                        "semantic",
                        "missing_required_distinctions_on_replan",
                        "替代转换路径仍缺少必需区分：" + ", ".join(best["missing"]),
                        {"missing": best["missing"], "path": _route_uids(best)},
                    ).to_dict()
                )
            else:
                blockers.append(
                    PlanBlocker(
                        "endpoint",
                        "no_adapter_route_from_current_representation",
                        f"没有可用转换端点路径可从 {start_rep} 到达 {target_rep}",
                        {"start_rep": start_rep, "target_rep": target_rep, "at": at, "scope": scope},
                    ).to_dict()
                )

        result = self._plan_dict(None, required_set, budget, at, scope, False, start_rep, target_rep)
        result.update(
            {
                "request_rep": start_rep,
                "start_rep": start_rep,
                "target_rep": target_rep,
                "blockers": blockers,
                "considered_routes": [
                    {"path": _route_uids(route), "cost": route["cost"], "missing": route["missing"]}
                    for route in routes[:MAX_DIAGNOSTIC_PATHS]
                ],
            }
        )
        return result

    # ------------------------------------------------------------------
    def _enumerate_adapter_paths(
        self, start: str, target: str, at: int, scope: Any
    ) -> list[list[AdapterDefinition]]:
        edges = [
            adapter
            for adapter in self.registry.active_adapters(at=at)
            if adapter.supports_scope(scope)
        ]
        outgoing: dict[str, list[AdapterDefinition]] = {}
        for edge in edges:
            outgoing.setdefault(edge.input_rep, []).append(edge)
        for choices in outgoing.values():
            choices.sort(key=lambda item: (item.cost, item.edge_id, item.version))

        paths: list[list[AdapterDefinition]] = []

        def visit(node: str, path: list[AdapterDefinition], seen: set[str]) -> None:
            if node == target:
                paths.append(list(path))
                return
            if len(path) >= MAX_PATH_HOPS:
                return
            for edge in outgoing.get(node, []):
                next_node = edge.output_rep
                state_key = f"{edge.uid}:{next_node}"
                if state_key in seen:
                    continue
                # Representation-level cycle guard.
                if next_node in seen and next_node != target:
                    continue
                path.append(edge)
                seen.add(state_key)
                seen.add(next_node)
                visit(next_node, path, seen)
                seen.discard(next_node)
                seen.remove(state_key)
                path.pop()

        visit(start, [], {start})
        paths.sort(key=lambda path: (sum(item.cost for item in path), len(path), _adapter_path_key(path)))
        return paths

    def _endpoint_diagnostic(
        self,
        request_rep: str,
        target_rep: str,
        at: int,
        scope: Any,
        tool_id: str | None,
        tools: list[ToolDefinition],
    ) -> dict[str, Any]:
        if tool_id and self.registry.latest_tool(tool_id) is None:
            return PlanBlocker(
                "endpoint",
                "undeclared_tool",
                f"工具端点 {tool_id} 尚未声明",
                {"tool": tool_id},
            ).to_dict()

        matching_request = [
            tool
            for versions in self.registry.tools.values()
            for tool in versions
            if tool.request_rep == request_rep
            and (tool_id is None or tool.tool_id == tool_id)
        ]
        if not matching_request:
            return PlanBlocker(
                "endpoint",
                "no_provider_for_request_representation",
                f"没有工具端点接受请求表示 {request_rep}",
                {"request_rep": request_rep, "target_rep": target_rep},
            ).to_dict()

        outputs = sorted({tool.output_rep for tool in matching_request})
        graph_edges = list(self.registry.adapters.values())
        flat_edges = [edge for versions in graph_edges for edge in versions]
        reachable = _representation_reachability(outputs, flat_edges, target_rep)
        revoked = [
            edge.uid
            for edge in flat_edges
            if edge.revoked_at is not None and (edge.input_rep in outputs or True)
        ]
        return PlanBlocker(
            "endpoint",
            "no_route_to_target_representation",
            f"没有可用转换端点路径可从 {outputs} 到达 {target_rep}",
            {
                "tool_outputs": outputs,
                "target_rep": target_rep,
                "reachable_representations": reachable,
                "revoked_edges": revoked,
                "scope": scope,
                "at": at,
            },
        ).to_dict()

    def _plan_dict(
        self,
        route: dict[str, Any] | None,
        required: set[str],
        budget: int,
        at: int,
        scope: Any,
        feasible: bool,
        request_rep: str | None = None,
        target_rep: str | None = None,
    ) -> dict[str, Any]:
        if route is None:
            return {
                "feasible": False,
                "request_rep": request_rep,
                "target_rep": target_rep,
                "required_distinctions": sorted(required),
                "budget": budget,
                "at": at,
                "scope": scope,
                "cost": None,
                "tool": None,
                "hops": [],
                "blockers": [],
            }

        tool: ToolDefinition | None = route["tool"]
        if tool is not None:
            hops = [
                {
                    "kind": "tool",
                    "service": tool.provider or tool.tool_id,
                    "tool": tool.tool_id,
                    "version": tool.version,
                    "uid": tool.uid,
                    "input": tool.request_rep,
                    "output": tool.output_rep,
                    "cost": 0,
                    "endpoint": tool.endpoint,
                }
            ]
            request_rep = tool.request_rep
            tool_summary = {
                "tool": tool.tool_id,
                "version": tool.version,
                "uid": tool.uid,
                "output_rep": tool.output_rep,
                "provider": tool.provider,
            }
        else:
            hops = []
            tool_summary = None
        for adapter in route["adapters"]:
            hops.append(
                {
                    "kind": "adapter",
                    "service": adapter.provider or adapter.edge_id,
                    "edge": adapter.edge_id,
                    "version": adapter.version,
                    "uid": adapter.uid,
                    "input": adapter.input_rep,
                    "output": adapter.output_rep,
                    "keeps": sorted(adapter.keeps),
                    "cost": adapter.cost,
                    "endpoint": adapter.endpoint,
                    "provable": adapter.provable,
                }
            )
        return {
            "feasible": feasible,
            "request_rep": request_rep,
            "target_rep": target_rep,
            "required_distinctions": sorted(required),
            "budget": budget,
            "at": at,
            "scope": scope,
            "cost": route["cost"],
            "tool": tool_summary,
            "hops": hops,
            "blockers": [],
        }


def _route_key(route: dict[str, Any]) -> str:
    tool_part = route["tool"].uid if route.get("tool") is not None else "representation"
    return tool_part + "|" + ",".join(adapter.uid for adapter in route["adapters"])


def _route_uids(route: dict[str, Any]) -> list[str]:
    prefix = [route["tool"].uid] if route.get("tool") is not None else []
    return prefix + [adapter.uid for adapter in route["adapters"]]


def _adapter_path_key(path: list[AdapterDefinition]) -> str:
    return ",".join(item.uid for item in path)


def _representation_reachability(
    starts: list[str], edges: list[AdapterDefinition], target: str
) -> list[str]:
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge.input_rep, []).append(edge.output_rep)
    seen = set(starts)
    stack = list(starts)
    while stack:
        node = stack.pop()
        for nxt in outgoing.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return sorted(seen)
