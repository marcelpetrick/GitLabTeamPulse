"""A small synthetic GitLab API for demos, integration tests and browser end-to-end tests.

It implements just the endpoints Team Pulse uses, with deterministic data relative to the
moment it was created. Control endpoints under ``/-/fake/`` simulate outages and new activity.
It is never used when a real ``TEAMPULSE_GITLAB_URL`` is configured.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

FAKE_TOKEN = "fake-token"  # noqa: S105 - public demo credential for the fake server only

FIRST = ["Alex", "Sam", "Robin", "Kim", "Jona", "Mika", "Toni", "Charlie", "Luca", "Noa"]
LAST = ["Berger", "Novak", "Silva", "Kowalski", "Lindqvist", "Okafor", "Tanaka", "Moreau"]
PROJECTS = [
    ("platform", "api"),
    ("platform", "web"),
    ("platform", "infra"),
    ("tools", "cli"),
    ("docs", "handbook"),
    ("research", "ml-lab"),
]
LABELS = ["bug", "feature", "tech-debt", "priority::high", "priority::medium", "docs", "ux"]
ISSUE_TITLES = [
    "Fix flaky login redirect",
    "Add CSV export to reports",
    "Upgrade PostgreSQL client library",
    "Dark mode contrast on tables",
    "Document the release checklist",
    "Cache warmup after deploy",
    "Improve error message for expired tokens",
    "Pagination breaks with 10k+ rows",
    "Support custom CA bundles",
    "Rename confusing config keys",
    "Reduce Docker image size",
    "Nightly backup verification",
]
MR_TITLES = [
    "Refactor session handling",
    "Draft: new onboarding flow",
    "Bump dependencies",
    "Add retry with jitter to webhook sender",
    "Split monolithic settings module",
    "Speed up CI with layer caching",
]
COMMENTS = ["Looks good to me", "Can we add a test?", "Rebased on main", "Deployed to staging"]


@dataclass
class FakeData:
    now: datetime
    users: list[dict[str, Any]] = field(default_factory=list)
    projects: dict[int, dict[str, Any]] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    merge_requests: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    timelogs: list[dict[str, Any]] = field(default_factory=list)
    epics: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 1


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _user(uid: int, username: str, name: str, created: datetime, **extra: Any) -> dict[str, Any]:
    """Build a user payload; ``bot=None``/``user_type=None`` omit the metadata entirely."""
    payload = {
        "id": uid,
        "username": username,
        "name": name,
        "state": extra.pop("state", "active"),
        "avatar_url": None,
        "web_url": "",
        "created_at": _iso(created),
        "bot": extra.pop("bot", False),
        "user_type": extra.pop("user_type", "human"),
        "is_admin": extra.pop("is_admin", False),
        **extra,
    }
    return {k: v for k, v in payload.items() if k not in ("bot", "user_type") or v is not None}


def build_data(now: datetime | None = None, seed: int = 7) -> FakeData:
    """Generate a deterministic instance: 32 accounts, 6 projects, work, events and timelogs."""
    now = now or datetime.now(UTC)
    rng = random.Random(seed)  # noqa: S311 - synthetic data, not cryptography
    data = FakeData(now=now)
    data.users.append(_user(1, "root", "Administrator", now - timedelta(days=2000), is_admin=True))
    uid = 2
    for index in range(24):
        first, last = FIRST[index % len(FIRST)], LAST[(index * 3) % len(LAST)]
        state = "blocked" if index in (20, 21) else ("deactivated" if index == 22 else "active")
        data.users.append(
            _user(
                uid,
                f"{first.lower()}.{last.lower()}{index if index >= len(FIRST) else ''}",
                f"{first} {last}",
                now - timedelta(days=1500 - index * 55),
                state=state,
            )
        )
        uid += 1
    for username, name, user_type in [
        ("project_7_bot", "Project Access Token Bot", "project_bot"),
        ("alert-bot", "GitLab Alert Bot", "alert_bot"),
        ("renovate", "Renovate Service", "service_account"),
        ("ci-deployer", "CI Deployer", "service_account"),
        ("support-bot", "GitLab Support Bot", "support_bot"),
        ("ghost", "Ghost User", "ghost"),
        ("legacy-sync", "Legacy Sync (no type)", ""),
    ]:
        data.users.append(
            _user(
                uid,
                username,
                name,
                now - timedelta(days=rng.randint(10, 900)),
                bot=None if not user_type else user_type != "service_account",
                user_type=user_type or None,
            )
        )
        uid += 1
    for pid, (group, path) in enumerate(PROJECTS, start=1):
        data.projects[pid] = {
            "id": pid,
            "name": path.replace("-", " ").title(),
            "path_with_namespace": f"{group}/{path}",
            "path": path,
        }

    active = [u for u in data.users[1:13] if u["state"] == "active"]
    item_id = 1000
    for user in active[:10]:
        for number in range(rng.randint(4, 8)):
            item_id += 1
            project = rng.choice(list(data.projects))
            updated = now - timedelta(hours=rng.randint(1, 20 * 24))
            state = rng.choice(["opened", "opened", "closed"])
            data.issues.append(
                {
                    "id": item_id,
                    "iid": 10 + number + item_id % 90,
                    "project_id": project,
                    "title": rng.choice(ISSUE_TITLES),
                    "state": state,
                    "labels": rng.sample(LABELS, rng.randint(0, 3)),
                    "milestone": {"title": rng.choice(["2026.10", "2026.11", "Backlog"])},
                    "due_date": (now + timedelta(days=rng.randint(-5, 30))).date().isoformat()
                    if rng.random() < 0.5
                    else None,
                    "created_at": _iso(updated - timedelta(days=rng.randint(1, 40))),
                    "updated_at": _iso(updated),
                    "closed_at": _iso(updated) if state == "closed" else None,
                    "author": {"username": rng.choice(active)["username"]},
                    "assignees": [{"id": user["id"], "username": user["username"]}],
                    "issue_type": rng.choice(["issue", "issue", "issue", "task", "incident"]),
                }
            )
        for number in range(rng.randint(2, 4)):
            item_id += 1
            updated = now - timedelta(hours=rng.randint(1, 15 * 24))
            state = rng.choice(["opened", "merged", "merged", "closed"])
            reviewer = rng.choice([u for u in active if u is not user])
            title = rng.choice(MR_TITLES)
            data.merge_requests.append(
                {
                    "id": item_id,
                    "iid": 100 + number + item_id % 50,
                    "project_id": rng.choice(list(data.projects)),
                    "title": title,
                    "state": state,
                    "draft": title.startswith("Draft"),
                    "labels": rng.sample(LABELS, rng.randint(0, 2)),
                    "milestone": None,
                    "created_at": _iso(updated - timedelta(days=rng.randint(1, 10))),
                    "updated_at": _iso(updated),
                    "merged_at": _iso(updated) if state == "merged" else None,
                    "closed_at": _iso(updated) if state == "closed" else None,
                    "author": {"username": user["username"]},
                    "assignees": [{"id": user["id"], "username": user["username"]}],
                    "reviewers": [{"id": reviewer["id"], "username": reviewer["username"]}],
                }
            )
        for _ in range(rng.randint(12, 36)):
            add_event(data, user["id"], now - timedelta(minutes=rng.randint(5, 9 * 24 * 60)), rng)
        # A year of older history for the contribution calendar: busier on weekdays, with
        # quiet stretches (holidays) so the graph looks like a real profile.
        vacation = rng.randint(20, 330)
        for days_ago in range(10, 366):
            if vacation <= days_ago < vacation + 14:
                continue
            weekday = (now - timedelta(days=days_ago)).weekday()
            if rng.random() < (0.12 if weekday >= 5 else 0.72):
                for _ in range(rng.choice([1, 1, 2, 3, 4, 6, 9, 14])):
                    minute = rng.randint(8 * 60, 18 * 60)
                    add_event(data, user["id"], now - timedelta(days=days_ago, minutes=minute), rng)
        timelog_id = len(data.timelogs)
        for day in range(7):
            if rng.random() < 0.25:
                continue
            for _ in range(rng.randint(1, 3)):
                timelog_id += 1
                issue = rng.choice(
                    [i for i in data.issues if i["assignees"][0]["id"] == user["id"]]
                )
                spent = now - timedelta(days=day, hours=rng.randint(0, 8))
                data.timelogs.append(
                    {
                        "id": timelog_id,
                        "user_id": user["id"],
                        "spent_at": spent,
                        "seconds": rng.choice([900, 1800, 3600, 5400, 7200, 10800]),
                        "summary": rng.choice([None, "Investigation", "Review", "Pairing"]),
                        "issue": issue,
                    }
                )
    for number, user in enumerate(active[:3], start=1):
        updated = now - timedelta(days=number * 2)
        data.epics.append(
            {
                "id": f"gid://gitlab/WorkItem/{9000 + number}",
                "iid": number,
                "group": "platform",
                "title": ["Q4 platform reliability", "Self-service onboarding", "API v2"][
                    number - 1
                ],
                "state": "OPEN" if number != 2 else "CLOSED",
                "createdAt": _iso(updated - timedelta(days=30)),
                "updatedAt": _iso(updated),
                "closedAt": _iso(updated) if number == 2 else None,
                "reference": f"platform&{number}",
                "author": {"username": "root"},
                "assignee": user["username"],
                "labels": ["roadmap", "priority::high"] if number == 1 else ["roadmap"],
                "dueDate": (now + timedelta(days=40)).date().isoformat(),
            }
        )
    return data


def add_event(
    data: FakeData, user_id: int, when: datetime, rng: random.Random | None = None
) -> dict[str, Any]:
    rng = rng or random.Random()  # noqa: S311
    project_id = rng.choice(list(data.projects))
    kind = rng.choice(["push", "push", "push", "comment", "comment", "issue", "mr", "joined"])
    event: dict[str, Any] = {
        "id": data.next_event_id,
        "project_id": project_id,
        "author_id": user_id,
        "created_at": _iso(when),
        "target_type": None,
        "target_title": None,
        "target_iid": None,
    }
    data.next_event_id += 1
    if kind == "push":
        count = rng.randint(1, 5)
        event |= {
            "action_name": "pushed to",
            "push_data": {
                "commit_count": count,
                "action": "pushed",
                "ref_type": "branch",
                "ref": rng.choice(["main", "feature/export", "fix/login"]),
                "commit_title": rng.choice(ISSUE_TITLES),
            },
        }
    elif kind == "comment":
        iid = rng.randint(1, 60)
        event |= {
            "action_name": "commented on",
            "target_type": "Note",
            "target_title": rng.choice(ISSUE_TITLES),
            "note": {
                "id": data.next_event_id * 10,
                "body": rng.choice(COMMENTS),
                "noteable_type": rng.choice(["Issue", "MergeRequest"]),
                "noteable_iid": iid,
            },
        }
    elif kind == "issue":
        event |= {
            "action_name": rng.choice(["opened", "closed"]),
            "target_type": "Issue",
            "target_iid": rng.randint(1, 60),
            "target_title": rng.choice(ISSUE_TITLES),
        }
    elif kind == "mr":
        event |= {
            "action_name": rng.choice(["opened", "accepted", "approved"]),
            "target_type": "MergeRequest",
            "target_iid": rng.randint(1, 40),
            "target_title": rng.choice(MR_TITLES),
        }
    else:
        event |= {"action_name": "joined"}
    data.events.append(event)
    return event


def _paginate(request: Request, items: list[dict[str, Any]]) -> JSONResponse:
    page = max(int(request.query_params.get("page", "1")), 1)
    per_page = min(max(int(request.query_params.get("per_page", "20")), 1), 100)
    start = (page - 1) * per_page
    chunk = items[start : start + per_page]
    headers = {"X-Page": str(page), "X-Per-Page": str(per_page), "X-Total": str(len(items))}
    if start + per_page < len(items):
        params = dict(request.query_params)
        params["page"] = str(page + 1)
        query = "&".join(f"{key}={value}" for key, value in params.items())
        next_url = f"{str(request.base_url).rstrip('/')}{request.url.path}?{query}"
        headers["Link"] = f'<{next_url}>; rel="next"'
        headers["X-Next-Page"] = str(page + 1)
    return JSONResponse(chunk, headers=headers)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace(" ", "+"))


def create_fake_gitlab(
    data: FakeData | None = None, *, token: str = FAKE_TOKEN, admin: bool = True
) -> FastAPI:
    """Build the fake GitLab ASGI app; state lives on ``app.state``."""
    app = FastAPI(title="Fake GitLab", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.data = data or build_data()
    app.state.down = False
    app.state.admin = admin
    app.state.requests = 0
    app.state.request_log = []  # (path, query) of every API request, for load assertions
    app.state.graphql_error = None  # set to a message to simulate missing GraphQL capability
    app.state.epics_supported = True
    app.state.latency = 0.0  # seconds added to every API response (makes "Refreshing" visible)

    def base(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    def project_url(request: Request, project_id: int) -> str:
        project = app.state.data.projects[project_id]
        return f"{base(request)}/{project['path_with_namespace']}"

    def decorate_user(request: Request, user: dict[str, Any]) -> dict[str, Any]:
        public = {k: v for k, v in user.items() if k != "is_admin" or app.state.admin}
        public["web_url"] = f"{base(request)}/{user['username']}"
        return public

    def decorate_work(request: Request, item: dict[str, Any], kind: str) -> dict[str, Any]:
        project = app.state.data.projects[item["project_id"]]
        segment = "issues" if kind == "issue" else "merge_requests"
        sigil = "#" if kind == "issue" else "!"
        return {
            **item,
            "web_url": f"{project_url(request, item['project_id'])}/-/{segment}/{item['iid']}",
            "references": {"full": f"{project['path_with_namespace']}{sigil}{item['iid']}"},
        }

    @app.middleware("http")
    async def gate(request: Request, call_next: Any) -> Response:
        if request.url.path.startswith("/-/fake"):
            return await call_next(request)  # type: ignore[no-any-return]
        app.state.requests += 1
        app.state.request_log.append((request.url.path, str(request.url.query)))
        if app.state.latency:
            await asyncio.sleep(app.state.latency)
        if app.state.down:
            return JSONResponse({"message": "503 Service Unavailable"}, status_code=503)
        if request.headers.get("PRIVATE-TOKEN") != token:
            return JSONResponse({"message": "401 Unauthorized"}, status_code=401)
        return await call_next(request)  # type: ignore[no-any-return]

    @app.get("/api/v4/version")
    async def version() -> dict[str, str]:
        return {"version": "17.4.0-fake", "revision": "fake"}

    @app.get("/api/v4/user")
    async def current_user(request: Request) -> dict[str, Any]:
        root = decorate_user(request, app.state.data.users[0])
        root["is_admin"] = app.state.admin
        return root

    @app.get("/api/v4/users")
    async def users(request: Request) -> JSONResponse:
        visible = [
            decorate_user(request, u)
            for u in app.state.data.users
            if app.state.admin or u["state"] == "active"
        ]
        return _paginate(request, visible)

    @app.get("/api/v4/projects/{project_id}")
    async def project(request: Request, project_id: int) -> JSONResponse:
        found = app.state.data.projects.get(project_id)
        if found is None:
            return JSONResponse({"message": "404 Project Not Found"}, status_code=404)
        return JSONResponse({**found, "web_url": project_url(request, project_id)})

    def filter_work(request: Request, items: list[dict[str, Any]], kind: str) -> JSONResponse:
        params = request.query_params
        updated_after = _parse_time(params.get("updated_after"))
        assignee = params.get("assignee_id")
        reviewer = params.get("reviewer_id")
        selected = []
        for item in items:
            if assignee and all(str(a["id"]) != assignee for a in item.get("assignees", [])):
                continue
            if reviewer and all(str(r["id"]) != reviewer for r in item.get("reviewers", [])):
                continue
            if updated_after and datetime.fromisoformat(item["updated_at"]) < updated_after:
                continue
            selected.append(decorate_work(request, item, kind))
        selected.sort(key=lambda i: i["updated_at"], reverse=True)
        return _paginate(request, selected)

    @app.get("/api/v4/issues")
    async def issues(request: Request) -> JSONResponse:
        return filter_work(request, app.state.data.issues, "issue")

    @app.get("/api/v4/merge_requests")
    async def merge_requests(request: Request) -> JSONResponse:
        return filter_work(request, app.state.data.merge_requests, "merge_request")

    @app.get("/api/v4/users/{user_id}/events")
    async def events(request: Request, user_id: int) -> JSONResponse:
        after_param = request.query_params.get("after")
        after = date.fromisoformat(after_param) if after_param else None
        chosen = [
            e
            for e in app.state.data.events
            if e["author_id"] == user_id
            and (after is None or datetime.fromisoformat(e["created_at"]).date() > after)
        ]
        chosen.sort(key=lambda e: (e["created_at"], e["id"]), reverse=True)
        return _paginate(request, chosen)

    def graphql_epics(request: Request, variables: dict[str, Any]) -> JSONResponse:
        if not app.state.epics_supported:
            return JSONResponse(
                {"errors": [{"message": "Field 'workItems' doesn't exist on type 'Group'"}]}
            )
        path = variables.get("fullPath")
        if path not in {
            project["path_with_namespace"].split("/")[0]
            for project in app.state.data.projects.values()
        }:
            return JSONResponse({"data": {"group": None}})
        nodes = [
            {
                "id": epic["id"],
                "iid": epic["iid"],
                "title": epic["title"],
                "state": epic["state"],
                "webUrl": f"{base(request)}/groups/{epic['group']}/-/epics/{epic['iid']}",
                "createdAt": epic["createdAt"],
                "updatedAt": epic["updatedAt"],
                "closedAt": epic["closedAt"],
                "reference": epic["reference"],
                "author": epic["author"],
                "widgets": [
                    {"type": "ASSIGNEES", "assignees": {"nodes": [{"username": epic["assignee"]}]}},
                    {"type": "LABELS", "labels": {"nodes": [{"title": t} for t in epic["labels"]]}},
                    {"type": "MILESTONE", "milestone": None},
                    {"type": "START_AND_DUE_DATE", "dueDate": epic["dueDate"]},
                ],
            }
            for epic in app.state.data.epics
            if epic["group"] == path and epic["assignee"] == variables.get("username")
        ]
        return JSONResponse(
            {"data": {"group": {"workItems": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}}}
        )

    @app.post("/api/graphql")
    async def graphql(request: Request) -> JSONResponse:
        body = await request.json()
        if app.state.graphql_error:
            return JSONResponse({"errors": [{"message": app.state.graphql_error}]})
        if "workItems" in body.get("query", ""):
            return graphql_epics(request, body.get("variables") or {})
        if "timelogs" not in body.get("query", ""):
            return JSONResponse({"errors": [{"message": "unsupported query"}]})
        variables = body.get("variables") or {}
        username = variables.get("username")
        start = _parse_time(variables.get("start"))
        end = _parse_time(variables.get("end"))
        offset = int(variables.get("after") or 0)
        user = next((u for u in app.state.data.users if u["username"] == username), None)
        logs = [
            t
            for t in app.state.data.timelogs
            if user is not None
            and t["user_id"] == user["id"]
            and (start is None or t["spent_at"] >= start)
            and (end is None or t["spent_at"] <= end)
        ]
        page = logs[offset : offset + 100]
        nodes = []
        for entry in page:
            issue = decorate_work(request, entry["issue"], "issue")
            project_data = app.state.data.projects[issue["project_id"]]
            nodes.append(
                {
                    "id": f"gid://gitlab/Timelog/{entry['id']}",
                    "spentAt": _iso(entry["spent_at"]),
                    "timeSpent": entry["seconds"],
                    "summary": entry["summary"],
                    "user": {"username": username},
                    "project": {
                        "id": f"gid://gitlab/Project/{issue['project_id']}",
                        "fullPath": project_data["path_with_namespace"],
                        "webUrl": project_url(request, issue["project_id"]),
                    },
                    "issue": {
                        "iid": issue["iid"],
                        "title": issue["title"],
                        "webUrl": issue["web_url"],
                    },
                    "mergeRequest": None,
                }
            )
        has_next = offset + 100 < len(logs)
        return JSONResponse(
            {
                "data": {
                    "timelogs": {
                        "nodes": nodes,
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": str(offset + 100) if has_next else None,
                        },
                    }
                }
            }
        )

    # ------------------------------------------------------------------ control

    @app.get("/-/fake/state")
    async def state() -> dict[str, Any]:
        return {
            "down": app.state.down,
            "admin": app.state.admin,
            "latency": app.state.latency,
            "requests": app.state.requests,
            "users": len(app.state.data.users),
            "events": len(app.state.data.events),
        }

    @app.post("/-/fake/outage")
    async def outage(down: bool = True) -> dict[str, bool]:
        app.state.down = down
        return {"down": down}

    @app.post("/-/fake/latency")
    async def latency(seconds: float = 0.0) -> dict[str, float]:
        app.state.latency = max(0.0, min(seconds, 10.0))
        return {"latency": app.state.latency}

    @app.post("/-/fake/activity")
    async def activity(user_id: int = 2) -> dict[str, Any]:
        event = add_event(app.state.data, user_id, datetime.now(UTC))
        return {"event_id": event["id"]}

    return app
