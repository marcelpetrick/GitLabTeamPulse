# Performance and profiling

Measured on 2026-10-15 (v0.4.1/v0.4.2) against the production instance: 328 accounts, 8 selected
people, 479 cached work items, 830 finished items and 4,573 daily history rows. The scripts
are described at the end; nothing here needs a live GitLab except the sync timings taken from
the server log.

## Results

### Startup

| Step | Time |
| --- | --- |
| App start (create app, migration check, lifespan, scheduler start) | 0.04 s |
| First page served | immediately, from the SQLite cache |
| Directory refresh (325 accounts, 4 pages) | 15.3 s, in the background |
| First sync after the 0.4 upgrade (history backfill, 48 datasets) | 84 s, in the background |
| Regular sync (8 people, 24 datasets) | 5.8–6.6 s every 10 minutes |

The UI never waits for GitLab: every dataset commits on its own, so cards fill in while a sync
is still running.

### API reads (real data, median of 15)

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

### Browser (Chromium, real data)

Rendering the dashboard (8 cards, 193 attention items) takes 37–46 ms and produces about 8,300
DOM nodes; fetching and parsing the payload takes about 40 ms.

### Sync parallelism (fake GitLab, 80 ms per request, 8 people)

| `TEAMPULSE_GITLAB_CONCURRENCY` | Backfill run (226 requests) | Regular run (93 requests) |
| --- | --- | --- |
| 4 (default) | 5.1 s | 2.1 s |
| 8 | 2.7 s | 1.0 s |

People and datasets already run in parallel. A run takes requests × latency ÷ concurrency
almost exactly, so the limit on parallel GitLab requests sets the speed, not the code.

## What changed, and what was decided

- **Done: gzip compression** for responses (`GZipMiddleware`). The dashboard payload is
  refetched on every data change and shrinks about 9× (714 KB to 80 KB), which matters most for
  browsers on a VPN.
- **Kept: concurrency 4 by default.** Doubling it halves sync time but doubles the burst load
  on GitLab. For a faster first backfill, set `TEAMPULSE_GITLAB_CONCURRENCY=8` temporarily.
- **Not changed: the directory refresh pages sequentially** (15 s, hourly, in the background).
  Fetching pages in parallel would save about 11 s an hour nobody waits for, and would compete
  with the dashboard sync for the same request slots.
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
  time it with `performance.now()`.
- **Real sync timings:** the server log's `selected refresh (…): … in N s` lines.
