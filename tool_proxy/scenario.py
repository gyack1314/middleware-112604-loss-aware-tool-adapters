"""Run local JSONL drills through the tool proxy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scenario_reader import iter_messages

from .service import SUPPORTED_KINDS, ToolProxy
from .store import JsonlEventStore


def run_scenario(
    path: str | Path,
    store_directory: str | Path | None = None,
    auto_transform: bool = True,
    auto_deliver: bool = True,
) -> dict[str, Any]:
    path = Path(path)
    store = JsonlEventStore(store_directory) if store_directory is not None else None
    if store is not None:
        store.initialize()

    proxy = ToolProxy(store=store, auto_transform=auto_transform, auto_deliver=auto_deliver)
    inspections: list[dict[str, Any]] = []
    message_count = 0

    for message in iter_messages(path, SUPPORTED_KINDS):
        message_count += 1
        result = proxy.dispatch(message) if store is not None else proxy.handle(message)

        if message["kind"] == "restart":
            # Replay the durable/in-memory log, including the restart marker.
            proxy.recover_in_place()

        if message["kind"] == "inspect":
            inspections.append(
                {
                    "at": message["at"],
                    "request": message["record"],
                    "result": result,
                }
            )

    return {
        "scenario": path.name,
        "message_count": message_count,
        "inspections": inspections,
        "application_ids": sorted(proxy.apps),
        "citation_ids": sorted(proxy.citations),
        "proxy": proxy,
    }
