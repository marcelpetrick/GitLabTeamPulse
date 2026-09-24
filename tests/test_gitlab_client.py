from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from pydantic import SecretStr

from gitlab_team_pulse.gitlab.client import GitLabClient
from gitlab_team_pulse.gitlab.errors import (
    GitLabAuthError,
    GitLabCapabilityError,
    GitLabError,
    GitLabForbiddenError,
    GitLabNotFoundError,
    GitLabRateLimitError,
    GitLabResponseError,
    GitLabUnavailableError,
)

BASE = "https://gitlab.test"
API = f"{BASE}/api/v4"
TOKEN = "glpat-super-secret"


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
async def client(sleeps: Sleeps) -> AsyncIterator[GitLabClient]:
    async with GitLabClient(BASE, SecretStr(TOKEN), max_retries=2, sleep=sleeps, max_pages=5) as c:
        yield c


def user(uid: int) -> dict[str, object]:
    return {"id": uid, "username": f"u{uid}", "name": f"User {uid}"}


@respx.mock
async def test_token_header_and_version(client: GitLabClient) -> None:
    route = respx.get(f"{API}/version").mock(
        return_value=httpx.Response(200, json={"version": "17.4.0"})
    )
    assert await client.version() == "17.4.0"
    request = route.calls.last.request
    assert request.headers["PRIVATE-TOKEN"] == TOKEN
    assert request.headers["User-Agent"].startswith("gitlab-team-pulse/")
    assert client.request_count == 1


@respx.mock
async def test_version_rejects_bad_payload(client: GitLabClient) -> None:
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"nope": 1}))
    with pytest.raises(GitLabResponseError):
        await client.version()


@respx.mock
async def test_list_users_follows_link_pagination(client: GitLabClient) -> None:
    respx.get(f"{API}/users", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=[user(3)])
    )
    respx.get(f"{API}/users").mock(
        return_value=httpx.Response(
            200,
            json=[user(1), user(2)],
            headers={"Link": f'<{API}/users?page=2&per_page=100>; rel="next"'},
        )
    )
    users = await client.list_users()
    assert [u.id for u in users] == [1, 2, 3]


@respx.mock
async def test_x_next_page_pagination_and_limit(client: GitLabClient) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        headers = {"X-Next-Page": str(page + 1)} if page < 3 else {}
        return httpx.Response(200, json=[user(page)], headers=headers)

    respx.get(f"{API}/users").mock(side_effect=responder)
    assert [u.id for u in await client.list_users()] == [1, 2, 3]
    assert len(await client.paginate("users", limit=2)) == 2


@respx.mock
async def test_pagination_stops_at_max_pages(client: GitLabClient) -> None:
    respx.get(f"{API}/users").mock(
        side_effect=lambda r: httpx.Response(200, json=[user(1)], headers={"X-Next-Page": "2"})
    )
    assert len(await client.paginate("users")) == 5


@respx.mock
async def test_pagination_refuses_foreign_host(client: GitLabClient) -> None:
    respx.get(f"{API}/users").mock(
        return_value=httpx.Response(
            200, json=[], headers={"Link": '<https://evil.test/api/v4/users?page=2>; rel="next"'}
        )
    )
    with pytest.raises(GitLabResponseError, match="another host"):
        await client.list_users()


@respx.mock
async def test_pagination_requires_lists_and_json(client: GitLabClient) -> None:
    respx.get(f"{API}/users").mock(return_value=httpx.Response(200, json={"a": 1}))
    with pytest.raises(GitLabResponseError, match="list"):
        await client.list_users()
    respx.get(f"{API}/user").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(GitLabResponseError, match="JSON"):
        await client.current_user()


@respx.mock
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, GitLabAuthError),
        (403, GitLabForbiddenError),
        (404, GitLabNotFoundError),
        (418, GitLabError),
    ],
)
async def test_client_errors_fail_fast(
    client: GitLabClient, sleeps: Sleeps, status: int, error: type[GitLabError]
) -> None:
    route = respx.get(f"{API}/user").mock(return_value=httpx.Response(status))
    with pytest.raises(error) as info:
        await client.current_user()
    assert route.call_count == 1
    assert sleeps.calls == []
    assert info.value.status == status
    assert TOKEN not in str(info.value)


