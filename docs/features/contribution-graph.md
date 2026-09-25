# Feature: contribution graph (2D calendar and 3D skyline)

Status: **implemented on `feature/contribution-graph`, in review**. It will be squash-merged into
`master` after review, with a minor version bump (0.3.0).

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
