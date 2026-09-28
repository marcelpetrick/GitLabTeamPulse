# Feature: contribution graph (2D calendar and 3D skyline)

Status: **released in 0.3.0** (rebased onto `master` and fast-forwarded, tag `v0.3.0`).

## Review follow-ups (2026-09-28)

Found in the branch review, an independent deep review and a first run against production
data. All were fixed before the release.

| # | Area | Problem | Fix | Owner files |
| --- | --- | --- | --- | --- |
| 1 | UI | Every `data_version` bump re-renders the dashboard and recreates the 3D skyline, so rotation and zoom reset (and a drag in progress breaks) every refresh | Keep the view (`azimuth`, `elevation`, `zoom`) in the per-card UI state and pass it into `Skyline`; E2E proves it survives a refresh | `static/js/contributions.js`, `static/js/dashboard.js`, `tests/e2e/test_browser.py` |
| 2 | Freshness | The calendar pill turns "Stale" for up to ~8 min every hour: it's due after 60 min but only starts on the next selected run (+10 min), while stale = 60 min + 2 min grace | Freshness interval = refresh minutes + the selected refresh interval | `dashboard.py`, `tests/test_contributions.py` |
| 3 | Sync | A failed backfill leaves the calendar empty for an hour: the throttle keys on the last *attempt* | Due again on the next run when the last attempt failed | `sync.py`, `tests/test_contributions.py` |
| 4 | Diagnostics | Contribution errors map to the global `selected` state, so "last good data" is wrong | Map `contributions` errors to their own dataset state | `dashboard.py`, `tests/test_contributions.py` |
| 5 | Performance | Calendar payload scans all rows once per user on every dashboard poll | Group rows by user once | `dashboard.py` |
| 6 | Demo | Links in the demo open a bare JSON 404 on the fake GitLab | The fake GitLab answers its own web URLs (profiles, projects, issues, MRs) with a small HTML page naming the target, so demo links land on the server in use | `fake_gitlab.py`, `tests/test_fake_gitlab.py` |
| 7 | Tooling | `make lint` fails when `.claude/` is unreadable | Exclude `.claude` in the Ruff config | `pyproject.toml` |
| 8 | Docs | No hint how to run with your own env file; the first-sync backfill duration is undocumented | README: `.env` / `uv run --env-file`, a note that the first sync backfills a year (~70 s for 6 users on production) and that the demo occupies port 8000 | `README.md` |
| 9 | Release | Version still 0.2.22, changelog "Unreleased" | Bump to 0.3.0, finalize the changelog, rebase onto `master`, fast-forward, tag `v0.3.0` | `__init__.py`, `CHANGELOG.md` |
| 10 | UI | "Busiest day" shows the previous day west of UTC (bare date parsed as UTC, printed local) | Format the date zone-free, like the grid | `static/js/dashboard.js` |
| 11 | Rule | Design uploads never counted: the API reports a created design as "uploaded" | Accept "uploaded" | `gitlab/models.py` |
| 12 | UI | Hovering a 3D building's side wall shows the day behind it | Hit-test visible side faces too | `static/js/contributions.js` |
| 13 | A11y | Arrow-key inspection of the 2D grid is silent for screen readers | Announce the active day in an `aria-live` region | `static/js/contributions.js` |
| 14 | Sync | Hitting the page cap stored an incomplete backfill as zeros and reported success | `paginate(complete=True)` raises; the dataset fails and keeps last-known-good | `gitlab/client.py`, `sync.py` |
| 15 | Sync | A changed `TEAMPULSE_TIMEZONE` left older days bucketed in the old zone | The cursor records its zone (`date@zone`); a different zone forces a full backfill | `sync.py` |

## Goal

Each person's card shows GitLab's familiar contribution graph for the last 12 months: a 2D
calendar grid (weeks × weekdays, darker = more contributions), the same view as on a GitLab
profile. A **3D "skyline"** view renders the same grid as buildings whose height is the day's
count, and it can be **rotated and zoomed** with the mouse, touch or keyboard.

## Key finding: why the counts are computed, not copied

GitLab draws the profile graph from the web route `/users/<username>/calendar.json`. On the
target instance that route redirects to the sign-in page even with a valid token, because web
routes do not accept API tokens. Team Pulse therefore computes the calendar itself from the
**Events API**, applying GitLab's own contribution rule (`Event.contributions`):

- every **push** event counts once (not once per commit), and so does every **comment**;
- **issues, work items, merge requests and designs** count when they are created (opened),
  closed, merged (accepted) or approved.

Counts can differ slightly from the profile page: the token's visibility decides which events
are returned, and the profile applies the viewer's permissions and "private contributions"
setting.

## Load and storage

| Concern | Approach |
| --- | --- |
| First sync | Backfill: page through one year of the user's events once (bounded page count) |
| Later syncs | Incremental: refetch only from the last counted day; runs at most every `TEAMPULSE_CONTRIBUTIONS_REFRESH_MINUTES` (default 60), not every 10 minutes |
| Storage | New table `contribution_days(user_id, day, count)` (Alembic 0003); about 365 small rows per selected user |
| Retention | Days older than the visible 53-week window are purged. Deselected users lose them with the rest of their cache |
| Resilience | Its own dataset, `contributions:<user>`, with last-known-good semantics: a failed refresh keeps the previous graph |

## UI

- **Where:** a new "Contributions · last 12 months" section per card, with a `2D | 3D` toggle.
- **2D:** SVG with 53 week columns × 7 rows (weeks start on Sunday, as on GitLab), month and
  weekday labels, GitLab's intensity levels (0, 1–9, 10–19, 20–29, 30+), a hover and keyboard
  tooltip ("7 contributions on Tue 23 Sep 2026"), and the total and busiest day.
- **3D:** a dependency-free canvas renderer (no CDN, no WebGL library). The grid is drawn as
  shaded boxes with painter's-algorithm depth sorting.
  - **Rotate:** drag, or the arrow keys.
  - **Zoom:** mouse wheel, pinch, or `+`/`-`.
  - **Reset:** a "Reset view" button.
  - It follows the theme colours and doesn't auto-animate (it respects reduced motion).

## Tests

- **Unit:** the contribution rule, day bucketing across time zones, backfill vs incremental
  windows, due scheduling, retention, the payload shape and the fake calendar data.
- **Browser E2E:** the 2D grid shows 53×7 cells with a tooltip; the 3D view renders; dragging
  and zooming change the rendered image.
