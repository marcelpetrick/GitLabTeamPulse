# Performance and profiling

## 2026-10-19 (v0.5.0): profiling the whole setup again

Production instance (GitLab 19.5.0, 328 accounts, 8 selected people, 4 teams, 491 cached work
items, 1,042 cached events), measured on a copy of the database and, for the sync, against the
real GitLab with every request timed (queue wait and network) and the event loop profiled.

**Finding: the sync is bound by GitLab requests, not by our code.** The CPU profile is >90%
waiting on the network; the wall time is almost exactly requests × latency ÷ the 4 request
slots. So the lever is fewer requests and no sequential chains.

| Measurement | Before | After | What changed |
| --- | --- | --- | --- |
| Regular sync (every 10 min): requests | 197 | 85 | epic queries shared by everyone, five groups per request, personal namespaces skipped |
| Regular sync: wall time | 14.5 s | 8.5–10.7 s | same |
| Hourly sync (history, milestones): requests / wall | 250 / 17.0 s | 138 / 11.5 s | same |
| Epic requests per run | 132 | 20 (fewer once non-groups are known) | see [`GRAPHQL.md`](GRAPHQL.md#2-epics-work-items) |
| Directory refresh (4 pages) | 3.7 s | 2.9 s | current user and pages 2–4 in parallel (`X-Total-Pages`) |
| Read model of `/api/dashboard` | 27–30 ms | 20–23 ms | plain rows instead of ~1,700 ORM objects, one grouping pass |
| Browser: dashboard render | 38.6 ms | 20 ms | charts drawn once, no forced layout per chart, cached date formatters |
| Browser: blocking work on load (long tasks) | 66 + 93 ms | 73 ms | same |
| Browser: cards in the DOM after navigation | 164 ms | 118 ms | first view and teams fetched together with the status |

Unchanged and fine: app start 66 ms (plus 0.65 s of Python imports once per process),
`/api/status` 2–3 ms, `/api/users` 7–8 ms, team report 8–11 ms, digest 9–13 ms, retention
cleanup 43 ms, the new `/api/meeting-summary` 33–43 ms (on demand only), payloads unchanged
(`/api/dashboard` 719 KiB raw, 80 KiB gzip).

Not changed, on purpose: the default of 4 parallel GitLab requests (doubling it halves the
sync but doubles the burst load on GitLab, see below), and the six issue and MR lists per
person (open work of any age plus everything updated in the work window); merging them would
need a different last-known-good model for work items.

## 2026-10-15 (v0.4.1/v0.4.2)

Measured on 2026-10-15 (v0.4.1/v0.4.2) against the production instance: 328 accounts, 8 selected
people, 479 cached work items, 830 finished items and 4,573 daily history rows. The scripts
are described at the end; nothing here needs a live GitLab except the sync timings taken from
the server log.

### Results

#### Startup

| Step | Time |
| --- | --- |
| App start (create app, migration check, lifespan, scheduler start) | 0.04 s |
| First page served | immediately, from the SQLite cache |
| Directory refresh (325 accounts, 4 pages) | 15.3 s, in the background |
| First sync after the 0.4 upgrade (history backfill, 48 datasets) | 84 s, in the background |
| Regular sync (8 people, 24 datasets) | 5.8–6.6 s every 10 minutes |

The UI never waits for GitLab: every dataset commits on its own, so cards fill in while a sync
is still running.

#### API reads (real data, median of 15)

| Endpoint | Median | Payload |
| --- | --- | --- |
| `/api/status` (polled every 20 s, 2 s while refreshing) | 2 ms | 1 KiB |
| `/api/dashboard` | 33 ms | 697 KiB raw, **80 KiB gzip** |
| `/api/users` | 5 ms | 111 KiB |
| `/api/teams/{id}/report` (month or quarter) | 8 ms | 3 KiB |
| `/api/teams/{id}/digest` | 10 ms | 4 KiB |

Profile of `/api/dashboard`: 45% building the work lists (per-item payloads and project
lookups), 30% the activity lists, the rest is calendar, attention and milestones. Every read is
a fixed number of batched queries (no N+1).

#### Browser (Chromium, real data)

Rendering the dashboard (8 cards, 193 attention items) takes 37–46 ms and produces about 8,300
DOM nodes; fetching and parsing the payload takes about 40 ms.

#### Sync parallelism (fake GitLab, 80 ms per request, 8 people)

| `TEAMPULSE_GITLAB_CONCURRENCY` | Backfill run (226 requests) | Regular run (93 requests) |
| --- | --- | --- |
| 4 (default) | 5.1 s | 2.1 s |
| 8 | 2.7 s | 1.0 s |

People and datasets already run in parallel. A run takes requests × latency ÷ concurrency
almost exactly, so the limit on parallel GitLab requests sets the speed, not the code.

### What changed, and what was decided

- **Done: gzip compression** for responses (`GZipMiddleware`). The dashboard payload is
  refetched on every data change and shrinks about 9× (714 KB to 80 KB), which matters most for
  browsers on a VPN.
- **Kept: concurrency 4 by default.** Doubling it halves sync time but doubles the burst load
  on GitLab. For a faster first backfill, set `TEAMPULSE_GITLAB_CONCURRENCY=8` temporarily.
- **Not changed then: the directory refresh pages sequentially** (15 s, hourly, in the
  background). *Changed in v0.5.0:* the pages are fetched in parallel, because on a fresh
  install the People view waits for the directory.
- **Not changed: the overlap between the 7-day timelogs and the 31-day timelog re-read**, and
  between the activity and calendar event fetches. These are one or two small extra requests an
  hour; merging the categories would couple their last-known-good handling.

## How to repeat the measurements

The scripts live outside the package (they are throwaway measurement code):

- **API reads:** copy the database with SQLite's backup API (the running server keeps a WAL),
  create the app with the scheduler disabled, then time each endpoint with `TestClient`
  and `cProfile`.
- **Sync parallelism:** run `SyncService` against `create_fake_gitlab()` with
  `app.state.latency` set, for a backfill run and a regular run, at concurrency 4 and 8.
- **Browser:** with Playwright, call `window.teamPulse.dashboardView.render()` in a loop and
  time it with `performance.now()`; record long tasks with a `PerformanceObserver` and self
  time per function with the Chrome DevTools Protocol profiler; timestamp the first card with a
  `MutationObserver` in an init script.
- **Sync against the real GitLab (v0.5.0):** run `SyncService` on a database copy, wrap
  `GitLabClient._request` and `httpx.AsyncClient.request` to record queue wait and network
  time per request, and run the event loop under `cProfile`.
- **Real sync timings:** the server log's `selected refresh (…): … in N s` lines.
