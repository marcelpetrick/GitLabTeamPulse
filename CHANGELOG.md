# Changelog

Every commit bumps the patch version; major features bump the minor version.
The full per-commit history is `git log --oneline` (each subject ends with its version).

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
