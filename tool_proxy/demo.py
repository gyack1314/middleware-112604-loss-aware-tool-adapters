"""End-to-end offline demo using an in-process synthetic tool."""

from __future__ import annotations

from .service import ToolProxy
from .synthetic import SyntheticTool, SyntheticToolGateway


def run_demo() -> dict:
    gateway = SyntheticToolGateway(
        [
            SyntheticTool(
                tool_id="synthetic-review-db",
                request_rep="token",
                output_rep="full",
                responses=[{"state": "done", "body": {"status": "unknown", "case": "demo-1"}}],
            )
        ]
    )
    proxy = ToolProxy()

    for message in gateway.declaration_messages():
        proxy.handle(message)
    proxy.handle(
        {
            "at": 0,
            "kind": "adapter",
            "record": {
                "edge": "safe-proof",
                "input": "full",
                "output": "proof",
                "keeps": ["absent", "unknown"],
                "cost": 1,
                "lawful_basis": "internal-review-license",
            },
        }
    )
    proxy.handle(
        {
            "at": 1,
            "kind": "query",
            "record": {
                "id": "demo-app",
                "customer": "workbench-demo",
                "source": "token",
                "target": "proof",
                "purpose": "formal",
                "budget": 2,
                "data_scope": "reviews",
            },
        }
    )

    accepted = gateway.send("synthetic-review-db", "demo-app", {"query": "case-1"}, at=2)
    proxy.handle(accepted)
    receipt = gateway.receipt(accepted["record"]["attempt"], at=3)
    proxy.handle(receipt)
    proxy.handle(
        {
            "at": 4,
            "kind": "cite",
            "record": {
                "citation_id": "demo-cit-1",
                "application_id": "demo-app",
                "representation": "original",
                "ref": "review-case-1",
                "downstream": "proofreading-workbench",
            },
        }
    )

    app = proxy.query_application("demo-app")
    citation = proxy.query_citation("demo-cit-1")
    return {
        "synthetic_tool_calls": gateway.tools["synthetic-review-db"].calls,
        "application": {
            "id": app["id"],
            "state": app["state"],
            "business_answer": app["business_answer"],
            "formal_proofread": app["formal_proofread"],
            "budget": app["budget"],
        },
        "citation": {
            "id": citation["id"],
            "ref": citation["ref"],
            "historical_snapshot_unchanged": citation["historical_snapshot_unchanged"],
            "new_citation_allowed": citation["new_citation_allowed"],
            "answer_for_new_citations": citation["answer_for_new_citations"],
        },
    }
