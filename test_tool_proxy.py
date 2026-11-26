"""End-to-end and domain-rule tests using only local synthetic declarations."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scenario_reader import iter_messages
from tool_proxy.scenario import run_scenario
from tool_proxy.service import SUPPORTED_KINDS, ToolProxy
from tool_proxy.store import JsonlEventStore
from tool_proxy.synthetic import SyntheticTool, SyntheticToolGateway
from tool_proxy.semantics import project_body


def msg(at: int, kind: str, record: dict) -> dict:
    return {"at": at, "kind": kind, "record": record}


def tool_message(tool_id: str = "k", at: int = 0, **overrides) -> dict:
    record = {
        "tool": tool_id,
        "source": "token",
        "result": "full",
        "available": True,
        "data_scope": "*",
    }
    record.update(overrides)
    return msg(at, "tool", record)


def adapter_message(edge: str, at: int = 0, **record) -> dict:
    base = {"edge": edge, "input": "full", "output": "out", "keeps": ["absent", "unknown"], "cost": 1}
    base.update(record)
    return msg(at, "adapter", base)


def formal_query(app_id: str = "app", at: int = 1, budget: int = 5, **overrides) -> dict:
    record = {
        "id": app_id,
        "customer": "acme",
        "source": "token",
        "target": "out",
        "purpose": "formal",
        "need": ["absent", "unknown"],
        "budget": budget,
        "data_scope": "case-7",
    }
    record.update(overrides)
    return msg(at, "query", record)


class SemanticProjectionTests(unittest.TestCase):
    def test_lost_absent_or_unknown_both_project_to_legacy_empty(self):
        absent, absent_report = project_body({"status": "absent"}, keeps=[])
        unknown, unknown_report = project_body({"status": "unknown"}, keeps=[])
        self.assertEqual(absent["status"], "empty")
        self.assertEqual(unknown["status"], "empty")
        self.assertEqual(absent_report["lost_distinctions"], ["absent"])
        self.assertEqual(unknown_report["lost_distinctions"], ["unknown"])

    def test_kept_distinction_survives(self):
        projected, report = project_body({"status": "unknown"}, keeps=["unknown"])
        self.assertEqual(projected["status"], "unknown")
        self.assertEqual(report["lost_distinctions"], [])


class ProvidedScenarioTests(unittest.TestCase):
    def test_summary_path_is_not_formal_citation_path(self):
        outcome = run_scenario("examples/compatible-summary-not-citation.jsonl")
        proxy = outcome["proxy"]

        inspected = outcome["inspections"][0]["result"]
        self.assertEqual(inspected["state"], "blocked")
        self.assertEqual(inspected["blockers"][0]["kind"], "semantic")
        self.assertIn("unknown", inspected["blockers"][0]["missing"])

        overview = proxy.query_application("overview")
        self.assertEqual(overview["state"], "delivered")
        self.assertEqual(overview["deliveries"][-1]["body"]["status"], "absent")
        self.assertTrue(overview["formal_proofread"]["sufficient_for_requested_use"])
        self.assertFalse(overview["formal_proofread"]["can_use_delivered_representation"])
        self.assertFalse(overview["business_answer"]["owner_follow_up_recommended"])

    def test_retired_edge_unknown_state_restart_and_late_terminal_reply(self):
        path = Path("examples/retired-edge-with-live-tool.jsonl")
        messages = list(iter_messages(path, SUPPORTED_KINDS))
        proxy = ToolProxy()

        unknown_seen = False
        for message in messages:
            result = proxy.handle(message)
            if message["kind"] == "restart":
                proxy.recover_in_place()
            if message["kind"] == "reply" and message["record"]["state"] == "unknown":
                view = proxy.query_application("q")
                self.assertEqual(view["state"], "endpoint_unknown")
                self.assertFalse(view["original_representations"])
                self.assertFalse(view["budget"]["settled"])
                self.assertEqual(view["budget"]["hold_cost"], 4)
                unknown_seen = True
            if message["kind"] == "inspect":
                inspected = result

        self.assertTrue(unknown_seen)
        self.assertEqual(inspected["state"], "blocked")
        self.assertEqual(inspected["blockers"][0]["kind"], "budget")
        self.assertEqual(inspected["blockers"][0]["cheapest_cost"], 5)
        self.assertEqual(inspected["attempts"][0]["attempt_id"], "a")
        self.assertTrue(inspected["attempts"][0]["terminal"])
        self.assertEqual(len(inspected["original_representations"]), 1)
        self.assertEqual(inspected["original_representations"][0]["business_status"], "unknown")
        self.assertTrue(inspected["formal_proofread"]["can_use_original_for_formal_proofreading"])
        self.assertFalse(inspected["formal_proofread"]["can_use_delivered_representation"])
        self.assertTrue(inspected["business_answer"]["owner_follow_up_recommended"])
        self.assertTrue(inspected["budget"]["settled"])

    def test_scenario_can_recover_from_durable_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlEventStore(directory).initialize()
            outcome = run_scenario(
                "examples/retired-edge-with-live-tool.jsonl",
                store_directory=directory,
            )
            final_before = outcome["proxy"].query_application("q")
            recovered = ToolProxy.recover_from_store(store)
            final_after = recovered.query_application("q")
            self.assertEqual(final_after["recovery"]["actual_attempt_id"], "a")
            self.assertEqual(final_after["recovery"]["data_scope"], final_before["recovery"]["data_scope"])
            self.assertEqual(final_after["state"], final_before["state"])
            self.assertEqual(len(final_after["original_representations"]), 1)


class RoutingAndBudgetTests(unittest.TestCase):
    def test_multihop_cheap_path_beats_over_budget_direct_path(self):
        proxy = ToolProxy()
        proxy.handle(tool_message())
        proxy.handle(adapter_message("a", output="mid", cost=2))
        proxy.handle(adapter_message("b", input="mid", output="out", cost=2))
        proxy.handle(adapter_message("direct", output="out", cost=5))
        proxy.handle(formal_query(budget=4))

        app = proxy.query_application("app")
        self.assertTrue(app["active_plan"]["feasible"])
        self.assertEqual(app["active_plan"]["cost"], 4)
        self.assertEqual([hop["uid"] for hop in app["active_plan"]["hops"] if hop["kind"] == "adapter"], ["a@v1", "b@v1"])

    def test_endpoint_scope_and_window_diagnostics(self):
        proxy = ToolProxy()
        proxy.handle(tool_message(data_scope=["secret"], window={"start": 10, "end": 20}))
        proxy.handle(adapter_message("edge"))
        proxy.handle(formal_query(data_scope="public", at=1, budget=3))
        app = proxy.query_application("app")
        self.assertFalse(app["active_plan"]["feasible"])
        self.assertEqual(app["blockers"][0]["kind"], "endpoint")
        self.assertIn("data_scope_not_supported", app["blockers"][0]["reasons"])

        proxy2 = ToolProxy()
        proxy2.handle(tool_message(window={"start": 10, "end": 20}))
        proxy2.handle(adapter_message("edge"))
        proxy2.handle(formal_query(at=1, budget=3))
        self.assertEqual(proxy2.query_application("app")["blockers"][0]["reasons"], ["outside_available_window"])

    def test_switch_from_reserved_expensive_path_to_cheaper_compatible_path(self):
        proxy = ToolProxy()
        proxy.handle(tool_message())
        proxy.handle(adapter_message("route", cost=4))
        proxy.handle(formal_query(budget=8))
        proxy.handle(msg(2, "accepted", {"id": "app", "tool": "k", "attempt": "a1"}))
        self.assertEqual(proxy.apps["app"]["budget"]["hold_cost"], 4)

        # A cheaper compatible version is declared before any conversion starts.
        proxy.handle(adapter_message("route", cost=2, version=2))
        app = proxy.query_application("app")
        self.assertEqual(app["active_plan"]["cost"], 2)
        self.assertEqual(app["active_plan"]["hops"][-1]["version"], 2)
        self.assertEqual(app["active_attempt_id"], "a1")
        # Conservative hold is not released before the execution endpoint is terminal.
        self.assertEqual(app["budget"]["hold_cost"], 4)

        proxy.handle(msg(3, "reply", {"attempt": "a1", "state": "done", "body": {"status": "unknown"}}))
        app = proxy.query_application("app")
        self.assertEqual(app["state"], "delivered")
        self.assertEqual(app["budget"]["actual_cost"], 2)
        self.assertTrue(app["budget"]["settled"])
        self.assertEqual(app["budget"]["released_unused_hold"], 2)
        self.assertEqual(app["attempts"][0]["attempt_id"], "a1")

    def test_withdraw_unstarted_edge_replans_and_keeps_attempt_identity(self):
        proxy = ToolProxy()
        proxy.handle(tool_message())
        proxy.handle(adapter_message("a", output="mid", cost=1))
        proxy.handle(adapter_message("b", input="mid", output="out", cost=1))
        proxy.handle(adapter_message("alt", output="out", cost=3))
        proxy.handle(formal_query(budget=5))
        proxy.handle(msg(2, "accepted", {"id": "app", "tool": "k", "attempt": "a1"}))
        proxy.handle(msg(3, "withdraw", {"edge": "b"}))

        app = proxy.query_application("app")
        self.assertIn(app["state"], {"accepted", "planned"})
        self.assertEqual([h["uid"] for h in app["active_plan"]["hops"] if h["kind"] == "adapter"], ["alt@v1"])
        proxy.handle(msg(4, "reply", {"attempt": "a1", "state": "done", "body": {"status": "absent"}}))
        app = proxy.query_application("app")
        self.assertEqual(app["state"], "delivered")
        self.assertEqual(app["deliveries"][-1]["segment_uids"], ["alt@v1"])
        self.assertEqual(app["attempts"][0]["attempt_id"], "a1")

    def test_completed_prefix_cost_counts_against_replan_budget(self):
        proxy = ToolProxy(auto_transform=False, auto_deliver=False)
        proxy.handle(tool_message())
        proxy.handle(adapter_message("a", output="mid", cost=2, authorization={"grant_id": "g-a"}))
        proxy.handle(adapter_message("b", input="mid", output="out", cost=2, authorization={"grant_id": "g-b"}))
        proxy.handle(adapter_message("direct", output="out", cost=5, authorization={"grant_id": "g-d"}))
        proxy.handle(formal_query(budget=6, auto_transform=False, auto_deliver=False))
        proxy.handle(msg(2, "accepted", {"id": "app", "tool": "k", "attempt": "a1"}))
        proxy.handle(msg(3, "reply", {"attempt": "a1", "state": "done", "body": {"status": "absent"}}))
        proxy.handle(msg(4, "transform", {"id": "app", "segment_index": 0, "body": {"status": "absent"}}))

        # Prefix a completed; b is still unstarted. Withdraw b and use direct.
        proxy.handle(msg(5, "withdraw", {"edge": "b"}))
        app = proxy.query_application("app")
        self.assertEqual(app["budget"]["actual_cost"], 2)
        self.assertEqual(app["state"], "blocked")
        self.assertEqual(app["blockers"][0]["kind"], "budget")
        self.assertEqual(app["blockers"][0]["cheapest_cost"], 5)


class RevocationCitationTests(unittest.TestCase):
    def _proxy_with_completed_transform(self, *, provable: bool, auto_deliver: bool = False):
        proxy = ToolProxy(auto_deliver=auto_deliver)
        proxy.handle(tool_message())
        adapter = {"edge": "safe", "cost": 1}
        if provable:
            adapter["lawful_basis"] = "license-1"
        proxy.handle(adapter_message(**adapter))
        query = formal_query()
        query["record"]["auto_deliver"] = auto_deliver
        proxy.handle(query)
        proxy.handle(msg(2, "accepted", {"id": "app", "tool": "k", "attempt": "a1"}))
        proxy.handle(msg(3, "reply", {"attempt": "a1", "state": "done", "body": {"status": "unknown"}}))
        return proxy

    def test_completed_undelivered_unprovable_transform_is_quarantined_but_original_remains(self):
        proxy = self._proxy_with_completed_transform(provable=False)
        self.assertEqual(proxy.query_application("app")["state"], "awaiting_delivery")

        proxy.handle(msg(4, "withdraw", {"edge": "safe"}))
        app = proxy.query_application("app")
        self.assertEqual(app["state"], "held_for_review")
        delivery = app["deliveries"][-1]
        self.assertEqual(delivery["status"], "held_for_review")
        self.assertFalse(delivery["new_citation_allowed"])
        self.assertEqual(delivery["fallback_representation_id"], app["authoritative_original_id"])
        self.assertTrue(any(issue["code"] == "completed_transform_legality_unproven" for issue in app["review_issues"]))

        denied = proxy.deliver_delivery("app", at=5)
        self.assertFalse(denied["delivered"])

        # A later alternative definition must not silently clear a legal hold.
        proxy.handle(adapter_message("alt-safe", at=5, cost=1, lawful_basis="license-2"))
        self.assertEqual(proxy.query_application("app")["state"], "held_for_review")

        proxy.handle(msg(6, "cite", {"citation_id": "c-orig", "application_id": "app", "representation": "original", "ref": "r1"}))
        citation = proxy.query_citation("c-orig")
        self.assertTrue(citation["found"])
        self.assertTrue(citation["new_citation_allowed"])

    def test_completed_transform_with_proof_can_be_delivered_after_revocation(self):
        proxy = self._proxy_with_completed_transform(provable=True)
        proxy.handle(msg(4, "withdraw", {"edge": "safe"}))
        result = proxy.deliver_delivery("app", at=5)
        self.assertTrue(result["delivered"])
        app = proxy.query_application("app")
        self.assertEqual(app["state"], "delivered")
        self.assertEqual(app["segments"][0]["legality_status"], "proven_after_revocation")

    def test_cited_original_is_immutable_and_conflicting_late_receipt_blocks_new_citation(self):
        proxy = self._proxy_with_completed_transform(provable=True, auto_deliver=True)
        proxy.handle(msg(4, "cite", {"citation_id": "c1", "application_id": "app", "ref": "downstream-ref"}))
        before = proxy.query_citation("c1")
        self.assertEqual(before["snapshot"]["body"]["status"], "unknown")
        self.assertTrue(before["new_citation_allowed"])

        proxy.handle(msg(5, "reply", {"attempt": "a1", "state": "done", "body": {"status": "absent", "corrected": True}}))
        after = proxy.query_citation("c1")
        self.assertTrue(after["historical_snapshot_unchanged"])
        self.assertEqual(after["snapshot"]["body"]["status"], "unknown")
        self.assertFalse(after["new_citation_allowed"])
        self.assertIn("conflicting_terminal_receipts_unresolved", after["eligibility_checks"]["reasons"])

        by_ref = proxy.query_entry({"citation": "downstream-ref"})
        self.assertEqual(by_ref["citation_id"], "c1")

    def test_duplicate_terminal_receipt_does_not_create_second_delivery(self):
        proxy = self._proxy_with_completed_transform(provable=True, auto_deliver=True)
        proxy.handle(msg(5, "reply", {"attempt": "a1", "state": "done", "body": {"status": "unknown"}}))
        app = proxy.query_application("app")
        self.assertEqual(len(app["deliveries"]), 1)
        self.assertTrue(proxy.query_application("app")["formal_proofread"]["can_use_delivered_representation"])


class CustomerUpgradeAndSyntheticTests(unittest.TestCase):
    def test_adapter_upgrade_only_replans_waiting_customer(self):
        proxy = ToolProxy()
        proxy.handle(tool_message())
        proxy.handle(adapter_message("route", cost=4))

        proxy.handle(formal_query("waiting", customer="acme", budget=8))
        proxy.handle(formal_query("done", customer="globex", budget=8))
        proxy.handle(msg(2, "accepted", {"id": "waiting", "tool": "k", "attempt": "w"}))
        proxy.handle(msg(2, "accepted", {"id": "done", "tool": "k", "attempt": "d"}))
        proxy.handle(msg(3, "reply", {"attempt": "d", "state": "done", "body": {"status": "absent"}}))
        done_before = proxy.query_application("done")
        self.assertEqual(done_before["state"], "delivered")

        proxy.handle(adapter_message("route", cost=2, version=2, keeps=["absent", "unknown"]))
        waiting = proxy.query_application("waiting")
        done_after = proxy.query_application("done")

        self.assertEqual(waiting["active_plan"]["hops"][-1]["version"], 2)
        self.assertEqual(waiting["recovery"]["customer"], "acme")
        self.assertEqual(waiting["attempts"][0]["attempt_id"], "w")
        self.assertEqual(done_after["active_plan_version"], done_before["active_plan_version"])
        self.assertEqual(len(done_after["attempts"]), 1)
        self.assertEqual(done_after["deliveries"][-1]["segment_uids"], ["route@v1"])
        self.assertFalse(any(e["event"] == "internal_replan" for e in done_after["timeline"][-2:]))

    def test_restart_recovers_scope_customer_attempt_and_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlEventStore(directory).initialize()
            proxy = ToolProxy(store=store)
            proxy.dispatch(tool_message())
            proxy.dispatch(adapter_message("route"))
            proxy.dispatch(formal_query(customer="customer-z", data_scope={"tenant": "z", "region": "cn"}))
            proxy.dispatch(msg(2, "accepted", {"id": "app", "tool": "k", "attempt": "attempt-z"}))
            proxy.dispatch(msg(3, "reply", {"attempt": "attempt-z", "state": "unknown"}))

            recovered = ToolProxy.recover_from_store(store)
            app = recovered.query_application("app")
            self.assertEqual(app["customer"], "customer-z")
            self.assertEqual(app["recovery"]["data_scope"], {"region": "cn", "tenant": "z"})
            self.assertEqual(app["recovery"]["actual_attempt_id"], "attempt-z")
            self.assertEqual(app["state"], "endpoint_unknown")
            self.assertFalse(app["budget"]["settled"])
            self.assertEqual(app["budget"]["hold_cost"], 1)

    def test_local_synthetic_gateway_produces_terminal_reply(self):
        gateway = SyntheticToolGateway(
            [SyntheticTool("synth", responses=[{"state": "done", "body": {"status": "absent"}}])]
        )
        proxy = ToolProxy()
        for message in gateway.declaration_messages():
            proxy.handle(message)
        proxy.handle(adapter_message("edge"))
        proxy.handle(formal_query())
        accepted = gateway.send("synth", "app", {"q": 1}, at=2)
        # The declaration helper used tool id k; register/use matching synth path explicitly.
        proxy.handle(accepted)
        receipt = gateway.receipt(accepted["record"]["attempt"], at=3)
        proxy.handle(receipt)
        app = proxy.query_application("app")
        self.assertEqual(app["attempts"][0]["tool_id"], "synth")
        self.assertEqual(app["state"], "delivered")
        self.assertEqual(app["business_answer"]["business_status"], "absent")


if __name__ == "__main__":
    unittest.main()
