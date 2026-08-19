"""Durable task checkpoints, alongside the intent journal.

docs/blackout-design.md §2.8: intents are durable but the agent's
in-progress task/plan is not, and that asymmetry is a bug -- a crash
mid-partition would leave a queue of pending intents with no memory of the
task that produced them. CheckpointStore closes that gap.

Same conventions as journal.py: its own SQLite connection (WAL supports
multiple connections to one file, so this points at the journal's file path
without touching journal.py's internals), rows HMAC-signed for
tamper-evidence, lazy verification on read. Deliberately without
journal.py's monotonic-clock/TTL machinery -- checkpoints don't expire, so
there is nothing here that branches on elapsed time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

_EPOCH_BASE = 1_700_000_000_000


class CheckpointStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


def _ulid() -> str:
    ms = int(time.time() * 1000) - _EPOCH_BASE
    return f"{ms:011x}{uuid.uuid4().hex[:14]}"


@dataclass(slots=True)
class Checkpoint:
    task: str
    reasoning_trace_id: str | None = None
    completed_steps: list[dict[str, Any]] = field(default_factory=list)
    pending_plan: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=_ulid)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: CheckpointStatus = CheckpointStatus.ACTIVE


class CheckpointStore:
    def __init__(self, path: str | Path, secret: bytes | None = None) -> None:
        self.path = Path(path)
        self._secret = secret or os.urandom(32)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id                  TEXT PRIMARY KEY,
                task                TEXT NOT NULL,
                reasoning_trace_id  TEXT,
                completed_steps     TEXT NOT NULL,
                pending_plan        TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                status              TEXT NOT NULL,
                hmac                TEXT NOT NULL
            )
            """
        )

    def _sign(self, row: dict[str, Any]) -> str:
        payload = json.dumps(
            {k: row[k] for k in sorted(row) if k != "hmac"},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def start(self, task: str, reasoning_trace_id: str | None = None) -> Checkpoint:
        checkpoint = Checkpoint(task=task, reasoning_trace_id=reasoning_trace_id)
        row = self._to_row(checkpoint)
        row["hmac"] = self._sign(row)
        cols = ", ".join(row)
        placeholders = ", ".join(f":{c}" for c in row)
        self._conn.execute(f"INSERT INTO checkpoints ({cols}) VALUES ({placeholders})", row)
        return checkpoint

    def record_step(self, checkpoint_id: str, step: dict[str, Any]) -> None:
        checkpoint = self._get_or_raise(checkpoint_id)
        checkpoint.completed_steps.append(step)
        self._update(checkpoint)

    def set_pending_plan(self, checkpoint_id: str, plan: dict[str, Any]) -> None:
        checkpoint = self._get_or_raise(checkpoint_id)
        checkpoint.pending_plan = plan
        self._update(checkpoint)

    def complete(self, checkpoint_id: str) -> None:
        checkpoint = self._get_or_raise(checkpoint_id)
        checkpoint.status = CheckpointStatus.COMPLETED
        self._update(checkpoint)

    def abandon(self, checkpoint_id: str) -> None:
        checkpoint = self._get_or_raise(checkpoint_id)
        checkpoint.status = CheckpointStatus.ABANDONED
        self._update(checkpoint)

    def live_ids(self) -> set[str]:
        """IDs of every checkpoint that loads and verifies, regardless of
        status -- completion doesn't erase a record. One that fails HMAC
        verification (or was never written) is simply absent, which is what
        makes journal.orphan_missing_checkpoints's "missing or corrupt"
        check work without a separate quarantine mechanism."""
        rows = self._conn.execute("SELECT * FROM checkpoints").fetchall()
        return {
            checkpoint.id
            for checkpoint in (self._verify_and_load(row) for row in rows)
            if checkpoint is not None
        }

    def _get_or_raise(self, checkpoint_id: str) -> Checkpoint:
        checkpoint = self.get(checkpoint_id)
        if checkpoint is None:
            raise KeyError(f"no checkpoint {checkpoint_id!r}")
        return checkpoint

    def _update(self, checkpoint: Checkpoint) -> None:
        row = self._to_row(checkpoint)
        row["hmac"] = self._sign(row)
        self._conn.execute(
            "UPDATE checkpoints SET task = :task, reasoning_trace_id = :reasoning_trace_id, "
            "completed_steps = :completed_steps, pending_plan = :pending_plan, "
            "created_at = :created_at, status = :status, hmac = :hmac WHERE id = :id",
            row,
        )

    def get(self, checkpoint_id: str) -> Checkpoint | None:
        row = self._conn.execute(
            "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            return None
        return self._verify_and_load(row)

    def _verify_and_load(self, row: sqlite3.Row) -> Checkpoint | None:
        data = dict(row)
        expected = data.pop("hmac")
        if not hmac.compare_digest(self._sign(data), expected):
            return None
        return self._from_row(data)

    @staticmethod
    def _to_row(checkpoint: Checkpoint) -> dict[str, Any]:
        d = asdict(checkpoint)
        d["completed_steps"] = json.dumps(d["completed_steps"])
        d["pending_plan"] = json.dumps(d["pending_plan"], sort_keys=True)
        d["status"] = str(d["status"])
        return d

    @staticmethod
    def _from_row(data: dict[str, Any]) -> Checkpoint:
        d = dict(data)
        d["completed_steps"] = json.loads(d["completed_steps"])
        d["pending_plan"] = json.loads(d["pending_plan"])
        d["status"] = CheckpointStatus(d["status"])
        return Checkpoint(**d)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CheckpointStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
