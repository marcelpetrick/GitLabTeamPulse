from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from gitlab_team_pulse.fake_gitlab import FAKE_TOKEN, build_data, create_fake_gitlab


@pytest.fixture
async def http(fake_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_app),
        base_url="http://fake.gitlab",
        headers={"PRIVATE-TOKEN": FAKE_TOKEN},
    ) as client:
        yield client


def test_build_data_is_deterministic() -> None:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    first, second = build_data(now), build_data(now)
    assert first.users == second.users
    assert len(first.projects) == 6
    assert first.issues
    assert first.merge_requests
    assert first.timelogs
    assert all(e["created_at"] <= now.isoformat().replace("+00:00", "Z") for e in first.events)


async def test_requires_token(fake_app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_app), base_url="http://fake.gitlab"
    ) as client:
        assert (await client.get("/api/v4/version")).status_code == 401


async def test_version_and_projects(http: httpx.AsyncClient) -> None:
    assert (await http.get("/api/v4/version")).json()["version"].startswith("17")
    project = (await http.get("/api/v4/projects/1")).json()
    assert project["web_url"] == "http://fake.gitlab/platform/api"
    assert (await http.get("/api/v4/projects/99")).status_code == 404


async def test_users_pagination_headers(http: httpx.AsyncClient) -> None:
    response = await http.get("/api/v4/users", params={"per_page": 10})
    assert len(response.json()) == 10
    assert response.headers["X-Next-Page"] == "2"
    assert 'rel="next"' in response.headers["Link"]
    last = await http.get("/api/v4/users", params={"per_page": 10, "page": 4})
    assert "Link" not in last.headers


async def test_outage_and_activity_controls(http: httpx.AsyncClient, fake_app: FastAPI) -> None:
    before = (await http.get("/-/fake/state")).json()
    assert before["down"] is False
    await http.post("/-/fake/outage", params={"down": "true"})
    assert (await http.get("/api/v4/version")).status_code == 503
    await http.post("/-/fake/outage", params={"down": "false"})
    created = (await http.post("/-/fake/activity", params={"user_id": 3})).json()
    events = (await http.get("/api/v4/users/3/events", params={"per_page": 100})).json()
    assert events[0]["id"] == created["event_id"]
    assert (await http.get("/-/fake/state")).json()["events"] == before["events"] + 1


async def test_work_filters(http: httpx.AsyncClient, clock: object) -> None:
    issues = (await http.get("/api/v4/issues", params={"assignee_id": 2, "per_page": 100})).json()
    assert issues
    assert all(i["assignees"][0]["id"] == 2 for i in issues)
    assert issues[0]["references"]["full"].count("#") == 1
    cutoff = datetime.now(UTC) + timedelta(days=1)
    none = await http.get("/api/v4/issues", params={"updated_after": cutoff.isoformat()})
    assert none.json() == []
    reviews = await http.get("/api/v4/merge_requests", params={"reviewer_id": 2, "per_page": 100})
    assert all(any(r["id"] == 2 for r in m["reviewers"]) for m in reviews.json())


async def test_graphql(http: httpx.AsyncClient) -> None:
    unsupported = await http.post("/api/graphql", json={"query": "{ metadata { version } }"})
    assert unsupported.json()["errors"]
    body = {
        "query": "query { timelogs }",
        "variables": {"username": "nobody", "start": None, "end": None},
    }
    empty = (await http.post("/api/graphql", json=body)).json()
    assert empty["data"]["timelogs"]["nodes"] == []


def test_non_admin_hides_inactive_accounts() -> None:
    app = create_fake_gitlab(build_data(), admin=False)
    assert app.state.admin is False


async def test_latency_control(http: httpx.AsyncClient, fake_app: FastAPI) -> None:
    assert (await http.post("/-/fake/latency", params={"seconds": 99})).json() == {"latency": 10.0}
    await http.post("/-/fake/latency", params={"seconds": 0.01})
    assert (await http.get("/api/v4/version")).status_code == 200
    assert (await http.get("/-/fake/state")).json()["latency"] == 0.01


@pytest.fixture
async def browser(fake_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client without the API token, like a browser following a demo link."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_app), base_url="http://fake.gitlab"
    ) as client:
        yield client


async def _page(client: httpx.AsyncClient, url: str, status: int = 200) -> str:
    response = await client.get(url)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>" in response.text
    assert "Fake GitLab (demo)" in response.text
    assert "real GitLab instance" in response.text
    return response.text


async def test_web_pages_for_demo_links(
    http: httpx.AsyncClient, browser: httpx.AsyncClient
) -> None:
    user = next(u for u in (await http.get("/api/v4/users")).json() if u["id"] == 2)
    profile = await _page(browser, user["web_url"])
    assert f"@{user['username']}" in profile
    assert user["name"] in profile
    project = (await http.get("/api/v4/projects/1")).json()
    assert "<code>platform/api</code>" in await _page(browser, project["web_url"])
    issue = (await http.get("/api/v4/issues")).json()[0]
    issue_page = await _page(browser, issue["web_url"])
    assert f"Issue <strong>#{issue['iid']}</strong>" in issue_page
    mr = (await http.get("/api/v4/merge_requests")).json()[0]
    mr_page = await _page(browser, mr["web_url"])
    assert f"Merge request <strong>!{mr['iid']}</strong>" in mr_page
    assert f"<code>{mr['references']['full'].split('!')[0]}</code>" in mr_page
    epic = await _page(browser, "/groups/platform/-/epics/1")
    assert "Q4 platform reliability" in epic


@pytest.mark.parametrize(
    "path",
    [
        "/nobody.here",
        "/platform/nope",
        "/platform/api/-/issues/999999",
        "/platform/api/-/merge_requests/999999",
        "/platform/api/-/pipelines/1",
        "/groups/platform/-/epics/99",
        "/<script>alert(1)</script>",
    ],
)
async def test_unknown_web_pages_are_html_404(browser: httpx.AsyncClient, path: str) -> None:
    text = await _page(browser, path, status=404)
    assert "<script>" not in text


async def test_web_pages_do_not_touch_api_state(
    http: httpx.AsyncClient, browser: httpx.AsyncClient, fake_app: FastAPI
) -> None:
    await http.post("/-/fake/outage", params={"down": "true"})
    before, log = fake_app.state.requests, list(fake_app.state.request_log)
    await _page(browser, "/platform/api")
    await _page(browser, "/")
    assert fake_app.state.requests == before
    assert fake_app.state.request_log == log
    assert (await http.get("/api/v4/version")).status_code == 503
    await http.post("/-/fake/outage", params={"down": "false"})
    # API routes and the control routes keep their JSON behaviour.
    assert (await browser.get("/api/v4/version")).status_code == 401
    unknown = await http.get("/api/v4/nonexistent")
    assert unknown.status_code == 404
    assert unknown.headers["content-type"].startswith("application/json")
    assert (await http.get("/-/fake/state")).json()["down"] is False
    assert (await browser.get("/-/fake/unknown")).status_code == 404
