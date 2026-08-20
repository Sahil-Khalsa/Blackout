"""Injection points (docs/blackout-design.md §3.1, spec §6): 7 network
faults via Toxiproxy toxics, plus a separate tier-0 disk-exhaustion
primitive Toxiproxy can't reach (spec §11 / this plan's Global Constraints:
8 total fault injectors, not 7).

Each network injector is a context manager taking a Toxiproxy-admin-API-
shaped client and a proxy name, so runner.py can apply/clear a fault around
exactly the window a scenario names. Duck-typed (not importing
ToxiproxyClient) so this module stays stdlib-only.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Protocol

from blackout_core import IntentJournal

_TOXIC_NAME = "chaos_toxic"


class _AdminClient(Protocol):
    def set_enabled(self, proxy: str, enabled: bool) -> None: ...
    def add_toxic(self, proxy: str, name: str, type: str, stream: str = ..., **attrs: Any) -> dict: ...
    def remove_toxic(self, proxy: str, name: str) -> None: ...


@contextmanager
def pre_plan(client: _AdminClient, proxy: str) -> Iterator[None]:
    client.set_enabled(proxy, False)
    try:
        yield
    finally:
        client.set_enabled(proxy, True)


@contextmanager
def mid_plan(client: _AdminClient, proxy: str, bytes: int = 16) -> Iterator[None]:
    client.add_toxic(proxy, _TOXIC_NAME, type="limit_data", stream="downstream", bytes=bytes)
    try:
        yield
    finally:
        client.remove_toxic(proxy, _TOXIC_NAME)


@contextmanager
def post_request_pre_response(client: _AdminClient, proxy: str, timeout_ms: int = 3000) -> Iterator[None]:
    client.add_toxic(proxy, _TOXIC_NAME, type="timeout", stream="downstream", timeout=timeout_ms)
    try:
        yield
    finally:
        client.remove_toxic(proxy, _TOXIC_NAME)


@contextmanager
def partial_response(client: _AdminClient, proxy: str, bytes: int = 16) -> Iterator[None]:
    client.add_toxic(proxy, _TOXIC_NAME, type="limit_data", stream="downstream", bytes=bytes)
    try:
        yield
    finally:
        client.remove_toxic(proxy, _TOXIC_NAME)


@contextmanager
def slow_success(
    client: _AdminClient, proxy: str, latency_ms: int = 6000, jitter_ms: int = 0
) -> Iterator[None]:
    client.add_toxic(
        proxy, _TOXIC_NAME, type="latency", stream="downstream", latency=latency_ms, jitter=jitter_ms
    )
    try:
        yield
    finally:
        client.remove_toxic(proxy, _TOXIC_NAME)


@contextmanager
def flapping(client: _AdminClient, proxy: str, cycles: int = 3, interval_s: float = 0.5) -> Iterator[None]:
    try:
        for _ in range(cycles):
            client.set_enabled(proxy, False)
            time.sleep(interval_s)
            client.set_enabled(proxy, True)
            time.sleep(interval_s)
        yield
    finally:
        client.set_enabled(proxy, True)


@contextmanager
def recovery_storm(client: _AdminClient, proxy: str) -> Iterator[None]:
    """Not a toxic -- a scenario-runner behavior (spec §6): accumulate
    several deferrals under a `down` tool proxy, then set_enabled(True)
    immediately on exit so runner.py can reconcile right after and check
    the ledger for duplicates under the burst."""
    client.set_enabled(proxy, False)
    try:
        yield
    finally:
        client.set_enabled(proxy, True)


class _ExhaustedConn:
    """Proxies every attribute to the real connection except execute(),
    which raises the same error SQLite gives on a full disk. Swapped onto
    IntentJournal._conn (a plain instance attribute -- sqlite3.Connection
    itself does not allow arbitrary attribute assignment, which is why the
    swap happens one level up, on the journal, not on the connection)."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, *args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


@contextmanager
def disk_exhausted(journal: IntentJournal) -> Iterator[None]:
    """Tier-0 fault (§2.11): the journal's next write raises
    sqlite3.OperationalError('database or disk is full'). Restores both the
    real connection and journal.available on exit -- IntentJournal.append
    sets _available = False permanently on any sqlite3.Error, so without
    restoring both here, every append after this context would keep raising
    JournalUnavailable forever, not just the one write made inside it."""
    original_conn = journal._conn
    original_available = journal._available
    journal._conn = _ExhaustedConn(original_conn)
    try:
        yield
    finally:
        journal._conn = original_conn
        journal._available = original_available
