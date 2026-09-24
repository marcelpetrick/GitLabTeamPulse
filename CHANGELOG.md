# Changelog

Every commit bumps the patch version; major features bump the minor version.
The full per-commit history is `git log --oneline` (each subject ends with its version).

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

### 0.2.11 – 0.2.16: second review round and architecture docs
- GraphQL errors are classified: only schema, license and permission errors count as a missing
  capability. Transient errors fail the dataset and keep last-known-good data, and epics that
  worked before are never dropped silently.
- Only the cards a running sync covers show "refreshing".
- The first page load fetches only the routed view's data.
- Changelog version ranges corrected.
- Dependabot for Python, GitHub Actions and the Docker base image.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): C4 architecture overview with Mermaid diagrams.

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