@respx.mock
async def test_server_errors_are_retried_then_succeed(client: GitLabClient, sleeps: Sleeps) -> None:
    route = respx.get(f"{API}/user").mock(
        side_effect=[httpx.Response(502), httpx.Response(503), httpx.Response(200, json=user(1))]
    )
    assert (await client.current_user()).id == 1
    assert route.call_count == 3
    assert len(sleeps.calls) == 2
    assert 0.5 <= sleeps.calls[0] <= 1.0
    assert 1.0 <= sleeps.calls[1] <= 1.5


@respx.mock
async def test_retries_are_bounded(client: GitLabClient, sleeps: Sleeps) -> None:
    route = respx.get(f"{API}/user").mock(return_value=httpx.Response(500))
    with pytest.raises(GitLabUnavailableError, match="server error"):
        await client.current_user()
    assert route.call_count == 3
    assert len(sleeps.calls) == 2


@respx.mock
async def test_rate_limit_honors_retry_after(client: GitLabClient, sleeps: Sleeps) -> None:
    respx.get(f"{API}/user").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429, headers={"Retry-After": "9999"}),
            httpx.Response(429, headers={"Retry-After": "soon"}),
        ]
    )
    with pytest.raises(GitLabRateLimitError):
        await client.current_user()
    assert sleeps.calls[:2] == [7.0, 60.0]


@respx.mock
async def test_network_failures_are_retried(client: GitLabClient, sleeps: Sleeps) -> None:
    respx.get(f"{API}/user").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), httpx.ConnectError("x")]
    )
    with pytest.raises(GitLabUnavailableError, match="connection failed"):
        await client.current_user()
    assert len(sleeps.calls) == 2


@respx.mock
async def test_timeout_message(sleeps: Sleeps) -> None:
    respx.get(f"{API}/user").mock(side_effect=httpx.ReadTimeout("slow"))
    async with GitLabClient(BASE, SecretStr(TOKEN), max_retries=0, sleep=sleeps) as c:
        with pytest.raises(GitLabUnavailableError, match="timed out"):
            await c.current_user()


async def test_concurrency_is_bounded() -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={"version": "1"})

    transport = httpx.MockTransport(handler)
    async with GitLabClient(BASE, SecretStr(TOKEN), concurrency=2, transport=transport) as c:
        await asyncio.gather(*(c.version() for _ in range(10)))
    assert peak == 2


@respx.mock
async def test_get_project(client: GitLabClient) -> None:
    respx.get(f"{API}/projects/3").mock(
        return_value=httpx.Response(
            200, json={"id": 3, "name": "App", "path_with_namespace": "g/app"}
        )
    )
    project = await client.get_project(3)
    assert project.path_with_namespace == "g/app"


@respx.mock
async def test_get_user_work_queries_all_states(client: GitLabClient) -> None:
    item = {
        "id": 1,
        "iid": 1,
        "title": "t",
        "state": "closed",
        "updated_at": "2026-09-20T10:00:00Z",
    }
    issues = respx.get(f"{API}/issues").mock(return_value=httpx.Response(200, json=[item]))
    assigned = respx.get(f"{API}/merge_requests", params={"assignee_id": "5"}).mock(
        return_value=httpx.Response(200, json=[{**item, "id": 2}])
    )
    review = respx.get(f"{API}/merge_requests", params={"reviewer_id": "5"}).mock(
        return_value=httpx.Response(200, json=[{**item, "id": 3}])
    )
    work = await client.get_user_work(5, datetime(2026, 9, 1, tzinfo=UTC))
    assert [(w.kind, w.relation) for w in work] == [
        ("issue", "assignee"),
        ("merge_request", "assignee"),
        ("merge_request", "reviewer"),
    ]
    params = issues.calls.last.request.url.params
    assert params["state"] == "all"
    assert params["scope"] == "all"
    assert params["updated_after"] == "2026-09-01T00:00:00+00:00"
    assert assigned.called
    assert review.called


@respx.mock
async def test_get_user_recent_activity(client: GitLabClient) -> None:
    route = respx.get(f"{API}/users/5/events").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "action_name": "opened", "created_at": "2026-09-20T10:00:00Z"}]
        )
    )
    events = await client.get_user_recent_activity(5, date(2026, 9, 10))
    assert events[0].summary == "Opened"
    assert route.calls.last.request.url.params["after"] == "2026-09-10"


def timelog_node(tid: int, username: str = "alex") -> dict[str, object]:
    return {
        "id": f"gid://gitlab/Timelog/{tid}",
        "spentAt": "2026-09-20T08:00:00Z",
        "timeSpent": 1800,
        "user": {"username": username},
    }


