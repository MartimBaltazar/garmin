"""The WhatsApp gateway is tiny, but it sits between every alert and the phone.

It is not part of the exporter package (it ships as a ConfigMap), so it is
loaded from its path here.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from types import ModuleType
from typing import Any

import pytest

SCRIPT = pathlib.Path(__file__).parents[2] / "deploy" / "helper" / "whatsapp-gateway.py"


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("whatsapp_gateway", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "WEBHOOK_TOKEN", "s3cret")
    return module


@pytest.fixture
def server(gateway: ModuleType) -> Iterator[tuple[str, list[str], ModuleType]]:
    sent: list[str] = []
    gateway.send_whatsapp = lambda text: sent.append(text) or True  # type: ignore[attr-defined]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", sent, gateway
    finally:
        httpd.shutdown()


def _post(url: str, body: Any, token: str | None) -> int:
    request = urllib.request.Request(url + "/send", data=json.dumps(body).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


PAYLOAD = {
    "status": "firing",
    "commonLabels": {"alertname": "GarminMetricStoppedArriving", "severity": "warning"},
    "alerts": [
        {"annotations": {"summary": "garmin_daily_hrv_last_night_ms stopped", "description": "d"}},
        {"annotations": {"summary": "garmin_daily_vo2max stopped", "description": "d"}},
    ],
}


def test_every_alert_in_a_group_is_rendered(gateway: ModuleType) -> None:
    text = gateway.render(PAYLOAD)
    assert "hrv_last_night_ms" in text and "vo2max" in text


def test_long_messages_are_truncated(gateway: ModuleType) -> None:
    payload = {**PAYLOAD, "alerts": [{"annotations": {"summary": "x" * 5000}}]}
    assert len(gateway.render(payload)) <= gateway.MAX_MESSAGE_CHARS


def test_requests_without_the_token_are_rejected(server: tuple[str, list[str], ModuleType]) -> None:
    url, sent, _ = server
    assert _post(url, PAYLOAD, None) == 401
    assert _post(url, PAYLOAD, "wrong") == 401
    assert sent == []


def test_a_valid_request_is_sent(server: tuple[str, list[str], ModuleType]) -> None:
    url, sent, _ = server
    assert _post(url, PAYLOAD, "s3cret") == 200
    assert len(sent) == 1


def test_a_failed_send_is_reported_so_alertmanager_retries(
    server: tuple[str, list[str], ModuleType],
) -> None:
    url, _, gateway = server
    gateway.send_whatsapp = lambda text: False  # type: ignore[attr-defined]
    assert _post(url, PAYLOAD, "s3cret") == 502


def test_healthz(server: tuple[str, list[str], ModuleType]) -> None:
    url, _, _ = server
    with urllib.request.urlopen(url + "/healthz", timeout=5) as resp:
        assert resp.status == 200
