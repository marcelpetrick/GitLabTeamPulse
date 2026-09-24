from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gitlab_team_pulse import db
from gitlab_team_pulse.app import AppContext, create_app
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.fake_gitlab import build_data, create_fake_gitlab

from .conftest import Clock, fake_client_factory

ALEX = 2


@pytest.fixture
def api(
    settings: Settings, fake_app: FastAPI, clock: Clock
) -> Iterator[tuple[TestClient, AppContext]]:
    app = create_app(
        settings.model_copy(update={"scheduler_enabled": False}),
        client_factory=fake_client_factory(fake_app),
    )
    with TestClient(app) as client:
        context: AppContext = app.state.ctx
        context.service.clock = clock
        yield client, context


def sync_all(client: TestClient, context: AppContext) -> None:
    assert client.portal is not None
    client.portal.call(context.service.sync_directory, "manual")
    client.portal.call(context.service.sync_selected, "manual")


def test_health(api: tuple[TestClient, AppContext]) -> None:
    client, context = api
    body = client.get("/api/health").json()
    assert body == {
        "status": "ok",
        "version": body["version"],
        "database": True,
        "migrated": True,
        "gitlab_configured": True,
    }
    db.downgrade(context.engine, "0001")
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["migrated"] is False


def test_initial_status_is_never_synced(api: tuple[TestClient, AppContext]) -> None:
    client, _ = api
    status = client.get("/api/status").json()
    assert status["freshness"] == "never"
    assert status["crawler"]["state"] == "idle"
    assert status["users"] == {"known": 0, "selected": 0}
    assert status["upstream"]["status"] == "unknown"
    assert status["gitlab"] == {"configured": True, "url": "http://fake.gitlab"}
    assert status["ui_poll_interval_seconds"] == 20


def test_user_directory_and_selection(api: tuple[TestClient, AppContext]) -> None:
    client, context = api
    assert client.portal is not None
    client.portal.call(context.service.sync_directory, "startup")
    listing = client.get("/api/users").json()
    assert len(listing["users"]) == 32
    assert listing["directory"]["status"] == "fresh"
    assert {u["account_type"] for u in listing["users"]} >= {"bot", "service", "human"}
    version = listing["data_version"]

    assert client.patch("/api/users/999/selection", json={"selected": True}).status_code == 404
    assert client.patch("/api/users/2/selection", json={"selected": "maybe"}).status_code == 422
    selected = client.patch(f"/api/users/{ALEX}/selection", json={"selected": True}).json()
    assert selected["selected"] is True
    status = client.get("/api/status").json()
    assert status["users"]["selected"] == 1
    assert status["crawler"]["refresh_pending"] is True
    assert status["crawler"]["state"] == "refreshing"
    assert status["data_version"] > version
    client.patch(f"/api/users/{ALEX}/selection", json={"selected": False})
    assert client.get("/api/status").json()["users"]["selected"] == 0


def test_dashboard_payload(api: tuple[TestClient, AppContext]) -> None:
    client, context = api
    empty = client.get("/api/dashboard").json()
    assert empty["cards"] == []
    assert empty["activity_days"] == 7
    assert client.portal is not None
    client.portal.call(context.service.sync_directory, "startup")
    client.patch(f"/api/users/{ALEX}/selection", json={"selected": True})
    client.portal.call(context.service.sync_selected, "manual")
    body = client.get("/api/dashboard").json()
    assert body["status"]["freshness"] == "fresh"
    card = body["cards"][0]
    assert card["user"]["id"] == ALEX
    assert card["freshness"]["status"] == "fresh"
    assert set(card["freshness"]["categories"]) == {"work", "activity", "timelogs"}
    assert card["counts"]["work_total"] == len(card["work"]) > 0
    assert card["counts"]["work_open"] <= card["counts"]["work_total"]
    updated = [w["updated_at"] for w in card["work"]]
    assert updated == sorted(updated, reverse=True)
    assert all(w["web_url"].startswith("http://fake.gitlab/") for w in card["work"])
    assert all(w["project"]["path"] for w in card["work"])
    assert 0 < len(card["activity"]) <= 12
    assert card["activity_preview_count"] == 5
    assert len(card["activity_days"]) == 7
    assert card["counts"]["events_7d"] == sum(d["total"] for d in card["activity_days"])
    time = card["time"]
    assert time["total_seconds"] == sum(d["seconds"] for d in time["days"]) > 0
    assert len(time["days"]) == 7
    assert sum(p["seconds"] for p in time["projects"]) == time["total_seconds"]
    assert time["entries"][0]["web_url"].startswith("http://fake.gitlab/")


def test_per_user_endpoints(api: tuple[TestClient, AppContext]) -> None:
    client, context = api
    client.portal.call(context.service.sync_directory, "startup")  # type: ignore[union-attr]
    client.patch(f"/api/users/{ALEX}/selection", json={"selected": True})
    client.portal.call(context.service.sync_selected, "manual")  # type: ignore[union-attr]
    work = client.get(f"/api/users/{ALEX}/work").json()
    assert work["work"]
    activity = client.get(f"/api/users/{ALEX}/activity").json()
    assert len(activity["days"]) == 7
    time = client.get(f"/api/users/{ALEX}/time-summary").json()
    assert "total_seconds" in time
    assert client.get("/api/users/999/work").status_code == 404