@respx.mock
async def test_timelogs_paginate_and_filter_foreign_users(client: GitLabClient) -> None:
    route = respx.post(f"{BASE}/api/graphql").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": {
                        "timelogs": {
                            "nodes": [timelog_node(1), timelog_node(2, "other")],
                            "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                        }
                    }
                },
            ),
            httpx.Response(
                200,
                json={"data": {"timelogs": {"nodes": [timelog_node(3)], "pageInfo": {}}}},
            ),
        ]
    )
    start = datetime(2026, 9, 14, tzinfo=UTC)
    logs = await client.get_user_timelogs("alex", start, datetime(2026, 9, 21, tzinfo=UTC))
    assert [log.id for log in logs] == [1, 3]
    assert b'"after":"c1"' in route.calls.last.request.content


@respx.mock
@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"errors": [{"message": "Field 'timelogs' doesn't exist"}]}, GitLabCapabilityError),
        ({"errors": ["plain"]}, GitLabUnavailableError),
        ({"errors": [{"message": "Request timed out"}]}, GitLabUnavailableError),
        ({"errors": [{"message": "Timelogs require admin access"}]}, GitLabCapabilityError),
        ({"data": None}, GitLabResponseError),
        ({"data": {"timelogs": None}}, GitLabResponseError),
        ([1, 2], GitLabResponseError),
    ],
)
async def test_graphql_failures(client: GitLabClient, body: object, error: type[Exception]) -> None:
    respx.post(f"{BASE}/api/graphql").mock(return_value=httpx.Response(200, json=body))
    now = datetime(2026, 9, 21, tzinfo=UTC)
    with pytest.raises(error):
        await client.get_user_timelogs("alex", now, now)


@respx.mock
async def test_recent_activity_limit_without_date(client: GitLabClient) -> None:
    route = respx.get(f"{API}/users/5/events").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": i, "action_name": "opened", "created_at": "2026-09-01T10:00:00Z"}
                for i in range(12)
            ],
        )
    )
    events = await client.get_user_recent_activity(5, limit=12)
    assert len(events) == 12
    params = route.calls.last.request.url.params
    assert "after" not in params
    assert params["per_page"] == "12"


@respx.mock
async def test_epics_skip_personal_namespaces_and_paginate(client: GitLabClient) -> None:
    node = {
        "id": "gid://gitlab/WorkItem/5",
        "iid": 1,
        "title": "E",
        "updatedAt": "2026-09-20T10:00:00Z",
    }
    respx.post(f"{BASE}/api/graphql").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"group": None}}),
            httpx.Response(
                200,
                json={
                    "data": {
                        "group": {
                            "workItems": {
                                "nodes": [node],
                                "pageInfo": {"hasNextPage": True, "endCursor": "c"},
                            }
                        }
                    }
                },
            ),
            httpx.Response(
                200,
                json={"data": {"group": {"workItems": {"nodes": [node], "pageInfo": {}}}}},
            ),
        ]
    )
    now = datetime(2026, 9, 21, tzinfo=UTC)
    epics = await client.get_user_epics("alex", {"alex", "grp"}, now)
    assert [e.gitlab_id for e in epics] == [5]


@respx.mock
async def test_epics_malformed_connection(client: GitLabClient) -> None:
    respx.post(f"{BASE}/api/graphql").mock(
        return_value=httpx.Response(200, json={"data": {"group": {"workItems": None}}})
    )
    with pytest.raises(GitLabResponseError):
        await client.get_user_epics("alex", {"grp"}, datetime(2026, 9, 21, tzinfo=UTC))


@respx.mock
async def test_redirect_is_a_configuration_error(client: GitLabClient, sleeps: Sleeps) -> None:
    respx.get(f"{API}/user").mock(
        return_value=httpx.Response(301, headers={"Location": "https://gitlab.test/users/sign_in"})
    )
    with pytest.raises(GitLabError, match=r"redirected \(301\).*TEAMPULSE_GITLAB_URL") as info:
        await client.current_user()
    assert info.value.status == 301
    assert sleeps.calls == []


@pytest.mark.parametrize(
    ("message", "capability"),
    [
        ("Field 'workItems' doesn't exist on type 'Group'", True),
        ("Argument 'types' on Field 'workItems' has an invalid value (EPIC)", True),
        ("You don't have permission to perform this action", True),
        ("This feature requires a Premium license", True),
        ("Internal server error", False),
        ("Request timed out", False),
    ],
)
def test_capability_classification(message: str, capability: bool) -> None:
    from gitlab_team_pulse.gitlab.client import is_capability_message

    assert is_capability_message(message) is capability
