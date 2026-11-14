"""WhatsApp gateway for Alertmanager webhooks.

Receives Alertmanager webhook POSTs on /send and forwards them to WhatsApp via
CallMeBot (https://www.callmebot.com/), which is free and needs no account:

1. Message the CallMeBot WhatsApp number to get an API key.
2. `make whatsapp-secret PHONE=+351... APIKEY=...`
3. Alertmanager already points here (deploy/config/alertmanager.yaml).

Standard library only, so it runs on a stock python image from a ConfigMap
with nothing to build.

Contract with Alertmanager:

* Requests must carry ``Authorization: Bearer <WEBHOOK_TOKEN>``. Alertmanager
  cannot sign webhooks, but it sends this header from ``credentials_file``.
* A failed WhatsApp send answers 502, so Alertmanager counts it as failed and
  retries, instead of a 200 that silently drops the alert.
* GET /healthz is for the kubelet probes.

Environment: WEBHOOK_TOKEN (required), WHATSAPP_PHONE and WHATSAPP_APIKEY
(without them the gateway accepts alerts, logs them, and sends nothing).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("whatsapp-gateway")

WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "")
WHATSAPP_APIKEY = os.getenv("WHATSAPP_APIKEY", "")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
#: WhatsApp accepts long messages, but CallMeBot passes text in a GET query.
MAX_MESSAGE_CHARS = 1500
MAX_BODY_BYTES = 1 << 20


def render(payload: dict[str, Any]) -> str:
    """One message per webhook, one line per alert.

    Alertmanager groups by alertname, so one webhook can carry several alerts
    whose labels differ (e.g. one per metric_name). ``commonAnnotations`` would
    drop exactly the part that differs, so render each alert's own summary.
    """
    status = str(payload.get("status", "unknown")).upper()
    labels = payload.get("commonLabels") or {}
    alertname = labels.get("alertname", "unknown")
    severity = labels.get("severity", "")
    alerts = payload.get("alerts") or []

    icon = "✅" if status == "RESOLVED" else {"critical": "🔴", "warning": "🟠"}.get(severity, "ℹ️")
    lines = [f"{icon} *{alertname}* [{status}]"]
    for alert in alerts:
        annotations = alert.get("annotations") or {}
        summary = annotations.get("summary") or alertname
        lines.append(f"• {summary}")
    first = (alerts[0].get("annotations") or {}) if alerts else {}
    if first.get("description") and status != "RESOLVED":
        lines.append("")
        lines.append(first["description"])

    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"
    return text


def send_whatsapp(text: str) -> bool:
    """Send via CallMeBot. True on success, or when sending is not configured."""
    if not WHATSAPP_PHONE or not WHATSAPP_APIKEY:
        log.warning("WHATSAPP_PHONE/WHATSAPP_APIKEY not set; would have sent: %s", text)
        return True
    query = urllib.parse.urlencode(
        {"phone": WHATSAPP_PHONE, "text": text, "apikey": WHATSAPP_APIKEY}
    )
    try:
        with urllib.request.urlopen(f"{CALLMEBOT_URL}?{query}", timeout=15) as resp:
            ok = 200 <= resp.status < 300
            # The URL carries the API key: log the response, never the request.
            log.info("callmebot answered %s", resp.status)
            return ok
    except Exception as exc:  # noqa: BLE001 - report, let Alertmanager retry
        log.error("WhatsApp send failed: %s", type(exc).__name__)
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "whatsapp-gateway"

    def _reply(self, code: int, body: str = "") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._reply(200, "ok")
        else:
            self._reply(404)

    def do_POST(self) -> None:
        if self.path != "/send":
            self._reply(404)
            return
        expected = f"Bearer {WEBHOOK_TOKEN}"
        given = self.headers.get("Authorization", "")
        if not WEBHOOK_TOKEN or not hmac.compare_digest(given, expected):
            self._reply(401, "unauthorized")
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(400, "bad length")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._reply(400, "bad json")
            return
        if not isinstance(payload, dict):
            self._reply(400, "bad payload")
            return

        if send_whatsapp(render(payload)):
            self._reply(200, "sent")
        else:
            self._reply(502, "upstream failed")

    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not WEBHOOK_TOKEN:
        log.warning("WEBHOOK_TOKEN not set: every request will be rejected")
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("WhatsApp gateway listening on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
