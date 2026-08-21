"""Mock backend for the chaos harness (docs/blackout-design.md §3.4).

Runs as a background thread inside the test process -- no subprocess, no
Docker. Threaded (ThreadingMixIn, not the default single-connection-at-a-time
HTTPServer) because a fault-injection test can hold one connection open
under a `timeout` toxic while the test's own /ledger polling request must
still get through on a different connection without deadlocking.

Bound to 127.0.0.1 by default (loopback-only, no firewall prompt). The live
end-to-end test explicitly passes host="0.0.0.0" so a Toxiproxy container
can reach it via host.docker.internal -- see docker-compose.yml.

Deliberately dumb and honest: POST /restock always appends to the ledger,
including exact duplicates. It does not deduplicate -- the duplicate-effect
detector needs ground truth to diff the agent's own idempotency logic
against, not an inferred one.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class EffectRecord:
    id: str
    received_at: str
    tool: str
    idempotency_key: str
    payload: dict


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        backend: MockBackendServer = self.server.backend  # type: ignore[attr-defined]
        if path.startswith("/inventory/"):
            sku = path.removeprefix("/inventory/")
            with backend._lock:
                level = backend._inventory.get(sku, 100)
            self._send_json(200, {"sku": sku, "level": level})
            return
        if path == "/ledger":
            with backend._lock:
                records = [asdict(r) for r in backend._ledger]
            self._send_json(200, {"ledger": records})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        backend: MockBackendServer = self.server.backend  # type: ignore[attr-defined]
        body = self._read_json()
        if path.startswith("/inventory/"):
            sku = path.removeprefix("/inventory/")
            with backend._lock:
                backend._inventory[sku] = body["level"]
            self._send_json(200, {"sku": sku, "level": body["level"]})
            return
        if path == "/restock":
            record = EffectRecord(
                id=uuid.uuid4().hex,
                received_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                tool="place_restock_order",
                idempotency_key=body.get("idempotency_key", ""),
                payload=body,
            )
            with backend._lock:
                backend._ledger.append(record)
            self._send_json(200, {"accepted": True, "id": record.id})
            return
        if path == "/reset":
            with backend._lock:
                backend._inventory.clear()
                backend._ledger.clear()
            self._send_json(200, {"reset": True})
            return
        self._send_json(404, {"error": "not found"})


class MockBackendServer:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self._lock = threading.Lock()
        self._inventory: dict[str, int] = {}
        self._ledger: list[EffectRecord] = []
        self._httpd = _ThreadingHTTPServer((host, 0), _Handler)
        self._httpd.backend = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def seed_inventory(self, sku: str, level: int) -> None:
        with self._lock:
            self._inventory[sku] = level

    def ledger(self) -> list[EffectRecord]:
        with self._lock:
            return list(self._ledger)

    def reset(self) -> None:
        with self._lock:
            self._inventory.clear()
            self._ledger.clear()

    def __enter__(self) -> MockBackendServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
