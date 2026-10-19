# GraphQL in GitLab Team Pulse

See also: [`ARCHITECTURE.md`](ARCHITECTURE.md) (where the GitLab client sits in the C4 model)
and [`README.md`](../README.md).

## What GraphQL is

GraphQL is a query language for APIs. GitLab's REST API has many fixed endpoints
(`/users`, `/issues`, `/merge_requests`, …), and each returns a predefined shape. GraphQL
has **one endpoint** (`POST /api/graphql`): the client sends a query that states exactly
which objects, filters and fields it wants, and the server returns JSON in exactly that
shape.

The HTTP method is `POST`, but a GraphQL **query** only reads data. Writing would require
a **mutation**, and Team Pulse never sends one. GraphQL therefore works with the same
read-only **`read_api`** token as REST.

## Why Team Pulse uses it

Most data comes over REST: users, issues, merge requests, events and projects. REST is simple
and well cached for those, and it covers them cleanly. GraphQL is used only where it
**materially reduces requests or is the only clean option**, as VISION §27.1 requires.

### 1. Timelogs (actual logged time)

REST has no endpoint that returns *all time entries logged by user X between two dates across
every project*. With REST you would have to:

1. enumerate every issue and merge request the user might have logged time on,
2. then fetch the time-tracking data for each item separately.

That is dozens to hundreds of requests per user, and it can still miss items the user no
longer touches. It would also break the "minimal upstream load" principle (VISION §5.6).

A single GraphQL query answers the question directly:

```graphql
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
```

**Efficiency:** one request per selected user per refresh (more only when a user has
over 100 entries in seven days). The response contains only the fields the dashboard
renders: duration, date, summary, project, and the linked issue or MR.

### 2. Epics (work items)

Newer GitLab models epics as *work items*. REST cannot filter epics by assignee (legacy
epics had no assignees at all). GraphQL can:

```graphql
g0: group(fullPath: $p0) {
  workItems(types: [EPIC], or: {assigneeUsernames: $usernames}, includeDescendants: true,
            updatedAfter: $updatedAfter, first: 50, after: $after) { ... }
}
g1: group(fullPath: $p1) { ... }   # up to five groups per request
```

Team Pulse does **not** scan every group. It derives the top-level groups from the people's
already-fetched issues and merge requests, and it queries only those (subgroups are
included through `includeDescendants`).

**One request for everyone, five groups at a time.** All people in a sync run share the epic
queries: `or: {assigneeUsernames: [...]}` matches epics assigned to *any* of them (up to 100),
and each epic is handed to the people it is assigned to. Up to five groups share one request
through aliases (`g0`…`g4`): GitLab rates one group block at about 37 of its maximum query
complexity of 300 (ten groups were rejected with "complexity of 370"). The requests for
different groups run in parallel; a group with more than one page continues on its own. A
top-level path that is not a group (a personal namespace) answers `null` and is not asked again
for a day. A GitLab that rejects the `or` filter before it ever worked gets one query per
person for the rest of the process (`assigneeUsernames: [$username]`).

Two kinds of query run: the epics updated inside the work window (`updatedAfter`), and the
**open** ones however old (`state: opened`), so stalled epics stay visible like stalled issues
and MRs. If an instance rejects the `state` argument before it ever worked, Team Pulse falls
back to one unfiltered query and filters locally for the rest of the process; if it fails
after it worked, the last known good epics are kept.

**Efficiency:** at most two requests per five top-level groups the monitored people work in,
for all of them together, filtered on the server by assignee and by update time or state,
never all epic history. On the production instance (8 people, 31 top-level paths) this cut the
epic requests per sync from 132 to 20, and to fewer once the personal namespaces are known.

## How it is implemented

- **One abstraction:** `GitLabClient` in `src/gitlab_team_pulse/gitlab/client.py` hides
  whether data comes from REST or GraphQL. The rest of the app only sees normalized
  `Timelog` and `WorkItem` objects (`gitlab/models.py`).
- **Pagination:** cursor-based paging through `pageInfo { hasNextPage endCursor }`, bounded
  by a page limit.
- **Same safeguards as REST:** the token is sent only to the configured host, requests
  share the bounded-concurrency limit, and transient failures use retries with backoff.
- **Capability handling:** if GitLab rejects a query (the field does not exist on an older
  version, a license is missing, or the rights are insufficient), the client raises
  `GitLabCapabilityError` instead of crashing:
  - **Timelogs:** only that user's timelog dataset is marked failed. Work and activity still
    refresh, and the last good timelogs stay visible (per-category last-known-good).
  - **Epics:** a single *warning* appears in the diagnostics drawer instead of epics
    silently missing (VISION §44.6, "no silent incompleteness"). The capability is re-checked
    hourly, and issues and merge requests are unaffected.
- **Checking it:** `gitlab-team-pulse doctor` runs a timelogs query and reports whether it
  works with your token and GitLab version.

## Permissions

A `read_api` token is sufficient. The top-level `timelogs` query may need an
**administrator** (or Auditor) account on some GitLab versions. Without that, timelogs show
up as a diagnostic, while everything else keeps working.
