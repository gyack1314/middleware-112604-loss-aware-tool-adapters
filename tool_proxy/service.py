"""Application service: requests, attempts, representations, citations and recovery."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .planner import PathPlanner
from .registry import DefinitionRegistry
from .semantics import (
    ABSENT,
    CORE_DISTINCTIONS,
    UNKNOWN,
    body_distinctions,
    owner_follow_up_required,
    project_body,
    requirements_for,
)
from .util import canonical_json, sha256_text, normalize_scope, scope_supported

SUPPORTED_KINDS = {
    "accepted",
    "adapter",
    "cite",
    "deliver",
    "inspect",
    "query",
    "reply",
    "restart",
    "tool",
    "transform",
    "withdraw",
}

FORMAL_PURPOSE_HINT = {"formal", "proofread", "citation", "cite", "正式", "正式校对", "引用"}


class ToolProxy:
    """Stateful proxy.

    The service is event-sourced: every input message is retained in ``events``
    and can be replayed to recover requests, actual attempts, data scopes and
    customers after a restart.
    """

    def __init__(
        self,
        registry: DefinitionRegistry | None = None,
        store: Any | None = None,
        auto_transform: bool = True,
        auto_deliver: bool = True,
    ) -> None:
        self.registry = registry or DefinitionRegistry()
        self.planner = PathPlanner(self.registry)
        self.store = store
        self.auto_transform = auto_transform
        self.auto_deliver = auto_deliver

        self.events: list[dict[str, Any]] = []
        self.apps: dict[str, dict[str, Any]] = {}
        self.attempt_index: dict[str, str] = {}
        self.citations: dict[str, dict[str, Any]] = {}
        self.citation_ref_index: dict[str, str] = {}
        self.citation_count = 0

    # ------------------------------------------------------------------
    # Event entry points and recovery
    # ------------------------------------------------------------------
    def handle(self, message: dict[str, Any]) -> Any:
        """Append and apply one scenario/input message."""

        self.events.append(message)
        return self.apply(message)

    def dispatch(self, message: dict[str, Any]) -> Any:
        """Handle a message and durably append it when a store is configured."""

        result = self.handle(message)
        if self.store is not None:
            self.store.append(message)
        return result

    def apply(self, message: dict[str, Any]) -> Any:
        kind = message.get("kind")
        at = int(message.get("at", 0))
        record = message.get("record") or {}
        if not isinstance(record, dict):
            raise ValueError("record 应为对象")

        if kind == "adapter":
            return self._on_adapter(at, record)
        if kind == "tool":
            return self._on_tool(at, record)
        if kind == "query":
            return self._on_query(at, record)
        if kind == "accepted":
            return self._on_accepted(at, record)
        if kind == "reply":
            return self._on_reply(at, record)
        if kind == "withdraw":
            return self._on_withdraw(at, record)
        if kind == "cite":
            return self._on_cite(at, record)
        if kind == "deliver":
            return self._on_deliver_event(at, record)
        if kind == "transform":
            return self._on_transform_event(at, record)
        if kind == "inspect":
            return self.query_entry(record)
        if kind == "restart":
            return {"restarted": True, "at": at}
        raise ValueError(f"未登记的消息名称: {kind}")

    def recover_in_place(self) -> "ToolProxy":
        """Replay retained events into fresh indexes and replace this state."""

        recovered = ToolProxy(
            store=self.store,
            auto_transform=self.auto_transform,
            auto_deliver=self.auto_deliver,
        )
        events = list(self.events)
        for message in events:
            recovered.apply(message)
        recovered.events = events
        self.__dict__.update(recovered.__dict__)
        return self

    def restart_now(self, at: int = 0) -> dict[str, Any]:
        """Append a restart marker, persist it when possible, and replay."""

        message = {"at": at, "kind": "restart", "record": {}}
        if self.store is not None:
            self.dispatch(message)
        else:
            self.handle(message)
        self.recover_in_place()
        return {"restarted": True, "at": at, "application_ids": sorted(self.apps)}

    @classmethod
    def from_events(cls, events: list[dict[str, Any]], **options: Any) -> "ToolProxy":
        proxy = cls(**options)
        for message in events:
            proxy.apply(message)
        proxy.events = list(events)
        return proxy

    @classmethod
    def recover_from_store(cls, store: Any, **options: Any) -> "ToolProxy":
        messages = store.messages() if hasattr(store, "messages") else list(store)
        proxy = cls(store=store, **options)
        for message in messages:
            proxy.apply(message)
        proxy.events = list(messages)
        return proxy

    # ------------------------------------------------------------------
    # Definitions
    # ------------------------------------------------------------------
    def _on_adapter(self, at: int, record: dict[str, Any]) -> dict[str, Any]:
        definition = self.registry.register_adapter(record, at=at)
        affected = self._replan_waiting_apps(at, reason="adapter_definition_changed")
        return {"adapter": definition.uid, "replanned_applications": affected}

    def _on_tool(self, at: int, record: dict[str, Any]) -> dict[str, Any]:
        definition = self.registry.register_tool(record, at=at)
        affected = self._replan_waiting_apps(at, reason="tool_definition_changed")
        return {"tool": definition.uid, "replanned_applications": affected}

    # ------------------------------------------------------------------
    # Query/application
    # ------------------------------------------------------------------
    def _on_query(self, at: int, record: dict[str, Any]) -> dict[str, Any]:
        app_id = str(record.get("id") or record.get("application_id"))
        if not app_id:
            raise ValueError("query 缺少 id")
        if app_id in self.apps:
            existing = self.apps[app_id]
            if canonical_json(existing["request_record"]) != canonical_json(record):
                raise ValueError(f"申请 {app_id} 已存在且请求内容冲突")
            return {"id": app_id, "idempotent": True}

        purpose = record.get("purpose", record.get("use"))
        formal, required = requirements_for(purpose, record.get("need", []))
        scope = normalize_scope(record.get("data_scope", record.get("scope", "default")))
        budget = int(record.get("budget", 0))
        if budget < 0:
            raise ValueError("budget 不能为负")

        app: dict[str, Any] = {
            "id": app_id,
            "customer": record.get("customer") or record.get("client") or record.get("tenant") or "workbench",
            "purpose": str(purpose or ("proofread" if formal else "summary")),
            "formal_proofread_requested": formal,
            "required_distinctions": required,
            "source_rep": str(record.get("source")),
            "target_rep": str(record.get("target")),
            "data_scope": scope,
            "budget_limit": budget,
            "budget": {
                "limit": budget,
                "planned_cost": None,
                "hold_cost": 0,
                "actual_cost": 0,
                "released_unused_hold": 0,
                "settled": False,
            },
            "state": "created",
            "state_reason": None,
            "active_plan_version": 0,
            "active_plan": None,
            "plan_history": [],
            "segments": [],
            "segment_archive": [],
            "attempts": [],
            "active_attempt_id": None,
            "actual_tool_id": record.get("tool"),
            "original_representations": [],
            "authoritative_original_id": None,
            "working_representation": None,
            "deliveries": [],
            "current_delivery_id": None,
            "citation_ids": [],
            "usage_facts": [],
            "review_issues": [],
            "receipt_conflicts": [],
            "timeline": [],
            "auto_transform": bool(record.get("auto_transform", self.auto_transform)),
            "auto_deliver": bool(record.get("auto_deliver", self.auto_deliver)),
            "created_at": at,
            "updated_at": at,
            "request_record": deepcopy(record),
            "recovery": {
                "data_scope": scope,
                "customer": record.get("customer") or record.get("client") or record.get("tenant") or "workbench",
                "actual_attempt_id": None,
            },
        }
        self.apps[app_id] = app
        self._append_timeline(app, at, "query_received", {"budget": budget, "purpose": app["purpose"]})
        self._create_plan_version(app, at, reason="query", tool_id=app["actual_tool_id"])
        return self.query_application(app_id)

    def _create_plan_version(
        self,
        app: dict[str, Any],
        at: int,
        reason: str,
        tool_id: str | None = None,
        start_rep: str | None = None,
        available_budget: int | None = None,
        preserve_prefix: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        version = app["active_plan_version"] + 1
        preserve_prefix = preserve_prefix or []
        prefix_cost = sum(int(segment["cost"]) for segment in preserve_prefix if segment.get("status") == "completed")
        sunk_cost = int(app.get("actual_cost", app["budget"].get("actual_cost", 0)))
        budget_for_new = app["budget_limit"] - sunk_cost if available_budget is None else available_budget

        if start_rep is None:
            plan = self.planner.plan(
                request_rep=app["source_rep"],
                target_rep=app["target_rep"],
                required=app["required_distinctions"],
                budget=app["budget_limit"],
                at=at,
                scope=app["data_scope"],
                tool_id=tool_id or app.get("actual_tool_id"),
            )
            prefix_hops: list[dict[str, Any]] = []
            new_hops = [hop for hop in plan.get("hops", []) if hop.get("kind") == "adapter"]
            tool_hop = next((hop for hop in plan.get("hops", []) if hop.get("kind") == "tool"), None)
        else:
            suffix = self.planner.plan_adapter_route(
                start_rep=start_rep,
                target_rep=app["target_rep"],
                required=app["required_distinctions"],
                budget=max(0, budget_for_new),
                at=at,
                scope=app["data_scope"],
            )
            plan = suffix
            tool_hop = self._tool_hop_for_active_attempt(app)
            prefix_hops = [self._segment_to_hop(segment) for segment in preserve_prefix]
            new_hops = [hop for hop in suffix.get("hops", []) if hop.get("kind") == "adapter"]
            plan["tool"] = tool_hop
            plan["hops"] = ([tool_hop] if tool_hop else []) + prefix_hops + new_hops
            plan["prefix_cost"] = prefix_cost
            plan["new_cost"] = suffix.get("cost")
            plan["sunk_cost"] = sunk_cost
            plan["total_budget_need"] = sunk_cost + int(suffix.get("cost") or 0)

        app["active_plan_version"] = version
        app["active_plan"] = plan
        app["plan_history"].append(
            {"version": version, "at": at, "reason": reason, "plan": deepcopy(plan)}
        )

        if plan.get("feasible"):
            new_segments = self._segments_from_hops(new_hops, version, start_index=len(preserve_prefix))
            app["segments"] = deepcopy(preserve_prefix) + new_segments
            app["blockers"] = []
            app["state"] = "planned" if not app["attempts"] else app.get("state", "accepted")
            app["state_reason"] = None
            planned_cost = int(plan.get("cost") or 0)
            app["budget"]["planned_cost"] = planned_cost
            if app["active_attempt_id"]:
                # Reservations never decrease before an endpoint terminal state.
                needed_hold = max(app["budget"]["hold_cost"], sunk_cost + int(plan.get("new_cost", planned_cost) if start_rep else planned_cost))
                app["budget"]["hold_cost"] = min(app["budget_limit"], max(app["budget"]["hold_cost"], needed_hold))
        else:
            app["segments"] = []
            app["blockers"] = plan.get("blockers", [])
            if app.get("state") != "endpoint_unknown":
                app["state"] = "blocked"
                primary = app["blockers"][0] if app["blockers"] else {}
                app["state_reason"] = primary.get("message") or "no_feasible_path"
        app["updated_at"] = at
        return plan

    def _segments_from_hops(self, hops: list[dict[str, Any]], version: int, start_index: int = 0) -> list[dict[str, Any]]:
        segments = []
        for offset, hop in enumerate(hops):
            if hop.get("kind") != "adapter":
                continue
            segments.append(
                {
                    "segment_id": f"app-plan-v{version}-seg-{start_index + offset}",
                    "plan_version": version,
                    "index": start_index + offset,
                    "edge_id": hop.get("edge"),
                    "version": hop.get("version"),
                    "uid": hop.get("uid"),
                    "input_rep": hop.get("input"),
                    "output_rep": hop.get("output"),
                    "keeps": hop.get("keeps", []),
                    "cost": int(hop.get("cost", 0)),
                    "status": "planned",
                    "input_hash": None,
                    "output_hash": None,
                    "projected_body": None,
                    "semantic_report": None,
                    "transform_receipt_id": None,
                    "proof": None,
                    "legality_status": "not_executed",
                    "started_at": None,
                    "completed_at": None,
                    "actual_cost": None,
                    "note": None,
                }
            )
        return segments

    # ------------------------------------------------------------------
    # Accepted attempts and receipts
    # ------------------------------------------------------------------
    def _on_accepted(self, at: int, record: dict[str, Any]) -> dict[str, Any]:
        app_id = str(record.get("id") or record.get("application_id"))
        attempt_id = str(record.get("attempt"))
        tool_id = str(record.get("tool") or record.get("tool_id") or "")
        app = self._require_app(app_id)

        if attempt_id in self.attempt_index:
            return {"id": app_id, "attempt": attempt_id, "idempotent": True}
        self.attempt_index[attempt_id] = app_id

        attempt = {
            "attempt_id": attempt_id,
            "tool_id": tool_id,
            "accepted_at": at,
            "state": "accepted",
            "terminal": False,
            "endpoint_terminal": False,
            "receipts": [],
        }
        app["attempts"].append(attempt)
        app["active_attempt_id"] = attempt_id
        app["actual_tool_id"] = tool_id
        app["recovery"]["actual_attempt_id"] = attempt_id
        app["state"] = "accepted"
        app["state_reason"] = None

        # The actual accepted tool determines the first endpoint hop.
        self._create_plan_version(app, at, reason="accepted_actual_tool", tool_id=tool_id)
        plan = app["active_plan"]
        if plan.get("feasible"):
            app["budget"]["hold_cost"] = max(app["budget"]["hold_cost"], int(plan.get("cost") or 0))
        self._append_timeline(app, at, "attempt_accepted", {"attempt": attempt_id, "tool": tool_id})
        return {"id": app_id, "attempt": attempt_id, "plan_feasible": plan.get("feasible")}

    def _on_reply(self, at: int, record: dict[str, Any]) -> dict[str, Any]:
        attempt_id = str(record.get("attempt"))
        app_id = self.attempt_index.get(attempt_id)
        if not app_id:
            raise KeyError(f"回执引用了未知 attempt: {attempt_id}")
        app = self.apps[app_id]
        attempt = next(item for item in app["attempts"] if item["attempt_id"] == attempt_id)

        receipt = {
            "received_at": at,
            "state": record.get("state"),
            "body": record.get("body"),
            "raw_record": deepcopy(record),
            "late": attempt["terminal"],
        }
        attempt["receipts"].append(receipt)
        app["updated_at"] = at

        state = record.get("state")
        if state == "unknown":
            return self._handle_unknown_endpoint(app, attempt, receipt, at, record)
        if state in {"never", "failed", "error"}:
            attempt["state"] = "never" if state == "never" else str(state)
            attempt["terminal"] = True
            attempt["endpoint_terminal"] = True
            app["state"] = "tool_never" if state == "never" else "tool_failed"
            app["state_reason"] = record.get("reason") or f"execution endpoint reported {state}"
            self._append_timeline(app, at, "attempt_terminal_without_result", {"attempt": attempt_id, "state": state})
            self._settle_budget(app, at)
            return {"id": app_id, "attempt": attempt_id, "terminal": True, "representation_created": False}
        if state == "done":
            body = record.get("body")
            if body is None:
                issue = {
                    "code": "done_without_body",
                    "at": at,
                    "attempt": attempt_id,
                    "message": "done 回执缺少 body，不能替原结果造表示",
                }
                app["review_issues"].append(issue)
                app["state"] = "malformed_receipt"
                self._settle_budget(app, at)
                return {"error": "done_without_body", "id": app_id}

            attempt["state"] = "done"
            attempt["terminal"] = True
            attempt["endpoint_terminal"] = True
            app["state"] = "terminal_done"
            app["state_reason"] = None
            representation, duplicate = self._receive_original_representation(app, attempt, body, at)
            self._append_timeline(
                app,
                at,
                "terminal_reply",
                {"attempt": attempt_id, "representation": representation["id"], "duplicate": duplicate},
            )
            if not duplicate and app["receipt_conflicts"]:
                app["state"] = "receipt_conflict"
                app["state_reason"] = "同一申请收到内容不同的终态回执；已保留首份表示，等待核对"
                self._settle_budget(app, at)
                return {"id": app_id, "representation_id": representation["id"], "conflict": True}

            if not duplicate:
                self._after_terminal_result(app, at)
                if app.get("auto_transform", self.auto_transform) and app.get("state") in {
                    "blocked",
                    "budget_exceeded",
                    "failed",
                }:
                    self._settle_budget(app, at)
            elif not app.get("deliveries") and app.get("auto_transform", self.auto_transform):
                self._after_terminal_result(app, at)
            return {
                "id": app_id,
                "attempt": attempt_id,
                "representation_id": representation["id"],
                "duplicate": duplicate,
            }

        # Accepted/running/progress-style receipts remain non-terminal.
        attempt["state"] = str(state)
        app["state"] = "in_progress"
        return {"id": app_id, "attempt": attempt_id, "terminal": False}

    def _handle_unknown_endpoint(
        self, app: dict[str, Any], attempt: dict[str, Any], receipt: dict[str, Any], at: int, record: dict[str, Any]
    ) -> dict[str, Any]:
        explicitly_terminal = bool(record.get("terminal") or record.get("endpoint_terminal"))
        attempt["state"] = "endpoint_unknown"
        attempt["terminal"] = explicitly_terminal
        attempt["endpoint_terminal"] = explicitly_terminal
        app["state"] = "endpoint_unknown"
        app["state_reason"] = "拿不到执行端终态；不创建结果表示，预算保留，重启后继续按原 attempt 恢复"
        app["recovery"]["actual_attempt_id"] = attempt["attempt_id"]
        app["budget"]["settled"] = False
        self._append_timeline(app, at, "endpoint_state_unknown", {"attempt": attempt["attempt_id"]})

        # A late ambiguous status after a done result cannot rewrite that fact.
        if app["authoritative_original_id"]:
            issue = {
                "code": "late_ambiguous_receipt",
                "at": at,
                "attempt": attempt["attempt_id"],
                "message": "done 之后又收到 unknown 状态；原表示不被重写，新引用前应核对执行端",
            }
            app["review_issues"].append(issue)
        return {"id": app["id"], "terminal": False, "representation_created": False, "budget_released": False}

    def _receive_original_representation(
        self, app: dict[str, Any], attempt: dict[str, Any], body: Any, at: int
    ) -> tuple[dict[str, Any], bool]:
        content_hash = sha256_text(body)
        for existing in app["original_representations"]:
            if existing["content_hash"] == content_hash:
                existing["receipt_count"] = int(existing.get("receipt_count", 1)) + 1
                return existing, True

        tool_uid = None
        supported = list(CORE_DISTINCTIONS)
        tool_id = attempt.get("tool_id")
        tool = self.registry.latest_tool(tool_id) if tool_id else None
        if tool:
            tool_uid = tool.uid
            declared = tool.raw.get("distinctions") or tool.raw.get("supported_distinctions")
            if declared:
                supported = sorted(set(declared))

        version = len(app["original_representations"]) + 1
        representation = {
            "id": f"original:{app['id']}:v{version}",
            "kind": "original",
            "application_id": app["id"],
            "attempt_id": attempt["attempt_id"],
            "tool_uid": tool_uid,
            "representation_rep": tool.output_rep if tool else app["active_plan"].get("tool", {}).get("output_rep") if app.get("active_plan") else None,
            "body": deepcopy(body),
            "content_hash": content_hash,
            "business_status": body.get("status") if isinstance(body, dict) else None,
            "distinctions": sorted(body_distinctions(body)),
            "supported_distinctions": supported,
            "received_at": at,
            "receipt_count": 1,
            "status": "active",
            "citation_ids": [],
        }

        if app["original_representations"]:
            representation["status"] = "conflicting"
            conflict = {
                "code": "conflicting_terminal_receipts",
                "at": at,
                "attempt_id": attempt["attempt_id"],
                "representation_ids": [app["authoritative_original_id"], representation["id"]],
                "message": "迟到终态回执与已保留原表示内容不同；已提交引用的快照不可被重写",
            }
            app["receipt_conflicts"].append(conflict)
            app["review_issues"].append(conflict)
            for old in app["original_representations"]:
                old["status"] = "disputed"
        else:
            app["authoritative_original_id"] = representation["id"]
            app["working_representation"] = {
                "representation_rep": representation["representation_rep"],
                "body": deepcopy(body),
                "content_hash": content_hash,
                "source": representation["id"],
            }

        app["original_representations"].append(representation)
        return representation, False

    def _after_terminal_result(self, app: dict[str, Any], at: int) -> None:
        if app["receipt_conflicts"]:
            return
        # Ensure the path is based on the actual accepted tool and current registry.
        if not app.get("active_plan", {}).get("feasible"):
            self._create_plan_version(app, at, reason="terminal_result_replan", tool_id=app.get("actual_tool_id"))
        if app.get("auto_transform", self.auto_transform):
            self.run_transformations(app["id"], at=at)
        else:
            app["state"] = "awaiting_transform"

    # ------------------------------------------------------------------
    # Transformations, proofs and delivery
    # ------------------------------------------------------------------
    def run_transformations(self, app_id: str, at: int) -> dict[str, Any]:
        app = self._require_app(app_id)
        if not app.get("authoritative_original_id") or not app.get("working_representation"):
            return {"ran": 0, "delivered": False, "reason": "no_terminal_original_representation"}

        ran = 0
        for segment in app["segments"]:
            if segment["status"] == "completed":
                continue
            if segment["status"] not in {"planned", "started"}:
                continue

            definition = self.registry.get_adapter(segment["edge_id"], segment["version"])
            if definition is None or not definition.available_at(at):
                segment["status"] = "revoked"
                segment["note"] = "转换端点在执行时不可用"
                self._create_plan_version(
                    app,
                    at,
                    reason="segment_unavailable_replan",
                    start_rep=self._last_good_representation(app),
                    preserve_prefix=self._good_prefix(app),
                )
                return self.run_transformations(app_id, at)

            cost = int(segment["cost"])
            if app["budget_limit"] - app["budget"]["actual_cost"] < cost:
                segment["status"] = "failed"
                segment["note"] = "actual conversion cost would exceed request budget"
                app["state"] = "budget_exceeded"
                app["state_reason"] = segment["note"]
                return {"ran": ran, "delivered": False, "reason": "budget_exceeded"}

            segment["status"] = "started"
            segment["started_at"] = at
            current = app["working_representation"]
            projected, report = project_body(current["body"], segment["keeps"])
            input_hash = current["content_hash"]
            output_hash = sha256_text(projected)

            segment.update(
                {
                    "status": "completed",
                    "completed_at": at,
                    "actual_cost": cost,
                    "input_hash": input_hash,
                    "output_hash": output_hash,
                    "projected_body": projected,
                    "semantic_report": report,
                    "legality_status": "proof_available" if definition.provable else "proof_absent",
                }
            )
            app["budget"]["actual_cost"] += cost
            app["budget"]["hold_cost"] = max(app["budget"]["hold_cost"], app["budget"]["actual_cost"])
            app["actual_cost"] = app["budget"]["actual_cost"]
            if definition.provable:
                segment["proof"] = self._issue_proof(app, segment, definition, at, input_hash, output_hash, cost)
            else:
                segment["proof"] = None
            app["working_representation"] = {
                "representation_rep": segment["output_rep"],
                "body": projected,
                "content_hash": output_hash,
                "source_segment": segment["segment_id"],
            }
            ran += 1

        if all(segment["status"] == "completed" for segment in app["segments"]) and app["segments"]:
            existing_delivery = self._find_delivery(app, app.get("current_delivery_id"))
            if existing_delivery and existing_delivery.get("plan_version") == app["active_plan_version"]:
                delivery = existing_delivery
            else:
                delivery = self._create_delivery(app, at)
            if app.get("auto_deliver", self.auto_deliver) and delivery.get("delivered_at") is None:
                self.deliver_delivery(app_id, at=at, delivery_id=delivery["id"])
            elif delivery.get("delivered_at") is None:
                app["state"] = "awaiting_delivery"
            return {"ran": ran, "delivery_id": delivery["id"], "delivered": app["state"] == "delivered"}

        if not app["segments"]:
            app["state"] = "blocked"
        return {"ran": ran, "delivered": False, "reason": "segments_not_complete"}

    def _issue_proof(
        self,
        app: dict[str, Any],
        segment: dict[str, Any],
        definition: Any,
        at: int,
        input_hash: str,
        output_hash: str,
        cost: int,
    ) -> dict[str, Any]:
        authorization = definition.authorization or {}
        grant_id = authorization.get("grant_id") or f"grant:{definition.uid}"
        payload = {
            "proof_id": f"proof:{app['id']}:{segment['segment_id']}",
            "grant_id": grant_id,
            "edge_uid": definition.uid,
            "application_id": app["id"],
            "data_scope": app["data_scope"],
            "completed_at": at,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "cost": cost,
            "lawful_basis": definition.lawful_basis or authorization.get("basis"),
        }
        payload["digest"] = sha256_text({key: value for key, value in payload.items() if key != "digest"})
        return payload

    def _verify_proof(self, proof: dict[str, Any] | None, segment: dict[str, Any], app: dict[str, Any]) -> bool:
        if not isinstance(proof, dict) or not proof.get("digest"):
            return False
        digest = proof["digest"]
        recomputed = sha256_text({key: value for key, value in proof.items() if key != "digest"})
        if digest != recomputed:
            return False
        if proof.get("edge_uid") != segment.get("uid"):
            return False
        if proof.get("output_hash") != segment.get("output_hash"):
            return False
        if proof.get("application_id") != app["id"]:
            return False
        return True

    def _create_delivery(self, app: dict[str, Any], at: int) -> dict[str, Any]:
        working = app["working_representation"]
        supported = set(CORE_DISTINCTIONS)
        for segment in app["segments"]:
            supported.intersection_update(segment.get("keeps", []))
        semantic_complete = set(app["required_distinctions"]).issubset(supported)
        all_proven = all(
            self._verify_proof(segment.get("proof"), segment, app)
            for segment in app["segments"]
        ) if app["segments"] else True

        delivery_id = f"delivery:{app['id']}:v{app['active_plan_version']}"
        delivery = {
            "id": delivery_id,
            "kind": "delivery",
            "application_id": app["id"],
            "plan_version": app["active_plan_version"],
            "representation_rep": working["representation_rep"],
            "target_rep": app["target_rep"],
            "body": deepcopy(working["body"]),
            "content_hash": working["content_hash"],
            "business_status": working["body"].get("status") if isinstance(working["body"], dict) else None,
            "distinctions": sorted(body_distinctions(working["body"])),
            "supported_distinctions": sorted(supported),
            "required_distinctions": app["required_distinctions"],
            "semantic_complete": semantic_complete,
            "legality_status": "proven" if all_proven else "proof_absent",
            "new_citation_allowed": True,
            "status": "ready_awaiting_delivery",
            "created_at": at,
            "delivered_at": None,
            "segment_uids": [segment["uid"] for segment in app["segments"]],
            "citation_ids": [],
        }
        app["deliveries"].append(delivery)
        app["current_delivery_id"] = delivery_id
        return delivery

    def deliver_delivery(self, app_id: str, at: int, delivery_id: str | None = None) -> dict[str, Any]:
        app = self._require_app(app_id)
        delivery = self._find_delivery(app, delivery_id or app.get("current_delivery_id"))
        if delivery is None:
            return {"delivered": False, "reason": "no_delivery"}
        if delivery.get("status") == "held_for_review":
            return {
                "delivered": False,
                "delivery_id": delivery["id"],
                "reason": "legality_unproven_pending_review",
                "review_issues": app["review_issues"],
            }

        delivery["status"] = "delivered"
        delivery["delivered_at"] = at
        app["state"] = "delivered"
        app["state_reason"] = None
        fact = {
            "kind": "delivery",
            "at": at,
            "delivery_id": delivery["id"],
            "representation_id": delivery["id"],
            "representation_rep": delivery["representation_rep"],
            "customer": app["customer"],
        }
        app["usage_facts"].append(fact)
        self._append_timeline(app, at, "delivered", {"delivery": delivery["id"]})
        self._settle_budget(app, at)
        return {"delivered": True, "delivery_id": delivery["id"]}

    def _on_deliver_event(self, at: int, record: dict[str, Any]) -> dict[str, Any]:
        app_id = str(record.get("id") or record.get("application_id"))
        return self.deliver_delivery(app_id, at=at, delivery_id=record.get("delivery_id"))

    def _on_transform_event(self, at: int, record: dict[str, Any]) -> dict[str, Any]:
        # The built-in synthetic path executes transforms deterministically.
        # This event is an explicit extension point for external adapter receipts.
        app_id = str(record.get("id") or record.get("application_id"))
        app = self._require_app(app_id)
        index = int(record.get("segment_index", record.get("index", 0)))
        segment = next((item for item in app["segments"] if item["index"] == index), None)
        if segment is None:
            raise KeyError(f"{app_id} 没有转换段 {index}")
        if segment.get("status") == "completed":
            return {"completed": segment["segment_id"], "idempotent": True}
        if record.get("body") is not None:
            current = app["working_representation"] or {"body": {}, "content_hash": sha256_text({})}
            projected = record["body"]
            segment.update(
                {
                    "status": "completed",
                    "completed_at": at,
                    "actual_cost": int(segment["cost"]),
                    "input_hash": current.get("content_hash"),
                    "output_hash": sha256_text(projected),
                    "projected_body": projected,
                }
            )
            app["budget"]["actual_cost"] += int(segment["cost"])
            app["working_representation"] = {
                "representation_rep": segment["output_rep"],
                "body": projected,
                "content_hash": segment["output_hash"],
            }
            if all(item["status"] == "completed" for item in app["segments"]):
                self._create_delivery(app, at)
            return {"completed": segment["segment_id"]}
        return self.run_transformations(app_id, at=at)

    # ------------------------------------------------------------------
    # Revocation and replanning
    # ------------------------------------------------------------------
    def _on_withdraw(self, at: int, record: dict[str, Any]) -> dict[str, Any]:
        affected_apps: list[str] = []
        edge_id = record.get("edge")
        if edge_id:
            version = record.get("version")
            definitions = self.registry.withdraw_adapter(
                str(edge_id),
                at=at,
                version=int(version) if version is not None else None,
                reason=record.get("reason"),
            )
            for app_id, app in list(self.apps.items()):
                if self._apply_adapter_revocation(app, str(edge_id), at, record, definitions):
                    affected_apps.append(app_id)

        tool_id = record.get("tool")
        if tool_id:
            self.registry.withdraw_tool(str(tool_id), at=at, reason=record.get("reason"))

        return {"withdrawn_edge": edge_id, "affected_applications": sorted(set(affected_apps))}

    def _apply_adapter_revocation(
        self,
        app: dict[str, Any],
        edge_id: str,
        at: int,
        record: dict[str, Any],
        withdrawn_definitions: list[Any],
    ) -> bool:
        affected = False
        need_replan = False
        version_filter = record.get("version")

        for segment in app["segments"]:
            if segment["edge_id"] != edge_id:
                continue
            if version_filter is not None and segment["version"] != int(version_filter):
                continue
            affected = True

            if segment["status"] in {"planned", "revoked"}:
                segment["status"] = "revoked"
                segment["note"] = "适配许可撤销；尚未开始，重新决定路径"
                need_replan = True
            elif segment["status"] == "started":
                segment["status"] = "revoked_in_flight"
                segment["note"] = "转换已在途；以执行开始时授权和终态回执为准"
            elif segment["status"] == "completed":
                lawful = self._verify_proof(segment.get("proof"), segment, app)
                if lawful:
                    segment["legality_status"] = "proven_after_revocation"
                    segment["note"] = "撤销前完成，且授权/完整性证明有效"
                else:
                    segment["status"] = "quarantined"
                    segment["legality_status"] = "unproven_after_revocation"
                    issue = {
                        "code": "completed_transform_legality_unproven",
                        "at": at,
                        "application_id": app["id"],
                        "segment": segment["uid"],
                        "message": "已完成但未下发的转换无法证明合法；不得下发该表示，保留原结果待复核",
                    }
                    app["review_issues"].append(issue)
                    self._hold_undelivered_delivery(app, segment, at)

        # A delivery may already have been handed downstream.  Its fact and
        # citation snapshots stay immutable, but new references can be blocked.
        for delivery in app["deliveries"]:
            if edge_id in {uid.split("@v")[0] for uid in delivery.get("segment_uids", [])}:
                if delivery.get("delivered_at") is not None:
                    unproven = any(
                        segment["edge_id"] == edge_id
                        and segment.get("status") == "quarantined"
                        for segment in app["segments"]
                    )
                    if unproven:
                        delivery["new_citation_allowed"] = False
                        delivery["legality_status"] = "unproven_after_revocation"
                        app["review_issues"].append(
                            {
                                "code": "delivered_representation_new_citation_blocked",
                                "at": at,
                                "delivery_id": delivery["id"],
                                "message": "已下发事实不重写，但合法性无法补证前不得用于新引用",
                            }
                        )

        if need_replan and app.get("state") != "delivered":
            self._replan_application(app, at, reason="adapter_withdrawn")
        app["updated_at"] = at
        return affected

    def _hold_undelivered_delivery(self, app: dict[str, Any], segment: dict[str, Any], at: int) -> None:
        delivery = self._find_delivery(app, app.get("current_delivery_id"))
        if delivery and delivery.get("delivered_at") is None:
            delivery["status"] = "held_for_review"
            delivery["new_citation_allowed"] = False
            delivery["legality_status"] = "unproven_after_revocation"
            delivery["hold_reason"] = "completed_transform_legality_unproven"
            delivery["fallback_representation_id"] = app.get("authoritative_original_id")
            delivery["fallback_body"] = deepcopy(
                next(
                    (item["body"] for item in app["original_representations"] if item["id"] == app.get("authoritative_original_id")),
                    None,
                )
            )
            app["state"] = "held_for_review"
            app["state_reason"] = "转换已完成未下发且不能证明合法；保留原结果，等待合法性复核"

    def _replan_waiting_apps(self, at: int, reason: str) -> list[str]:
        changed: list[str] = []
        terminal_states = {
            "awaiting_delivery",
            "closed",
            "delivered",
            "held_for_review",
            "malformed_receipt",
            "receipt_conflict",
            "tool_failed",
            "tool_never",
        }
        for app in self.apps.values():
            if app.get("state") in terminal_states:
                continue
            before = canonical_json(app.get("active_plan"))
            if app.get("active_attempt_id"):
                self._replan_application(app, at, reason=reason)
            else:
                self._create_plan_version(app, at, reason=reason, tool_id=app.get("actual_tool_id"))
            after = canonical_json(app.get("active_plan"))
            if before != after:
                changed.append(app["id"])
        return changed

    def _replan_application(self, app: dict[str, Any], at: int, reason: str) -> dict[str, Any] | None:
        if app.get("state") == "delivered":
            return None

        prefix = self._good_prefix(app)
        prefix_start_rep = prefix[-1]["output_rep"] if prefix else None
        original_rep = self._original_output_rep(app)
        if not original_rep and not prefix_start_rep:
            return None

        sunk_cost = app["budget"]["actual_cost"]
        remaining_budget = app["budget_limit"] - sunk_cost
        if remaining_budget < 0:
            return None

        chosen_start_rep = prefix_start_rep or original_rep
        chosen_prefix = prefix
        replan_diagnostics: dict[str, Any] = {}

        if prefix and prefix_start_rep:
            suffix_check = self.planner.plan_adapter_route(
                start_rep=prefix_start_rep,
                target_rep=app["target_rep"],
                required=app["required_distinctions"],
                budget=max(0, remaining_budget),
                at=at,
                scope=app["data_scope"],
            )
            if not suffix_check.get("feasible") and original_rep:
                # A completed prefix may be a dead end.  Re-deciding from the
                # original representation is legal, but all sunk cost still
                # counts against this request's budget.
                full_check = self.planner.plan_adapter_route(
                    start_rep=original_rep,
                    target_rep=app["target_rep"],
                    required=app["required_distinctions"],
                    budget=max(0, remaining_budget),
                    at=at,
                    scope=app["data_scope"],
                )
                replan_diagnostics["suffix_blockers"] = suffix_check.get("blockers", [])
                replan_diagnostics["original_representation_fallback"] = full_check
                chosen_start_rep = original_rep
                chosen_prefix = []
            elif suffix_check.get("feasible"):
                replan_diagnostics["reused_completed_prefix"] = [segment["uid"] for segment in prefix]

        old_version = app["active_plan_version"]
        old_segments = deepcopy(app["segments"])
        if old_segments:
            app["segment_archive"].append({"version": old_version, "at": at, "reason": reason, "segments": old_segments})

        plan = self._create_plan_version(
            app,
            at,
            reason=reason,
            start_rep=chosen_start_rep,
            available_budget=remaining_budget,
            preserve_prefix=chosen_prefix,
        )
        if replan_diagnostics:
            plan["replan_diagnostics"] = replan_diagnostics
            app["active_plan"]["replan_diagnostics"] = replan_diagnostics
        self._append_timeline(app, at, "internal_replan", {"version": app["active_plan_version"], "reason": reason})

        if plan.get("feasible") and app.get("authoritative_original_id") and app.get("auto_transform", self.auto_transform):
            self.run_transformations(app["id"], at=at)
        return plan

    def _good_prefix(self, app: dict[str, Any]) -> list[dict[str, Any]]:
        prefix: list[dict[str, Any]] = []
        for segment in sorted(app.get("segments", []), key=lambda item: item.get("index", 0)):
            if segment.get("status") == "completed" and segment.get("legality_status") not in {
                "unproven_after_revocation",
            } and segment.get("status") != "quarantined":
                prefix.append(deepcopy(segment))
            else:
                break
        return prefix

    def _last_good_representation(self, app: dict[str, Any]) -> str | None:
        prefix = self._good_prefix(app)
        if prefix:
            return prefix[-1]["output_rep"]
        return self._original_output_rep(app)

    def _original_output_rep(self, app: dict[str, Any]) -> str | None:
        attempt_id = app.get("active_attempt_id")
        if attempt_id:
            attempt = next((item for item in app["attempts"] if item["attempt_id"] == attempt_id), None)
            if attempt:
                tool = self.registry.latest_tool(attempt["tool_id"])
                if tool:
                    return tool.output_rep
        plan_tool = (app.get("active_plan") or {}).get("tool") or {}
        return plan_tool.get("output_rep")

    def _tool_hop_for_active_attempt(self, app: dict[str, Any]) -> dict[str, Any] | None:
        rep = self._original_output_rep(app)
        tool_id = app.get("actual_tool_id")
        if not tool_id:
            return None
        tool = self.registry.latest_tool(tool_id)
        return {
            "kind": "tool",
            "service": tool.provider if tool else tool_id,
            "tool": tool_id,
            "version": tool.version if tool else None,
            "uid": tool.uid if tool else f"tool:{tool_id}",
            "input": app["source_rep"],
            "output": rep,
            "cost": 0,
        }

    def _segment_to_hop(self, segment: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "adapter",
            "edge": segment["edge_id"],
            "version": segment["version"],
            "uid": segment["uid"],
            "input": segment["input_rep"],
            "output": segment["output_rep"],
            "keeps": segment.get("keeps", []),
            "cost": segment["cost"],
        }

    def _settle_budget(self, app: dict[str, Any], at: int) -> None:
        budget = app["budget"]
        actual = int(budget.get("actual_cost", 0))
        hold = int(budget.get("hold_cost", 0))
        budget["released_unused_hold"] = max(0, hold - actual)
        budget["settled"] = True
        budget["settled_at"] = at
        # actual_cost is intentionally retained; only unused reservation is released.
        self._append_timeline(
            app,
            at,
            "budget_settled",
            {"actual_cost": actual, "released_unused_hold": budget["released_unused_hold"]},
        )

    # ------------------------------------------------------------------
    # Citations and queries
    # ------------------------------------------------------------------
    def _on_cite(self, at: int, record: dict[str, Any]) -> dict[str, Any]:
        citation_id = str(record.get("citation_id") or record.get("id") or f"cit-{self.citation_count + 1}")
        if citation_id in self.citations:
            return self.query_citation(citation_id)

        app_id = (
            record.get("application_id")
            or record.get("app_id")
            or record.get("application")
            or record.get("for_application")
        )
        if not app_id:
            raise ValueError("cite 缺少 application_id")
        app = self._require_app(str(app_id))

        selector = record.get("representation_id") or record.get("rep_id")
        kind_hint = record.get("representation", "original")
        representation = self._find_representation(app, selector, kind_hint)
        if representation is None:
            raise KeyError(f"申请 {app_id} 没有可引用的表示 {selector or kind_hint}")

        self.citation_count += 1
        purpose = record.get("purpose", "citation")
        formal, _ = requirements_for(purpose, record.get("need", representation.get("supported_distinctions", [])))
        scope = normalize_scope(record.get("data_scope", record.get("scope", app["data_scope"])))
        citation = {
            "id": citation_id,
            "citation_id": citation_id,
            "ref": record.get("ref") or record.get("citation_ref") or record.get("downstream_ref") or citation_id,
            "downstream": record.get("downstream") or record.get("system") or "downstream-review",
            "application_id": app["id"],
            "customer": record.get("customer") or app["customer"],
            "representation_id": representation["id"],
            "representation_kind": representation.get("kind"),
            "representation_rep": representation.get("representation_rep"),
            "content_hash": representation["content_hash"],
            "snapshot": deepcopy(representation),
            "purpose": str(purpose),
            "formal_proofread_use": formal or str(purpose).lower() in FORMAL_PURPOSE_HINT,
            "data_scope": scope,
            "submitted_at": at,
            "immutable": True,
        }
        self.citations[citation_id] = citation
        if citation["ref"]:
            self.citation_ref_index[str(citation["ref"])] = citation_id
        app["citation_ids"].append(citation_id)
        representation.setdefault("citation_ids", []).append(citation_id)
        app["usage_facts"].append(
            {
                "kind": "citation_submitted",
                "at": at,
                "citation_id": citation_id,
                "ref": citation["ref"],
                "representation_id": representation["id"],
                "representation_kind": representation.get("kind"),
                "customer": citation["customer"],
                "data_scope": scope,
            }
        )
        self._append_timeline(app, at, "citation_submitted", {"citation": citation_id, "ref": citation["ref"]})
        return self.query_citation(citation_id)

    def query_entry(self, spec: str | dict[str, Any] | None = None) -> dict[str, Any]:
        if isinstance(spec, str):
            spec = {"id": spec}
        record = dict(spec or {})
        citation_key = record.get("citation") or record.get("citation_id") or record.get("ref")
        if citation_key and (citation_key in self.citations or citation_key in self.citation_ref_index):
            return self.query_citation(str(citation_key))

        app_id = record.get("application_id") or record.get("app_id") or record.get("id")
        if app_id and app_id in self.apps:
            return self.query_application(str(app_id))

        if citation_key:
            result = self.query_citation(str(citation_key))
            if result.get("found"):
                return result
        if app_id:
            # Maybe id is a citation id even when placed in id.
            if app_id in self.citations or app_id in self.citation_ref_index:
                return self.query_citation(str(app_id))
            return {"found": False, "id": app_id, "kind": "application"}
        return {"found": False, "error": "query requires id, application_id, citation or ref"}

    def query_application(self, app_id: str) -> dict[str, Any]:
        if app_id not in self.apps:
            return {"found": False, "id": app_id, "kind": "application"}
        app = deepcopy(self.apps[app_id])
        original = self._find_representation(app, app.get("authoritative_original_id"), "original")
        delivery = self._find_delivery(app, app.get("current_delivery_id"))

        original_eligibility = self._representation_eligibility(app, original) if original else {"allowed": False, "reasons": ["original_representation_missing"]}
        delivery_eligibility = self._representation_eligibility(app, delivery) if delivery else {"allowed": False, "reasons": ["delivery_representation_missing"]}

        requested_required = set(app["required_distinctions"])
        formal_required = set(CORE_DISTINCTIONS)
        original_requested_complete = bool(original and requested_required.issubset(set(original.get("supported_distinctions", []))))
        original_formal_complete = bool(original and formal_required.issubset(set(original.get("supported_distinctions", []))))
        delivery_requested_complete = bool(delivery and requested_required.issubset(set(delivery.get("supported_distinctions", []))) and delivery.get("semantic_complete"))
        delivery_formal_complete = bool(delivery and formal_required.issubset(set(delivery.get("supported_distinctions", []))))

        business_status = original.get("business_status") if original else None
        delivered_status = delivery.get("business_status") if delivery else None
        unknown_preserved = bool(delivery and UNKNOWN in set(delivery.get("supported_distinctions", [])))

        app["found"] = True
        app["query_kind"] = "application"
        app["blockers"] = (app.get("active_plan") or {}).get("blockers", app.get("blockers", []))
        if app.get("state") == "held_for_review" and original:
            app["retained_original_result"] = {
                "representation_id": original["id"],
                "representation_rep": original.get("representation_rep"),
                "body": original.get("body"),
                "content_hash": original.get("content_hash"),
                "note": "不能下发合法性未证明的转换表示；原结果保留且仍可按其用途评估引用",
                "review_reason": app.get("state_reason"),
            }
        app["formal_proofread"] = {
            "requested": app["formal_proofread_requested"],
            "required_distinctions": app["required_distinctions"],
            "formal_core_distinctions": list(CORE_DISTINCTIONS),
            "target_representation": app["target_rep"],
            "target_delivered": bool(delivery and delivery.get("delivered_at") is not None),
            "target_semantically_complete_for_requested_use": delivery_requested_complete,
            "target_formal_semantically_complete": delivery_formal_complete,
            "target_legally_usable": delivery_eligibility["allowed"],
            "sufficient_for_requested_use": bool(
                delivery and delivery.get("delivered_at") is not None and delivery_requested_complete and delivery_eligibility["allowed"]
            ),
            "can_use_delivered_representation": bool(
                delivery
                and delivery.get("delivered_at") is not None
                and delivery_formal_complete
                and delivery_eligibility["allowed"]
            ),
            "original_available": original is not None,
            "original_semantically_complete_for_requested_use": original_requested_complete,
            "original_formal_semantically_complete": original_formal_complete,
            "original_new_citation_allowed": original_eligibility["allowed"] and original_formal_complete,
            "can_use_original_for_formal_proofreading": bool(
                original and original_formal_complete and original_eligibility["allowed"]
            ),
            "reasons": sorted(set(original_eligibility.get("reasons", []) + delivery_eligibility.get("reasons", []))),
        }
        app["citation_query_entry"] = "query_citation(citation_id_or_ref)"
        app["application_query_entry"] = "query_application(id)"
        app["business_answer"] = {
            "business_status": business_status,
            "delivered_status": delivered_status,
            "unknown_preserved_in_delivery": unknown_preserved,
            "owner_follow_up_recommended": business_status == UNKNOWN,
            "delivery_can_drive_follow_up": delivered_status == UNKNOWN and unknown_preserved,
            "execution_terminal_state_missing": app.get("state") == "endpoint_unknown",
        }
        app["recovery"] = {
            "recover_by": ["application_id", "data_scope_at_request", "actual_attempt_id", "customer"],
            "application_id": app["id"],
            "data_scope": app["data_scope"],
            "actual_attempt_id": app.get("active_attempt_id"),
            "customer": app["customer"],
            "budget_settled": app["budget"].get("settled", False),
            "budget_hold_retained_when_terminal_missing": app.get("state") == "endpoint_unknown",
        }
        return app

    def query_citation(self, identifier: str, at: int | None = None, scope: Any = None) -> dict[str, Any]:
        citation_id = self.citation_ref_index.get(identifier, identifier)
        citation = self.citations.get(citation_id)
        if citation is None:
            return {"found": False, "query_kind": "citation", "id": identifier}

        citation_view = deepcopy(citation)
        app = self.apps.get(citation["application_id"])
        if app is None:
            citation_view.update(
                {
                    "found": True,
                    "query_kind": "citation",
                    "historical_snapshot_unchanged": True,
                    "new_citation_allowed": False,
                    "new_formal_proofread_citation_allowed": False,
                    "reasons": ["application_missing"],
                }
            )
            return citation_view

        representation = self._find_representation(app, citation["representation_id"], citation["representation_kind"])
        eligibility = self._representation_eligibility(app, representation, at=at, requested_scope=scope or citation.get("data_scope"))
        required = set(CORE_DISTINCTIONS) if citation.get("formal_proofread_use") else set()
        supported = set((representation or {}).get("supported_distinctions", []))
        semantic_ok = required.issubset(supported) if citation.get("formal_proofread_use") else True

        snapshot_hash = sha256_text(citation["snapshot"].get("body"))
        historical_unchanged = snapshot_hash == citation.get("content_hash")
        citation_view.update(
            {
                "found": True,
                "query_kind": "citation",
                "application_state": app.get("state"),
                "historical_snapshot_unchanged": historical_unchanged,
                "late_receipts_do_not_rewrite_snapshot": True,
                "new_reference_allowed": eligibility["allowed"],
                "new_citation_allowed": eligibility["allowed"] and semantic_ok,
                "new_formal_proofread_citation_allowed": eligibility["allowed"] and semantic_ok,
                "formal_semantic_ok": semantic_ok,
                "eligibility_checks": eligibility,
                "answer_for_new_citations": (
                    "该表示仍可用于新的正式校对引用"
                    if eligibility["allowed"] and semantic_ok
                    else "历史引用快照保持不变，但在解除以下事项前不可用于新引用：" + "、".join(eligibility.get("reasons", []))
                ),
            }
        )
        return citation_view

    def _representation_eligibility(
        self,
        app: dict[str, Any],
        representation: dict[str, Any] | None,
        at: int | None = None,
        requested_scope: Any = None,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        if representation is None:
            return {"allowed": False, "reasons": ["representation_missing"], "checks": {}}

        rep_id = representation["id"]
        if representation.get("status") in {"disputed", "conflicting"}:
            reasons.append("conflicting_terminal_receipts_unresolved")
        if app.get("receipt_conflicts") and rep_id not in {cid for conflict in app["receipt_conflicts"] for cid in conflict.get("representation_ids", [])}:
            # A third representation cannot escape an unresolved conflict.
            reasons.append("application_has_unresolved_receipt_conflict")

        if representation.get("kind") == "original":
            attempt = next(
                (item for item in app["attempts"] if item["attempt_id"] == representation.get("attempt_id")),
                None,
            )
            if attempt and not attempt.get("endpoint_terminal"):
                reasons.append("execution_endpoint_terminal_state_missing")
            tool = self.registry.latest_tool(attempt["tool_id"]) if attempt else None
            if tool and not scope_supported(tool.data_scope, requested_scope or app["data_scope"]):
                reasons.append("outside_tool_supported_data_scope")
            if requested_scope is not None and not scope_supported(app["data_scope"], requested_scope):
                reasons.append("outside_application_data_scope")
            if tool and tool.usage_window and at is not None and not tool.available_at(at):
                reasons.append("outside_usage_window")
            for issue in app.get("review_issues", []):
                if issue.get("representation_id") == rep_id and issue.get("blocking", True):
                    reasons.append(issue.get("code", "representation_hold"))
        else:
            if representation.get("status") == "held_for_review" or representation.get("new_citation_allowed") is False:
                reasons.append(representation.get("hold_reason") or "delivery_legality_or_review_hold")
            if representation.get("legality_status") in {"unproven_after_revocation", "proof_absent"} and representation.get("status") != "delivered":
                reasons.append("transformation_legality_not_proven")
            if requested_scope is not None and not scope_supported(app["data_scope"], requested_scope):
                reasons.append("outside_application_data_scope")

        # Deduplicate while preserving stable order.
        seen: set[str] = set()
        ordered = [item for item in reasons if not (item in seen or seen.add(item))]
        return {"allowed": not ordered, "reasons": ordered, "representation_id": rep_id}

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------
    def _require_app(self, app_id: str) -> dict[str, Any]:
        if app_id not in self.apps:
            raise KeyError(f"未知申请 id: {app_id}")
        return self.apps[app_id]

    def _find_representation(
        self, app: dict[str, Any], representation_id: str | None, kind_hint: str | None
    ) -> dict[str, Any] | None:
        if representation_id:
            for rep in app["original_representations"]:
                if rep["id"] == representation_id:
                    return rep
            return self._find_delivery(app, representation_id)

        if kind_hint in {"delivery", "client", "target"}:
            return self._find_delivery(app, app.get("current_delivery_id"))
        return next(
            (rep for rep in app["original_representations"] if rep["id"] == app.get("authoritative_original_id")),
            None,
        )

    def _find_delivery(self, app: dict[str, Any], delivery_id: str | None) -> dict[str, Any] | None:
        if not delivery_id:
            return app["deliveries"][-1] if app["deliveries"] else None
        return next((item for item in app["deliveries"] if item["id"] == delivery_id), None)

    def _append_timeline(self, app: dict[str, Any], at: int, event: str, details: dict[str, Any]) -> None:
        app.setdefault("timeline", []).append({"at": at, "event": event, "details": details})
        app["updated_at"] = at
