# Changelog

Every commit bumps the patch version; major features bump the minor version.
The full per-commit history is `git log --oneline` (each subject ends with its version).

## 0.4.1 (2026-10-15): About dialog, clearer review signal, diagnostics fix

- **About dialog** (top bar): version, author and contact, license, source link, Python version
  and the runtime dependencies with their versions and licenses, read from the installed
  package (`GET /api/about`).
- **"Reviewer not monitored"** replaces the misleading "Reviewer outside the team": the signal
  only means none of the reviewers is monitored on this dashboard (usually colleagues);
  reviewers are shown with their full names.
- **Diagnostics**: open problems of people who are no longer selected or in a team are
  resolved by the hourly cleanup instead of staying forever.
- Dependencies: fastapi 0.143.2.
- Docs: README showcase of the 0.4 features with new screenshots (milestones, team editor,
  About), [`AGENTS.md`](AGENTS.md) for working in the repository, plan for 0.4.1.

## 0.4.0 (2026-10-15): steering signals, teams and team reports

- **Needs attention** on the dashboard: one filterable list across the monitored people of
  stalled items (no update for 7+ days), overdue and due-soon work, MRs waiting for review, MRs
  that can't be merged, and MRs without a reviewer in the team. Open items stay visible however
  old they are; cards show stalled/overdue chips and work items `spent / estimate`.
- **Milestones**: progress, remaining estimate, unestimated and overdue issues and a burn-up,
  computed from each milestone's issues (GitLab Free has no burndown API).
- **Teams**: named, shared groups with an optional project scope, a *Show on dashboard* switch,
  and a team report per week, month or quarter. It compares hours, merged MRs, closed issues,
  reviews, MR cycle time and estimate accuracy with the same span of the previous period, and
  shows the share of effort per project and knowledge spread. Last week's digest can be copied
  as Markdown. The per-person table is optional (`TEAMPULSE_TEAM_PERSON_BREAKDOWN`).
- History as daily aggregates and finished items (53 weeks), refreshed hourly for selected users
  and team members; migration 0004. The first sync after the upgrade refetches each person's
  year of events once.
- New settings: `TEAMPULSE_STALLED_DAYS`, `_DUE_SOON_DAYS`, `_MILESTONES_MAX`,
  `_MAX_TRACKED_USERS`, `_TEAMS_READONLY`, `_TEAM_PERSON_BREAKDOWN`.
- Branch review fixes: the milestone progress sync and the dashboard use one selection rule;
  an unreadable milestone no longer causes a refetch every run; history diagnostics map to
  their own dataset.
- Docs: [steering signals](docs/features/steering-signals.md), architecture, VISION §43.1,
  [ideas](ideas20261015.md), [plan](plan.md) and new screenshots.

## 0.3.2 (2026-10-15): dependency update

- Dependencies on the latest stable releases: fastapi 0.143.1, pydantic 2.14.1,
  pydantic-settings 2.16.0, SQLAlchemy 2.1.4, playwright 1.64.0, pre-commit 4.7.0, ruff 0.17.0,
  uv 0.13.1 (Docker image, GitHub Actions, GitLab CI), setup-uv v10.3.0, upload-artifact v7.0.2.
- README screenshots regenerated; the dashboard shots now include the contribution calendar.

## 0.3.1 (2026-10-06): dependency update

- Dependencies on the latest stable releases: fastapi 0.142.2, SQLAlchemy 2.1.3, uvicorn
  0.54.0, mypy 2.4.0, ruff 0.16.10, uv 0.12.23 (Docker image, GitHub Actions, GitLab CI).
  Supersedes Dependabot PRs #1 and #2.
- The contribution calendar E2E test waits for the backfill instead of counting cells once
  (failed on slower CI runners).

## 0.3.0 (2026-09-28): contribution calendar

- Contribution calendar per person for the last 12 months, computed from GitLab events with
  GitLab's contribution rule (the profile's `calendar.json` web route rejects API tokens).
- 2D GitLab-style calendar grid and a rotatable, zoomable 3D skyline (dependency-free canvas).
- New `contributions` dataset (backfill once, then incremental, refreshed at most hourly),
  Alembic migration 0003 and `TEAMPULSE_CONTRIBUTIONS_REFRESH_MINUTES`. The first sync
  backfills a year per person (about 70 s for 6 people on a real instance).
