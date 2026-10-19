"""Async GitLab REST/GraphQL client with pagination, bounded retries and bounded concurrency.

The token is sent only in the ``PRIVATE-TOKEN`` header and only to the configured base URL;
pagination links pointing anywhere else are refused. GraphQL is used only for timelogs and
epics, where REST has no efficient equivalent; see ``docs/GRAPHQL.md``.
"""

from __future__ import annotations

import asyncio
import random
import re
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
    parse_epic,
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


EPICS_TEMPLATE = """
query(__GROUP_VARS__, __ASSIGNEE_VAR__, $updatedAfter: Time, $after: String__STATE_VAR__) {
__GROUPS__
}
"""
EPICS_GROUP = """  __ALIAS__: group(fullPath: $__PATH_VAR__) {
    workItems(types: [EPIC], __ASSIGNEE_ARG__, includeDescendants: true,
              updatedAfter: $updatedAfter, first: 50, after: $after__STATE_ARG__) {
      nodes {
        id iid title state webUrl createdAt updatedAt closedAt reference(full: true)
        author { username }
        widgets {
          type
          ... on WorkItemWidgetAssignees { assignees { nodes { username } } }
          ... on WorkItemWidgetLabels { labels { nodes { title } } }
          ... on WorkItemWidgetMilestone { milestone { title } }
          ... on WorkItemWidgetStartAndDueDate { dueDate }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }"""
# GitLab rates one group block at about 37 of its maximum query complexity of 300.
GROUPS_PER_EPIC_QUERY = 5
MAX_BATCH_USERNAMES = 100
# A top-level path that is a personal namespace, not a group, is not asked again for a day.
NOT_A_GROUP_SECONDS = 24 * 3600


def _epics_query(*, batch: bool, open_only: bool, groups: int = 1) -> str:
    """Epics of ``groups`` groups in one request (aliases ``g0``, ``g1``…, paths in ``$p0``…);
    for one person (``assigneeUsernames``: all of them) or several at once (``or``: any of
    them); every epic or only open ones (``state``)."""
    blocks = "\n".join(
        EPICS_GROUP.replace("__ALIAS__", f"g{i}").replace("__PATH_VAR__", f"p{i}")
        for i in range(groups)
    )
    query = (
        EPICS_TEMPLATE.replace("__GROUPS__", blocks)
        .replace("__GROUP_VARS__", ", ".join(f"$p{i}: ID!" for i in range(groups)))
        .replace("__ASSIGNEE_VAR__", "$usernames: [String!]!" if batch else "$username: String!")
        .replace(
            "__ASSIGNEE_ARG__",
            "or: {assigneeUsernames: $usernames}" if batch else "assigneeUsernames: [$username]",
        )
    )
    if open_only:
        return query.replace("__STATE_VAR__", ", $state: IssuableState").replace(
            "__STATE_ARG__", ", state: $state"
        )
    return query.replace("__STATE_VAR__", "").replace("__STATE_ARG__", "")


EPICS_QUERY = _epics_query(batch=False, open_only=False)
# Only open epics, however old: stalled epics stay visible without paging through all history.
OPEN_EPICS_QUERY = _epics_query(batch=False, open_only=True)


CAPABILITY_PATTERN = re.compile(
    r"doesn't exist|does not exist|unknown argument|has an invalid value|unknown type|"
    r"is not defined|not supported|not available|license|permission|not authorized|"
    r"do not have access|requires? (?:an )?admin",
    re.IGNORECASE,
)


def is_capability_message(message: str) -> bool:
    """True when a GraphQL error means "this installation/token cannot do that" (schema,
    license or permission), as opposed to a transient server-side failure."""
    return bool(CAPABILITY_PATTERN.search(message))


