"""Thin requests-based wrapper over Toxiproxy's admin REST API
(docs/blackout-design.md §3, spec §5). No Toxiproxy-specific pip dependency
-- just `requests`, part of the new `chaos` extra (chaos = ["requests",
"pyyaml"]).

Docker isn't launched by the test suite -- docker-compose.yml at the repo
root starts Toxiproxy (`docker compose up -d`). Live tests self-skip if
localhost:8474 isn't reachable, same convention as test_ollama_integration.py.
"""

from __future__ import annotations

from typing import Any

import requests


class ToxiproxyClient:
    def __init__(self, base_url: str = "http://localhost:8474") -> None:
        self.base_url = base_url.rstrip("/")

    def create_proxy(self, name: str, listen: str, upstream: str) -> dict:
        resp = requests.post(
            f"{self.base_url}/proxies",
            json={"name": name, "listen": listen, "upstream": upstream},
            timeout=5.0,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_proxy(self, name: str) -> None:
        resp = requests.delete(f"{self.base_url}/proxies/{name}", timeout=5.0)
        if resp.status_code not in (204, 404):
            resp.raise_for_status()

    def add_toxic(
        self, proxy: str, name: str, type: str, stream: str = "downstream", **attributes: Any
    ) -> dict:
        resp = requests.post(
            f"{self.base_url}/proxies/{proxy}/toxics",
            json={"name": name, "type": type, "stream": stream, "attributes": attributes},
            timeout=5.0,
        )
        resp.raise_for_status()
        return resp.json()

    def remove_toxic(self, proxy: str, name: str) -> None:
        resp = requests.delete(f"{self.base_url}/proxies/{proxy}/toxics/{name}", timeout=5.0)
        if resp.status_code not in (204, 404):
            resp.raise_for_status()

    def set_enabled(self, proxy: str, enabled: bool) -> None:
        resp = requests.post(
            f"{self.base_url}/proxies/{proxy}", json={"enabled": enabled}, timeout=5.0
        )
        resp.raise_for_status()

    def reset(self) -> None:
        resp = requests.post(f"{self.base_url}/reset", timeout=5.0)
        resp.raise_for_status()


def toxiproxy_reachable(base_url: str = "http://localhost:8474") -> bool:
    try:
        requests.get(f"{base_url}/version", timeout=2.0)
        return True
    except requests.exceptions.RequestException:
        return False
