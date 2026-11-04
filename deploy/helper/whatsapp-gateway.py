"""
WhatsApp gateway for Alertmanager webhooks.

Receives Alertmanager webhook POSTs and forwards them to WhatsApp via
CallMeBot (https://www.callmebot.com/). Free, no account needed:

1. Send a message to the CallMeBot WhatsApp number to get your API key
2. Set WHATSAPP_PHONE and WHATSAPP_APIKEY environment variables
3. Run: python whatsapp-gateway.py
4. Point Alertmanager's whatsapp webhook at this service

Alertmanager webhook JSON looks like:
  {
    "status": "firing",
    "commonLabels": {"alertname": "GarminExporterStale", ...},
    "commonAnnotations": {"summary": "...", "description": "..."},
    "alerts": [...]
  }
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger("whatsapp-gateway")

WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "")
WHATSAPP_APIKEY = os.getenv("WHATSAPP_APIKEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


def send_whatsapp(text: str) -> None:
    """Send a text message via CallMeBot WhatsApp API."""
    if not WHATSAPP_PHONE or not WHATSAPP_APIKEY:
        log.warning("WHATSAPP_PHONE/WHATSAPP_APIKEY not set; not sending")
        return
    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={urllib.parse.quote(WHATSAPP_PHONE)}"
        f"&text={urllib.parse.quote(text)}"
        f"&apikey={urllib.parse.quote(WHATSAPP_APIKEY)}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode()
            log.info("WhatsApp sent: %s", body[:200])
    except Exception as exc:  # noqa: BLE001 - never crash the webhook
        log.error("WhatsApp send failed: %s", exc)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # Optional HMAC verification
        if WEBHOOK_SECRET:
            sig = self.headers.get("X-Hook-Signature", "")
            expected = hmac.new(
                WEBHOOK_SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                self.send_response(401)
                self.end_headers()
                return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        status = payload.get("status", "unknown")
        labels = payload.get("commonLabels", {})
        annotations = payload.get("commonAnnotations", {})
        alertname = labels.get("alertname", "unknown")
        summary = annotations.get("summary", "")
        description = annotations.get("description", "")

        text = (
            f"*Garmin Alert [{status.upper()}]*\n"
            f"*{alertname}*\n{summary}\n{description}"
        ).strip()

        send_whatsapp(text)

        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - standard signature
        log.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    log.info("WhatsApp gateway listening on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