def test_errors_endpoint_reports_cached_data(
    api: tuple[TestClient, AppContext], fake_app: FastAPI, clock: Clock
) -> None:
    client, context = api
    sync_all(client, context)
    client.patch(f"/api/users/{ALEX}/selection", json={"selected": True})
    client.portal.call(context.scheduler.tick)  # type: ignore[union-attr]
    client.portal.call(context.scheduler.wait_idle)  # type: ignore[union-attr]
    fake_app.state.down = True
    clock.advance(minutes=30)
    client.portal.call(context.service.sync_selected, "scheduled")  # type: ignore[union-attr]
    body = client.get("/api/errors").json()
    assert body["unresolved"] == 3
    first = body["errors"][0]
    assert first["user"]["id"] == ALEX
    assert first["cached_data_shown"] is True
    assert first["last_success_at"] is not None
    assert first["related_dataset"].endswith(f":{ALEX}")
    status = client.get("/api/status").json()
    assert status["freshness"] == "error"
    assert status["selected"]["stale"] is True
    assert status["crawler"]["state"] == "error"
    assert status["upstream"]["status"] == "error"
    card = client.get("/api/dashboard").json()["cards"][0]
    assert card["freshness"]["status"] == "error"
    assert card["work"]  # last known good data is still served
    only_open = client.get("/api/errors", params={"include_resolved": False, "limit": 1}).json()
    assert len(only_open["errors"]) == 1


def test_directory_error_payload(api: tuple[TestClient, AppContext], fake_app: FastAPI) -> None:
    client, context = api
    fake_app.state.down = True
    client.portal.call(context.service.sync_directory, "startup")  # type: ignore[union-attr]
    error = client.get("/api/errors").json()["errors"][0]
    assert error["related_dataset"] == "directory"
    assert error["cached_data_shown"] is False
    assert error["user"] is None


def test_refresh_endpoint(api: tuple[TestClient, AppContext], clock: Clock) -> None:
    client, context = api
    first = client.post("/api/refresh")
    assert first.status_code == 202
    assert first.json() == {"accepted": True, "status": "started"}
    assert client.post("/api/refresh").json()["status"] == "coalesced"
    client.portal.call(context.scheduler.tick)  # type: ignore[union-attr]
    client.portal.call(context.scheduler.wait_idle)  # type: ignore[union-attr]
    throttled = client.post("/api/refresh")
    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"] == "10"
    clock.advance(seconds=11)
    assert client.post("/api/refresh").status_code == 202


def test_security_headers_and_index(api: tuple[TestClient, AppContext]) -> None:
    client, _ = api
    page = client.get("/")
    assert page.status_code == 200
    assert "GitLab Team Pulse" in page.text
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["Referrer-Policy"] == "no-referrer"
    api_response = client.get("/api/status")
    assert api_response.headers["Cache-Control"] == "no-store"
    assert "access-control-allow-origin" not in api_response.headers
    docs = client.get("/api/docs")
    assert "Content-Security-Policy" not in docs.headers


def test_token_never_leaks(
    tmp_path: object, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "glpat-VERY-SECRET-123"
    fake = create_fake_gitlab(build_data(clock.now), token=secret)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        gitlab_url="http://fake.gitlab",
        gitlab_token=SecretStr(secret),
        database_path=f"{tmp_path}/leak.db",
        scheduler_enabled=False,
    )

    def factory(s: Settings):  # type: ignore[no-untyped-def]
        client = fake_client_factory(fake)(s)
        client._http.headers["PRIVATE-TOKEN"] = secret
        return client

    app = create_app(settings, client_factory=factory)
    with TestClient(app) as client:
        context: AppContext = app.state.ctx
        client.portal.call(context.service.sync_directory, "startup")  # type: ignore[union-attr]
        client.patch(f"/api/users/{ALEX}/selection", json={"selected": True})
        fake.state.down = True
        client.portal.call(context.service.sync_selected, "manual")  # type: ignore[union-attr]
        import logging

        logging.getLogger("teampulse.test").error("echo %s", secret)
        bodies = [
            client.get(path).text
            for path in [
                "/api/health",
                "/api/status",
                "/api/users",
                "/api/dashboard",
                "/api/errors",
            ]
        ]
    assert all(secret not in body for body in bodies)
    assert secret not in capsys.readouterr().out


def test_selection_is_global_and_survives_restart(
    settings: Settings, fake_app: FastAPI, clock: Clock
) -> None:
    config = settings.model_copy(update={"scheduler_enabled": False})
    first = create_app(config, client_factory=fake_client_factory(fake_app))
    with TestClient(first) as client_a, TestClient(first) as client_b:
        client_a.portal.call(first.state.ctx.service.sync_directory, "startup")  # type: ignore[union-attr]
        client_a.patch(f"/api/users/{ALEX}/selection", json={"selected": True})
        assert client_b.get("/api/status").json()["users"]["selected"] == 1
    second = create_app(config, client_factory=fake_client_factory(fake_app))
    with TestClient(second) as client:
        users = client.get("/api/users").json()["users"]
        assert [u["id"] for u in users if u["selected"]] == [ALEX]
        assert client.get("/api/status").json()["directory"]["status"] in {"fresh", "stale"}
