"""Local deterministic synthetic tools for tests and offline demos."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class SyntheticTool:
    """In-process fake internal tool with scripted responses."""

    tool_id: str
    request_rep: str = "token"
    output_rep: str = "full"
    data_scope: Any = "*"
    window: Any = None
    available: bool = True
    responses: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _attempts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def declaration(self) -> dict[str, Any]:
        result = {
            "tool": self.tool_id,
            "source": self.request_rep,
            "request": self.request_rep,
            "result": self.output_rep,
            "output": self.output_rep,
            "available": self.available,
            "data_scope": self.data_scope,
        }
        if self.window is not None:
            result["window"] = self.window
        return result

    def send(self, application_id: str, record: dict[str, Any], at: int) -> dict[str, Any]:
        attempt = f"syn-{self.tool_id}-{uuid4().hex[:8]}"
        call = {"at": at, "id": application_id, "attempt": attempt, "record": dict(record)}
        self.calls.append(call)
        response = self.responses.pop(0) if self.responses else {"state": "done", "body": {"status": "unknown"}}
        self._attempts[attempt] = {"application_id": application_id, "response": response}
        return {"at": at, "kind": "accepted", "record": {"id": application_id, "tool": self.tool_id, "attempt": attempt}}

    def receipt(self, attempt: str, at: int | None = None) -> dict[str, Any]:
        item = self._attempts[attempt]
        response = dict(item["response"])
        record = {"attempt": attempt, **response}
        return {"at": at if at is not None else 0, "kind": "reply", "record": record}


class SyntheticToolGateway:
    """Registry of named local synthetic tools."""

    def __init__(self, tools: list[SyntheticTool] | None = None) -> None:
        self.tools = {tool.tool_id: tool for tool in tools or []}

    def add(self, tool: SyntheticTool) -> None:
        self.tools[tool.tool_id] = tool

    def declaration_messages(self, at: int = 0) -> list[dict[str, Any]]:
        return [
            {"at": at, "kind": "tool", "record": tool.declaration()}
            for tool in sorted(self.tools.values(), key=lambda item: item.tool_id)
        ]

    def send(self, tool_id: str, application_id: str, record: dict[str, Any], at: int) -> dict[str, Any]:
        return self.tools[tool_id].send(application_id, record, at)

    def receipt(self, attempt: str, at: int, tool_id: str | None = None) -> dict[str, Any]:
        if tool_id is not None:
            return self.tools[tool_id].receipt(attempt, at)
        for tool in self.tools.values():
            if attempt in tool._attempts:
                return tool.receipt(attempt, at)
        raise KeyError(f"unknown synthetic attempt {attempt}")
