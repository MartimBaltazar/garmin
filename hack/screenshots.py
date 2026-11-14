"""Capture the README screenshots from a running Grafana.

Run through `make screenshots` (which starts this in the Playwright container)
while `make grafana` is port-forwarding in another terminal. Writes PNGs to
assets/screenshots/, which the README links to.

Grafana's kiosk mode hides the navigation chrome, so the images show only the
dashboards. Basic auth uses the same admin password `make secret` stored.
"""

from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
USER = os.environ.get("GRAFANA_USER", "admin")
PASSWORD = os.environ.get("GRAFANA_PASSWORD", "admin")
OUT = os.environ.get("OUT_DIR", "/out")

#: (file name, dashboard path + query, viewport height). Heights fit each
#: dashboard's full grid so nothing is cut off.
SHOTS = (
    ("garmin-health.png", "/d/garmin-health?orgId=1&kiosk&from=now-14d&to=now", 2200),
    ("garmin-pipeline.png", "/d/garmin-pipeline?orgId=1&kiosk&from=now-24h&to=now", 1000),
    ("garmin-health-week.png", "/d/garmin-health?orgId=1&kiosk&from=now-7d&to=now", 1100),
)


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            http_credentials={"username": USER, "password": PASSWORD},
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=2,
            color_scheme="dark",
        )
        page = context.new_page()
        for name, path, height in SHOTS:
            page.set_viewport_size({"width": 1600, "height": height})
            page.goto(BASE + path, wait_until="networkidle", timeout=60_000)
            # Panels render after their queries resolve; networkidle covers the
            # queries, this covers the canvas paint.
            page.wait_for_timeout(3_000)
            target = os.path.join(OUT, name)
            page.screenshot(path=target, full_page=True)
            print(f"wrote {target}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
