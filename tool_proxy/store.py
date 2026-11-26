"""Append-only JSONL persistence for requests, receipts and revocations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator


class JsonlEventStore:
    """A small durable event log.

    The input scenario format is also JSONL, so the same message records can be
    appended here and replayed after a simulated or real process restart.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.events_path = self.directory / "events.jsonl"

    def initialize(self) -> "JsonlEventStore":
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events_path.touch(exist_ok=True)
        return self

    def append(self, message: dict[str, Any]) -> None:
        self.initialize()
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def messages(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        result: list[dict[str, Any]] = []
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    result.append(json.loads(line))
        return result

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.messages())
