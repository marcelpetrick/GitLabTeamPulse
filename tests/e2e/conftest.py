"""Browser end-to-end fixtures: real HTTP servers (fake GitLab + Team Pulse) and Chromium."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import Browser, Page, sync_playwright
from pydantic import SecretStr

from gitlab_team_pulse.app import create_app
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.fake_gitlab import FAKE_TOKEN, create_fake_gitlab


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ThreadedServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        pass


def serve(app: object, port: int) -> tuple[ThreadedServer, threading.Thread]:
    server = ThreadedServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))  # type: ignore[arg-type]
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("server did not start")
        time.sleep(0.02)
    return server, thread


@dataclass
class LiveStack:
    url: str
    gitlab: str

    def control(self, path: str, **params: object) -> dict[str, object]:
        response = httpx.post(f"{self.gitlab}/-/fake/{path}", params=params, timeout=10)
        response.raise_for_status()
        result: dict[str, object] = response.json()
        return result


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[LiveStack]:
    gitlab_port, app_port = free_port(), free_port()
    gitlab_server, gitlab_thread = serve(create_fake_gitlab(), gitlab_port)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        gitlab_url=f"http://127.0.0.1:{gitlab_port}",
        gitlab_token=SecretStr(FAKE_TOKEN),
        database_path=tmp_path / "e2e.db",
        scheduler_tick_seconds=0.3,
        manual_refresh_min_interval_seconds=1,
        ui_poll_interval_seconds=2,
    )
    app_server, app_thread = serve(create_app(settings), app_port)
    yield LiveStack(url=f"http://127.0.0.1:{app_port}", gitlab=f"http://127.0.0.1:{gitlab_port}")
    for server, thread in ((app_server, app_thread), (gitlab_server, gitlab_thread)):
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1400, "height": 950}, color_scheme="light")
    context.set_default_timeout(15000)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    yield page
    context.close()
    assert errors == [], f"JavaScript errors: {errors}"