- Review fixes before release: the 3D view keeps its angle and zoom across refreshes; the
  calendar pill no longer flickers to "stale" every hour; a failed backfill retries on the next
  run; calendar errors show the right "last good data"; design uploads count; a truncated
  backfill fails instead of storing zeros; a changed time zone triggers a full backfill; the
  busiest day no longer shifts by a day west of UTC; 3D side walls hit-test correctly; keyboard
  inspection of the 2D grid is announced to screen readers.
- Demo: links open a page on the fake GitLab instead of a JSON 404. README: running with your
  own env file.

## 0.2.x

### 0.2.0 (2026-09-24): epics
- Assigned epics via GraphQL work items for the top-level groups the user's work lives in; when
  the instance lacks the capability, a warning diagnostic appears instead of silent omission.

### 0.2.1 – 0.2.8: review fixes
- Separate `users_version`, so People tabs refetch the directory only when it changes.
- Crashed background jobs are logged and persisted as diagnostics.
- The demo database follows `TEAMPULSE_DATABASE_PATH` (persists on the container's `/data`).
- Redirects from GitLab are reported as a base-URL configuration error.
- Copied settings never keep a stale timezone.
- Inaccessible projects are not re-requested on every run (negative cache).

### 0.2.9 – 0.2.10: documentation
- [`docs/GRAPHQL.md`](docs/GRAPHQL.md): why and how GraphQL is used (timelogs, epics).

### 0.2.11 – 0.2.22: second and third review rounds, architecture and setup docs
- GraphQL errors are classified: only schema, license and permission errors count as a missing
  capability. Transient errors fail the dataset and keep last-known-good data, and epics that
  worked before are never dropped silently.
- Only the cards a running sync covers show "refreshing".
- The first page load fetches only the routed view's data.
- Changelog version ranges corrected.
- Dependabot for Python, GitHub Actions and the Docker base image.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): C4 architecture overview with Mermaid diagrams.
- Health checks parse the migration head once; CLI `--host`/`--port` are validated; `SECURITY.md`.
- README TL;DR setup (token creation, safe token entry, troubleshooting).
- Epics that stop working mid-run keep their last known good state while issues and merge
  requests keep refreshing; after the hourly recheck window they are marked unavailable.

## 0.1.x

### 0.1.0 (2026-09-24): Dockerized prototype
- Non-root Docker image: Gunicorn with a single Uvicorn worker, an explicit migration step,
  Docker secret support, a `/data` volume and a healthcheck.
- GitHub Actions workflow that smoke-tests the container (fake GitLab, secret, restart with a
  persistent volume) and publishes multi-arch images to GHCR.
- `compose.yaml` for production-style deployments.

## 0.0.x: local product

- 0.0.21: `localPipeline.sh`, Makefile, pre-commit, GitLab CI and the GitHub Actions pipeline.
- 0.0.19: Playwright browser end-to-end tests (filter, selection, refresh, theme, auto-update, outage).
- 0.0.18: People and Dashboard UI (SVG charts, freshness states, diagnostics drawer, light/dark themes).
- 0.0.17: CLI (`serve`, `migrate`, `refresh`, `doctor`, `demo`, `fake-gitlab`).
- 0.0.16: JSON API (health, status, users, selection, dashboard, per-user data, errors, refresh).
- 0.0.14: In-process scheduler with coalesced manual refresh and immediate sync of new selections.
- 0.0.13: Selected-user sync (work, activity, timelogs), last-known-good semantics, rolling retention.
- 0.0.12: Work item, activity, timelog and project cache tables (Alembic 0002).
- 0.0.11: User-directory synchronization.
- 0.0.10: Deterministic fake GitLab for demos and tests.
- 0.0.8–0.0.9: Freshness classification; persistence of sync state, runs and deduplicated errors.
- 0.0.6–0.0.7: GitLab payload normalization; async client with pagination, retries and bounded concurrency.
- 0.0.4: SQLite models and the first Alembic migration.
- 0.0.2–0.0.3: Configuration (env, `.env`, Docker secrets) and redacting stdout logging.
- 0.0.1: Project scaffold.
