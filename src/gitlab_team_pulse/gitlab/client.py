"""Async GitLab REST/GraphQL client with pagination, bounded retries and bounded concurrency.

The token is sent only in the ``PRIVATE-TOKEN`` header and only to the configured base URL;
pagination links pointing anywhere else are refused.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any, Self

import httpx
from pydantic import SecretStr

from gitlab_team_pulse import __version__
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
from gitlab_team_pulse.gitlab.models import (
    ActivityEvent,
    GitLabProject,
    GitLabUser,
    Relation,
    Timelog,
    WorkItem,
    WorkKind,
    guarded,
    parse_event,
    parse_project,
    parse_timelog,
    parse_user,
    parse_work_item,
)
from gitlab_team_pulse.logging_setup import get_logger

log = get_logger("gitlab")

RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_RETRY_AFTER_SECONDS = 60.0
PER_PAGE = 100

TIMELOGS_QUERY = """
query($username: String!, $start: Time!, $end: Time!, $after: String) {
  timelogs(username: $username, startTime: $start, endTime: $end, first: 100, after: $after) {
    nodes {
      id spentAt timeSpent summary
      user { username }
      project { id fullPath webUrl }
      issue { iid title webUrl }
      mergeRequest { iid title webUrl }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class GitLabClient:
    """One instance per process; share it between sync runs."""

    def __init__(
        self,
        base_url: str,
        token: SecretStr,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        concurrency: int = 4,
        verify: bool | str = True,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_base: float = 0.5,
        max_pages: int = 1000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_prefix = f"{self.base_url}/api/v4/"
        self._max_retries = max_retries
        self._semaphore = asyncio.Semaphore(concurrency)
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._max_pages = max_pages
        self.request_count = 0
        self._http = httpx.AsyncClient(
            base_url=self._api_prefix,
            headers={
                "PRIVATE-TOKEN": token.get_secret_value(),
                "User-Agent": f"gitlab-team-pulse/{__version__}",
                "Accept": "application/json",
            },
            timeout=timeout,
            verify=verify,
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ transport

    def _backoff(self, attempt: int) -> float:
        base: float = self._backoff_base * (2**attempt)
        return base + random.uniform(0, self._backoff_base)  # noqa: S311 - jitter only

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return min(max(float(value), 0.0), MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            return None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one request with bounded retries; raises a typed ``GitLabError``."""
        label = f"{method} {url.removeprefix(self._api_prefix)}"
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                async with self._semaphore:
                    self.request_count += 1
                    response = await self._http.request(method, url, params=params, json=json)
            except httpx.TimeoutException as exc:
                failure: GitLabError = GitLabUnavailableError(f"{label}: timed out ({exc!r})")
                delay = self._backoff(attempt)
            except httpx.TransportError as exc:
                failure = GitLabUnavailableError(
                    f"{label}: connection failed ({type(exc).__name__})"
                )
                delay = self._backoff(attempt)
            else:
                status = response.status_code
                log.debug("%s -> %d in %.2fs", label, status, time.monotonic() - started)
                if status < 400:
                    return response
                if status == 401:
                    raise GitLabAuthError(
                        f"{label}: authentication failed (401) - check the GitLab token",
                        status=status,
                    )
                if status == 403:
                    raise GitLabForbiddenError(
                        f"{label}: forbidden (403) - the token lacks permission", status=status
                    )
                if status == 404:
                    raise GitLabNotFoundError(f"{label}: not found (404)", status=status)
                if status == 429:
                    failure = GitLabRateLimitError(f"{label}: rate limited (429)", status=status)
                    delay = self._retry_after(response) or self._backoff(attempt)
                elif status in RETRYABLE_STATUS:
                    failure = GitLabUnavailableError(
                        f"{label}: server error ({status})", status=status
                    )
                    delay = self._backoff(attempt)
                else:
                    raise GitLabError(f"{label}: unexpected HTTP {status}", status=status)
            if attempt >= self._max_retries:
                raise failure
            attempt += 1
            log.warning("%s; retry %d/%d in %.1fs", failure, attempt, self._max_retries, delay)
            await self._sleep(delay)

    @staticmethod
    def _json(response: httpx.Response, what: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise GitLabResponseError(f"{what}: response is not valid JSON") from exc

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._json(await self._request("GET", path, params=params), path)

    async def paginate(
        self, path: str, params: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Follow ``Link: rel=next`` (keyset or offset) or ``X-Next-Page`` headers."""
        query: dict[str, Any] | None = {"per_page": PER_PAGE, **(params or {})}
        url = path
        items: list[dict[str, Any]] = []
        for _ in range(self._max_pages):
            response = await self._request("GET", url, params=query)
            page = self._json(response, path)
            if not isinstance(page, list):
                raise GitLabResponseError(f"{path}: expected a list page")
            items.extend(page)
            if limit is not None and len(items) >= limit:
                return items[:limit]
            next_url = response.links.get("next", {}).get("url")
            if next_url:
                if not next_url.startswith(self._api_prefix):
                    raise GitLabResponseError(f"{path}: refusing pagination link to another host")
                url, query = next_url, None
                continue
            next_page = response.headers.get("X-Next-Page", "").strip()
            if next_page and query is not None:
                query = {**query, "page": next_page}
                continue
            return items
        log.warning("%s: stopped after %d pages", path, self._max_pages)
        return items

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = await self._request(
            "POST", f"{self.base_url}/api/graphql", json={"query": query, "variables": variables}
        )
        body = self._json(response, "graphql")
        if not isinstance(body, dict):
            raise GitLabResponseError("graphql: expected an object")
        errors = body.get("errors")
        if errors:
            first = errors[0].get("message") if isinstance(errors[0], dict) else errors[0]
            raise GitLabCapabilityError(f"graphql: {first}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise GitLabResponseError("graphql: response has no data")
        return data

    # ------------------------------------------------------------------ operations

    async def version(self) -> str:
        payload = await self.get_json("version")
        if not isinstance(payload, dict) or "version" not in payload:
            raise GitLabResponseError("version: unexpected payload")
        return str(payload["version"])

    async def current_user(self) -> GitLabUser:
        return guarded("user", parse_user, await self.get_json("user"))

    async def list_users(self) -> list[GitLabUser]:
        """Every account visible to the token; bots, blocked and inactive included."""
        pages = await self.paginate(
            "users", {"order_by": "id", "sort": "asc", "exclude_internal": "false"}
        )
        return [guarded("user", parse_user, item) for item in pages]

    async def get_project(self, project_id: int) -> GitLabProject:
        payload = await self.get_json(f"projects/{project_id}", {"license": "false"})
        return guarded("project", parse_project, payload)

    async def _work(
        self, path: str, kind: WorkKind, relation: Relation, params: dict[str, Any]
    ) -> list[WorkItem]:
        pages = await self.paginate(path, params)
        return [guarded(kind, lambda p: parse_work_item(p, kind, relation), item) for item in pages]

    async def get_user_work(self, user_id: int, updated_after: datetime) -> list[WorkItem]:
        """Assigned issues and MRs plus MRs to review, in every state, updated since the cutoff."""
        common = {
            "scope": "all",
            "state": "all",
            "updated_after": updated_after.isoformat(),
            "order_by": "updated_at",
            "sort": "desc",
        }
        issues, assigned_mrs, review_mrs = await asyncio.gather(
            self._work("issues", "issue", "assignee", {**common, "assignee_id": user_id}),
            self._work(
                "merge_requests", "merge_request", "assignee", {**common, "assignee_id": user_id}
            ),
            self._work(
                "merge_requests", "merge_request", "reviewer", {**common, "reviewer_id": user_id}
            ),
        )
        return [*issues, *assigned_mrs, *review_mrs]

    async def get_user_recent_activity(
        self, user_id: int, after: date | None = None, *, limit: int | None = None
    ) -> list[ActivityEvent]:
        """Events strictly after ``after`` (GitLab date granularity), newest first."""
        params: dict[str, Any] = {"sort": "desc"}
        if after is not None:
            params["after"] = after.isoformat()
        if limit is not None:
            params["per_page"] = min(limit, PER_PAGE)
        pages = await self.paginate(f"users/{user_id}/events", params, limit=limit)
        return [guarded("event", parse_event, item) for item in pages]

    async def get_user_timelogs(
        self, username: str, start: datetime, end: datetime
    ) -> list[Timelog]:
        """Actual time entries logged by ``username`` in ``[start, end]`` (GraphQL)."""
        result: list[Timelog] = []
        cursor: str | None = None
        for _ in range(self._max_pages):
            data = await self.graphql(
                TIMELOGS_QUERY,
                {
                    "username": username,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "after": cursor,
                },
            )
            connection = data.get("timelogs")
            if not isinstance(connection, dict):
                raise GitLabResponseError("timelogs: missing connection")
            for node in connection.get("nodes") or []:
                owner = (node.get("user") or {}).get("username") if isinstance(node, dict) else None
                if owner not in (None, username):
                    continue
                result.append(guarded("timelog", parse_timelog, node))
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
        return result