WORK_LISTS: tuple[tuple[str, WorkKind, Relation], ...] = (
    ("issues", "issue", "assignee"),
    ("merge_requests", "merge_request", "assignee"),
    ("merge_requests", "merge_request", "reviewer"),
)


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
        self._not_groups: dict[str, float] = {}  # path -> monotonic time to ask again
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
                if status < 300:
                    return response
                if status < 400:
                    location = response.headers.get("Location", "?")
                    raise GitLabError(
                        f"{label}: redirected ({status}) to {location} - check "
                        "TEAMPULSE_GITLAB_URL (scheme, host and path must match exactly)",
                        status=status,
                    )
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
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        complete: bool = False,
        parallel: bool = False,
    ) -> list[dict[str, Any]]:
        """Follow ``Link: rel=next`` (keyset or offset) or ``X-Next-Page`` headers.

        With ``complete``, hitting the page cap raises instead of returning a partial list.
        With ``parallel`` and offset pagination that reports ``X-Total-Pages``, the remaining
        pages are fetched at the same time (within the concurrency limit).
        """
        query: dict[str, Any] | None = {"per_page": PER_PAGE, **(params or {})}
        url = path
        items: list[dict[str, Any]] = []
        for number in range(self._max_pages):
            response = await self._request("GET", url, params=query)
            page = self._json(response, path)
            if not isinstance(page, list):
                raise GitLabResponseError(f"{path}: expected a list page")
            items.extend(page)
            total = response.headers.get("X-Total-Pages", "").strip()
            if parallel and number == 0 and limit is None and total.isdigit() and int(total) > 1:
                return items + await self._remaining_pages(path, query or {}, int(total), complete)
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
        if complete:
            raise GitLabResponseError(f"{path}: more than {self._max_pages} pages")
        log.warning("%s: stopped after %d pages", path, self._max_pages)
        return items

    async def _remaining_pages(
        self, path: str, query: dict[str, Any], total: int, complete: bool
    ) -> list[dict[str, Any]]:
        last = min(total, self._max_pages)
        if total > last:
            if complete:
                raise GitLabResponseError(f"{path}: more than {self._max_pages} pages")
            log.warning("%s: stopped after %d pages", path, self._max_pages)

        async def fetch(number: int) -> list[dict[str, Any]]:
            page = self._json(
                await self._request("GET", path, params={**query, "page": number}), path
            )
            if not isinstance(page, list):
                raise GitLabResponseError(f"{path}: expected a list page")
            return page

        pages = await asyncio.gather(*(fetch(n) for n in range(2, last + 1)))
        return [item for page in pages for item in page]

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
            if is_capability_message(str(first)):
                raise GitLabCapabilityError(f"graphql: {first}")
            # Anything else (e.g. a server-side timeout reported inside HTTP 200) is transient:
            # the dataset fails and keeps its last known good data.
            raise GitLabUnavailableError(f"graphql: {first}")
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
            "users",
            {"order_by": "id", "sort": "asc", "exclude_internal": "false"},
            parallel=True,
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
        """Assigned issues and MRs plus MRs to review: every state updated since the cutoff,
        and every open item regardless of age (stalled work must not drop out of view)."""
        recent = {
            "scope": "all",
            "state": "all",
            "updated_after": updated_after.isoformat(),
            "order_by": "updated_at",
            "sort": "desc",
        }
        still_open = {"scope": "all", "state": "opened", "order_by": "updated_at", "sort": "desc"}
        lists = await asyncio.gather(
            *(
                self._work(path, kind, relation, {**common, f"{relation}_id": user_id})
                for common in (recent, still_open)
                for path, kind, relation in WORK_LISTS
            )
        )
        unique: dict[tuple[str, int, str], WorkItem] = {}
        for item in (item for found in lists for item in found):
            unique.setdefault((item.kind, item.gitlab_id, item.relation), item)
        return list(unique.values())

    async def get_user_outcomes(self, user_id: int, updated_after: datetime) -> list[WorkItem]:
        """Merged MRs authored by the user and issues assigned to them, updated since the cutoff.

        Issues come in every state so that a reopened issue can be removed from the outcomes.
        """
        common = {
            "scope": "all",
            "updated_after": updated_after.isoformat(),
            "order_by": "updated_at",
            "sort": "desc",
        }
        merged, issues = await asyncio.gather(
            self._work(
                "merge_requests",
                "merge_request",
                "assignee",
                {**common, "state": "merged", "author_id": user_id},
            ),
            self._work(
                "issues", "issue", "assignee", {**common, "state": "all", "assignee_id": user_id}
            ),
        )
        return [*merged, *issues]

    async def get_milestone_issues(
        self, milestone_id: int, *, project_id: int | None, group_id: int | None
    ) -> list[WorkItem]:
        """Every issue of a project or group milestone (Free: the basis of our own burndown)."""
        if project_id is not None:
            path = f"projects/{project_id}/milestones/{milestone_id}/issues"
        elif group_id is not None:
            path = f"groups/{group_id}/milestones/{milestone_id}/issues"
        else:
            raise GitLabResponseError(f"milestone {milestone_id}: neither project nor group")
        return await self._work(path, "issue", "assignee", {})

    async def get_user_recent_activity(
        self,
        user_id: int,
        after: date | None = None,
        *,
        limit: int | None = None,
        complete: bool = False,
    ) -> list[ActivityEvent]:
        """Events strictly after ``after`` (GitLab date granularity), newest first."""
        params: dict[str, Any] = {"sort": "desc"}
        if after is not None:
            params["after"] = after.isoformat()
        if limit is not None:
            params["per_page"] = min(limit, PER_PAGE)
        pages = await self.paginate(
            f"users/{user_id}/events", params, limit=limit, complete=complete
        )
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

    async def get_user_epics(
        self,
        username: str,
        group_paths: set[str],
        updated_after: datetime | None,
        *,
        open_only: bool = False,
    ) -> list[WorkItem]:
        """Epics assigned to ``username`` in the given top-level groups (and their subgroups).

        Raises ``GitLabCapabilityError`` when the installation has no work-item epics
        (older version or missing license); unknown or personal namespaces are skipped.
        """
        return await self.get_epics([username], group_paths, updated_after, open_only=open_only)

    async def get_epics(
        self,
        usernames: list[str],
        group_paths: set[str],
        updated_after: datetime | None,
        *,
        open_only: bool = False,
    ) -> list[WorkItem]:
        """Epics assigned to any of ``usernames`` in the given top-level groups.

        Up to ``GROUPS_PER_EPIC_QUERY`` groups share one request and the requests run in
        parallel; several people share them through the ``or`` filter. A group with more
        than one page continues on its own. Paths that turn out not to be groups (personal
        namespaces) are skipped for ``NOT_A_GROUP_SECONDS``.
        """
        if not 0 < len(usernames) <= MAX_BATCH_USERNAMES:
            raise ValueError(f"between 1 and {MAX_BATCH_USERNAMES} usernames, not {len(usernames)}")
        batch = len(usernames) > 1
        now = time.monotonic()
        paths = sorted(p for p in group_paths if self._not_groups.get(p, 0.0) <= now)
        common: dict[str, Any] = {"usernames": usernames} if batch else {"username": usernames[0]}
        common["updatedAfter"] = updated_after.isoformat() if updated_after else None
        if open_only:
            common["state"] = "opened"

        def connection(group: Any, path: str) -> dict[str, Any] | None:
            if not isinstance(group, dict):
                self._not_groups[path] = time.monotonic() + NOT_A_GROUP_SECONDS
                return None
            found = group.get("workItems")
            if not isinstance(found, dict):
                raise GitLabResponseError("workItems: missing connection")
            return found

        def nodes(found: dict[str, Any]) -> list[WorkItem]:
            return [guarded("epic", parse_epic, node) for node in found.get("nodes") or []]

        async def rest_of_group(path: str, cursor: str | None) -> list[WorkItem]:
            query = _epics_query(batch=batch, open_only=open_only)
            result: list[WorkItem] = []
            for _ in range(self._max_pages - 1):
                data = await self.graphql(query, {"p0": path, **common, "after": cursor})
                found = connection(data.get("g0"), path)
                if found is None:
                    break
                result.extend(nodes(found))
                page_info = found.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
            return result

        async def chunk(group: list[str]) -> list[WorkItem]:
            query = _epics_query(batch=batch, open_only=open_only, groups=len(group))
            variables = {**{f"p{i}": path for i, path in enumerate(group)}, **common}
            data = await self.graphql(query, {**variables, "after": None})
            result: list[WorkItem] = []
            more = []
            for i, path in enumerate(group):
                found = connection(data.get(f"g{i}"), path)
                if found is None:
                    continue
                result.extend(nodes(found))
                page_info = found.get("pageInfo") or {}
                if page_info.get("hasNextPage"):
                    more.append(rest_of_group(path, page_info.get("endCursor")))
            for rest in await asyncio.gather(*more):
                result.extend(rest)
            return result

        size = GROUPS_PER_EPIC_QUERY
        found = await asyncio.gather(
            *(chunk(paths[i : i + size]) for i in range(0, len(paths), size))
        )
        return list({epic.gitlab_id: epic for group in found for epic in group}.values())
