"""Pure/unit tests for MockBackendServer -- stdlib urllib only, no Docker,
no Toxiproxy. Runs in the main suite unconditionally."""

import json
import urllib.request

import pytest

from blackout_chaos.mock_backend import MockBackendServer


@pytest.fixture
def backend():
    server = MockBackendServer()
    server.start()
    yield server
    server.stop()


def _get(base_url, path):
    with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def _post(base_url, path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base_url}{path}", data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_inventory_defaults_to_100_when_never_seeded(backend):
    result = _get(backend.base_url, "/inventory/SKU-991")
    assert result == {"sku": "SKU-991", "level": 100}


def test_inventory_can_be_seeded_over_http(backend):
    _post(backend.base_url, "/inventory/SKU-991", {"level": 3})
    result = _get(backend.base_url, "/inventory/SKU-991")
    assert result == {"sku": "SKU-991", "level": 3}


def test_seed_inventory_sets_level_directly_in_process(backend):
    backend.seed_inventory("SKU-991", 7)
    result = _get(backend.base_url, "/inventory/SKU-991")
    assert result == {"sku": "SKU-991", "level": 7}


def test_restock_appends_to_ledger(backend):
    _post(backend.base_url, "/restock", {"sku": "SKU-991", "qty": 50, "window": "w1", "idempotency_key": "k1"})
    ledger = _get(backend.base_url, "/ledger")["ledger"]
    assert len(ledger) == 1
    assert ledger[0]["idempotency_key"] == "k1"


def test_restock_does_not_deduplicate_exact_duplicates(backend):
    body = {"sku": "SKU-991", "qty": 50, "window": "w1", "idempotency_key": "k1"}
    _post(backend.base_url, "/restock", body)
    _post(backend.base_url, "/restock", body)
    ledger = _get(backend.base_url, "/ledger")["ledger"]
    assert len(ledger) == 2


def test_reset_clears_ledger_and_inventory(backend):
    backend.seed_inventory("SKU-991", 3)
    _post(backend.base_url, "/restock", {"sku": "SKU-991", "qty": 1, "window": "w1", "idempotency_key": "k1"})
    _post(backend.base_url, "/reset", {})
    assert _get(backend.base_url, "/ledger")["ledger"] == []
    assert _get(backend.base_url, "/inventory/SKU-991") == {"sku": "SKU-991", "level": 100}


def test_ledger_method_returns_effect_record_instances(backend):
    _post(backend.base_url, "/restock", {"sku": "SKU-991", "qty": 1, "window": "w1", "idempotency_key": "k1"})
    records = backend.ledger()
    assert len(records) == 1
    assert records[0].idempotency_key == "k1"
    assert records[0].tool == "place_restock_order"
