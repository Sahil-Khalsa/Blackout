"""Durable intent journal.

An append-only SQLite table in WAL mode. SQLite is already a crash-safe append
log with well-understood durability semantics -- rebuilding that is the classic
mistake here.

Two design rules run through the whole module:

1.  Expiry is computed from a monotonic clock, never from stored wall-clock
    arithmetic. Offline devices drift. `created_at` exists only so a human can
    read a timestamp in the approval inbox; nothing branches on it.

2.  Any failure to write is a hard escalation to tier 0, never a silent
    swallow. An agent that believes it deferred an action when it did not is
    strictly more dangerous than one that refuses outright.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .policy import PreconditionValue

_EPOCH_BASE = 1_700_000_000_000


class IntentStatus(StrEnum):
    PENDING = "pending"
    REPLAYED = "replayed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONFLICTED = "conflicted"
    COLLAPSED = "collapsed"
    ORPHANED = "orphaned"
    UNCERTAIN = "uncertain"
    CORRUPT = "corrupt"


class JournalUnavailable(RuntimeError):
    """Raised when the journal cannot durably record an intent.

    The runtime must catch this and drop to tier 0. It must never be handled by
    proceeding as though the deferral succeeded.
    """


def _ulid() -> str:
    """Lexicographically sortable ID. Sorting the journal by id sorts it by
    creation time, so replay ordering is free."""
    ms = int(time.time() * 1000) - _EPOCH_BASE
    return f"{ms:011x}{uuid.uuid4().hex[:14]}"


@dataclass(slots=True)
class Intent:
    tool: str
    args: dict[str, Any]
    idempotency_key: str
    tier_at_creation: int
    ttl_seconds: int
    precondition_snapshot: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_source_age_s: float = 0.0
    reasoning_trace_id: str | None = None
    task_checkpoint_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    id: str = field(default_factory=_ulid)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    created_mono_ns: int = field(default_factory=time.monotonic_ns)
    boot_id: str = ""
    status: IntentStatus = IntentStatus.PENDING
    resolution_note: str = ""

    @classmethod
    def from_evaluation(
        cls,
        tool: str,
        args: Mapping[str, Any],
        idempotency_key: str,
        tier_at_creation: int,
        ttl_seconds: int,
        preconditions: Sequence[PreconditionValue] = (),
        **kwargs: Any,
    ) -> Intent:
        snapshot = {
            p.name: {"value": p.value, "source_age_s": p.source_age_s}
            for p in preconditions
        }
        worst = max((p.source_age_s for p in preconditions), default=0.0)
        return cls(
            tool=tool,
            args=dict(args),
            idempotency_key=idempotency_key,
            tier_at_creation=tier_at_creation,
            ttl_seconds=ttl_seconds,
            precondition_snapshot=snapshot,
            max_source_age_s=worst,
            **kwargs,
        )

    def age_s(self, now_ns: int | None = None) -> float:
        return ((now_ns or time.monotonic_ns()) - self.created_mono_ns) / 1e9

    def is_expired(self, now_ns: int | None = None, current_boot: str = "") -> bool:
        """Expired if past TTL, or if it predates the current boot.

        Monotonic clocks reset across reboots, so an intent carrying a
        monotonic stamp from a previous boot cannot be compared to the current
        one. Treat those as expired rather than guessing -- conservative, and
        easy to defend.
        """
        if current_boot and self.boot_id and self.boot_id != current_boot:
            return True
        return self.age_s(now_ns) > self.ttl_seconds


class IntentJournal:
    def __init__(self, path: str | Path, secret: bytes | None = None) -> None:
        self.path = Path(path)
        self._secret = secret or os.urandom(32)
        self.boot_id = uuid.uuid4().hex
        self._available = True
        try:
            self._conn = sqlite3.connect(self.path, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._migrate()
        except sqlite3.DatabaseError as exc:
            # WAL corruption on open: refuse to start the agent loop at all.
            self._available = False
            raise JournalUnavailable(f"cannot open journal at {self.path}: {exc}") from exc

    @property
    def available(self) -> bool:
        return self._available

    def _migrate(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intents (
                id                  TEXT PRIMARY KEY,
                created_at          TEXT NOT NULL,
                created_mono_ns     INTEGER NOT NULL,
                boot_id             TEXT NOT NULL,
                tier_at_creation    INTEGER NOT NULL,
                tool                TEXT NOT NULL,
                args                TEXT NOT NULL,
                idempotency_key     TEXT NOT NULL,
                precondition_snapshot TEXT NOT NULL,
                max_source_age_s    REAL NOT NULL,
                reasoning_trace_id  TEXT,
                task_checkpoint_id  TEXT,
                depends_on          TEXT NOT NULL,
                ttl_seconds         INTEGER NOT NULL,
                status              TEXT NOT NULL,
                resolution_note     TEXT NOT NULL DEFAULT '',
                hmac                TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_status ON intents(status, id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_idem ON intents(idempotency_key)"
        )

    def _sign(self, row: Mapping[str, Any]) -> str:
        payload = json.dumps(
            {k: row[k] for k in sorted(row) if k != "hmac"},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def append(self, intent: Intent) -> Intent:
        """Durably record an intent.

        Raises JournalUnavailable on any I/O failure. The caller must escalate
        to tier 0 rather than continuing.
        """
        if not self._available:
            raise JournalUnavailable("journal marked unavailable")

        intent.boot_id = self.boot_id
        row = self._to_row(intent)
        row["hmac"] = self._sign(row)
        cols = ", ".join(row)
        placeholders = ", ".join(f":{c}" for c in row)
        try:
            self._conn.execute(f"INSERT INTO intents ({cols}) VALUES ({placeholders})", row)
        except sqlite3.Error as exc:
            self._available = False
            raise JournalUnavailable(f"append failed: {exc}") from exc
        return intent

    def pending(self, now_ns: int | None = None) -> list[Intent]:
        """Pending intents in creation order, with expiry applied as a side
        effect so the caller always sees a settled view."""
        rows = self._conn.execute(
            "SELECT * FROM intents WHERE status = ? ORDER BY id",
            (IntentStatus.PENDING,),
        ).fetchall()

        out: list[Intent] = []
        for row in rows:
            intent = self._verify_and_load(row)
            if intent is None:
                continue
            if intent.is_expired(now_ns, self.boot_id):
                self.resolve(intent.id, IntentStatus.EXPIRED, "ttl elapsed or boot changed")
                continue
            out.append(intent)
        return out

    def by_status(self, status: IntentStatus) -> list[Intent]:
        rows = self._conn.execute(
            "SELECT * FROM intents WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
        return [i for i in (self._verify_and_load(r) for r in rows) if i is not None]

    def duplicates_of(self, idempotency_key: str) -> list[Intent]:
        rows = self._conn.execute(
            "SELECT * FROM intents WHERE idempotency_key = ? AND status = ? ORDER BY id",
            (idempotency_key, IntentStatus.PENDING),
        ).fetchall()
        return [i for i in (self._verify_and_load(r) for r in rows) if i is not None]

    def resolve(self, intent_id: str, status: IntentStatus, note: str = "") -> None:
        """Transition an intent and re-sign it.

        Re-signing on transition means a hand-edited status is detectable, not
        just a hand-edited payload.
        """
        row = self._conn.execute(
            "SELECT * FROM intents WHERE id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no intent {intent_id!r}")
        data = dict(row)
        data["status"] = str(status)
        data["resolution_note"] = note
        data["hmac"] = self._sign(data)
        try:
            self._conn.execute(
                "UPDATE intents SET status = :status, resolution_note = :resolution_note, "
                "hmac = :hmac WHERE id = :id",
                data,
            )
        except sqlite3.Error as exc:
            self._available = False
            raise JournalUnavailable(f"resolve failed: {exc}") from exc

    def orphan_missing_checkpoints(self, live_checkpoint_ids: Iterable[str]) -> list[Intent]:
        """Mark intents whose task checkpoint did not survive.

        These are never auto-replayed. They surface in the approval inbox in a
        separate section flagged as lacking justification context --
        orphaned-but-visible beats both silently-replayed and silently-dropped.
        """
        live = set(live_checkpoint_ids)
        orphaned = []
        for intent in self.pending():
            if intent.task_checkpoint_id and intent.task_checkpoint_id not in live:
                self.resolve(intent.id, IntentStatus.ORPHANED, "task checkpoint lost")
                intent.status = IntentStatus.ORPHANED
                orphaned.append(intent)
        return orphaned

    def verify_all(self) -> list[str]:
        """Eagerly verify every record, quarantining any that fail.

        Verification is otherwise lazy -- it happens only when a row is read
        through `pending()` or `by_status()`. That means tampering with an
        already-resolved record would go unnoticed indefinitely, since nothing
        routinely reads them. Run this on startup and before building an
        approval batch. Returns the IDs quarantined.
        """
        rows = self._conn.execute(
            "SELECT * FROM intents WHERE status != ?", (IntentStatus.CORRUPT,)
        ).fetchall()
        quarantined = []
        for row in rows:
            data = dict(row)
            expected = data.pop("hmac")
            if not hmac.compare_digest(self._sign(data), expected):
                self._quarantine(data["id"])
                quarantined.append(data["id"])
        return quarantined

    def corrupt_records(self) -> list[dict[str, Any]]:
        """Quarantined rows, returned raw and unverified.

        These deliberately bypass HMAC verification -- a corrupt record fails
        verification by definition, so routing it through the normal loader
        would filter it out of every read path and make it silently disappear.
        Silent loss is the exact failure mode the journal exists to prevent, so
        corruption surfaces in the approval inbox flagged unreviewable.
        """
        rows = self._conn.execute(
            "SELECT id, created_at, tool, idempotency_key, resolution_note "
            "FROM intents WHERE status = ? ORDER BY id",
            (IntentStatus.CORRUPT,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _verify_and_load(self, row: sqlite3.Row) -> Intent | None:
        """Quarantine on HMAC mismatch, keep serving the rest.

        One bad row must not take down the queue. The corrupt record still
        surfaces in the approval inbox, flagged unreviewable.
        """
        data = dict(row)
        expected = data.pop("hmac")
        if not hmac.compare_digest(self._sign(data), expected):
            self._quarantine(data["id"])
            return None
        return self._from_row(data)

    def _quarantine(self, intent_id: str) -> None:
        try:
            self._conn.execute(
                "UPDATE intents SET status = ?, resolution_note = ? WHERE id = ?",
                (IntentStatus.CORRUPT, "hmac mismatch", intent_id),
            )
        except sqlite3.Error:
            self._available = False

    @staticmethod
    def _to_row(intent: Intent) -> dict[str, Any]:
        d = asdict(intent)
        d["args"] = json.dumps(d["args"], sort_keys=True)
        d["precondition_snapshot"] = json.dumps(d["precondition_snapshot"], sort_keys=True)
        d["depends_on"] = json.dumps(d["depends_on"])
        d["status"] = str(d["status"])
        return d

    @staticmethod
    def _from_row(data: Mapping[str, Any]) -> Intent:
        d = dict(data)
        d["args"] = json.loads(d["args"])
        d["precondition_snapshot"] = json.loads(d["precondition_snapshot"])
        d["depends_on"] = json.loads(d["depends_on"])
        d["status"] = IntentStatus(d["status"])
        return Intent(**d)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> IntentJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
