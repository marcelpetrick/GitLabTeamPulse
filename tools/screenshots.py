"""Regenerate README screenshots from the built-in demo.

Usage: uv run python tools/screenshots.py   (starts the demo on free ports, writes docs/media/*.png)
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "media"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app_port, gitlab_port = free_port(), free_port()
    base = f"http://127.0.0.1:{app_port}"
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            sys.executable,
            "-m",
            "gitlab_team_pulse",
            "demo",
            "--port",
            str(app_port),
            "--gitlab-port",
            str(gitlab_port),
            "--database",
            f"{tmp}/demo.db",
        ]
        server = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(f"{base}/api/health", timeout=1)  # noqa: S310
                    break
                except OSError:
                    time.sleep(0.2)
            time.sleep(1.5)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                for theme in ("light", "dark"):
                    page = browser.new_page(
                        viewport={"width": 1440, "height": 1000}, color_scheme=theme
                    )
                    page.goto(f"{base}/#/dashboard")
                    page.wait_for_selector(".card .work-table")
                    page.wait_for_selector(".milestone-progress", timeout=30000)
                    page.wait_for_timeout(500)
                    if theme == "light":
                        page.screenshot(path=OUT / "attention.png")
                        # The fixed status bar would overlap an element screenshot.
                        bar = "document.getElementById('statusbar').style.visibility"
                        page.evaluate(f"{bar} = 'hidden'")
                        page.locator("section.milestones").screenshot(path=OUT / "milestones.png")
                        page.evaluate(f"{bar} = ''")
                    # The person cards sit below the attention strip and the milestones.
                    page.evaluate(
                        "window.scrollTo(0, document.querySelector('.cards').offsetTop - 70)"
                    )
                    page.wait_for_timeout(300)
                    page.screenshot(path=OUT / f"dashboard-{theme}.png")
                page = browser.new_page(
                    viewport={"width": 1440, "height": 1300}, color_scheme="light"
                )
                page.goto(f"{base}/#/teams")
                page.wait_for_selector(".pill.fresh >> text=History", timeout=60000)
                page.wait_for_selector(".kpis")
                page.wait_for_timeout(500)
                page.screenshot(path=OUT / "teams.png")
                page.get_by_role("button", name="Edit").click()
                page.wait_for_selector(".member-option")
                page.wait_for_timeout(300)
                page.set_viewport_size({"width": 1440, "height": 900})
                page.screenshot(path=OUT / "team-editor.png")
                page.keyboard.press("Escape")
                page.click("#open-about")
                page.wait_for_selector("#about .about-deps tbody tr")
                page.wait_for_timeout(300)
                page.screenshot(path=OUT / "about.png")
                page.keyboard.press("Escape")
                page = browser.new_page(
                    viewport={"width": 1440, "height": 900}, color_scheme="light"
                )
                page.goto(f"{base}/#/people")
                page.wait_for_selector(".people-row[data-user-id]")
                page.fill("#people-filter", "a")
                page.screenshot(path=OUT / "people.png")
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{gitlab_port}/-/fake/outage?down=true", method="POST"
                    )
                )
                page.goto(f"{base}/#/dashboard")
                page.wait_for_selector(".card .work-table")
                page.click("#refresh-now")
                page.wait_for_selector(".banner.err", timeout=30000)
                page.click("#open-diagnostics")
                page.wait_for_selector(".diag-item")
                page.screenshot(path=OUT / "outage-diagnostics.png")
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)
    print(f"screenshots written to {OUT}")


if __name__ == "__main__":
    main()
