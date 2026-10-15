# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

GitLab Team Pulse is a people-centric dashboard for one **self-managed GitLab (Free tier)**.
It shows what a chosen set of people are working on, flags stalled, overdue and waiting work,
tracks milestones, and reports on named teams. Every view says how fresh its data is.
Stack: Python 3.14 (`.python-version`; package supports >=3.12), FastAPI, SQLAlchemy and
Alembic on SQLite, httpx, and a vanilla ES-module frontend with no build step.

## Layout

`src/gitlab_team_pulse/`:

| Module | Responsibility |
| --- | --- |
| `gitlab/` (`client.py`, `models.py`, `errors.py`) | The only code that talks HTTP to GitLab (REST + GraphQL) or reads raw JSON; typed records and typed errors |
| `sync.py` | Synchronization per user and dataset, with last-known-good semantics and diagnostics |
| `scheduler.py` | In-process asyncio scheduler (single owner), refresh coalescing and throttling |
| `store.py` | Persistence helpers; they only stage changes on a caller-owned session |
| `models.py`, `db.py`, `migrations/versions/000N_*.py` | ORM models, engine and sessions, Alembic history (bundled in the package) |
| `retention.py` | Rolling cleanup that never removes the only good snapshot |
| `dashboard.py` | Read model: batched API payloads with freshness metadata, attention list, milestones |
| `teams.py` | Team payloads, team reports from the history aggregates, weekly Markdown digest |
| `about.py` | About dialog facts, read from the installed package metadata |
| `app.py` | FastAPI factory, security headers (CSP), static UI and JSON API |
| `freshness.py` | fresh / refreshing / stale / error / never classification |
| `config.py` | `TEAMPULSE_*` settings from env, `.env` and the secret file |
| `cli.py` | `serve`, `migrate`, `refresh`, `doctor`, `demo`, `fake-gitlab` |
| `fake_gitlab.py` | Deterministic fake GitLab for the demo, integration and browser tests |
| `logging_setup.py`, `gunicorn_conf.py` | Redacting stdout logging; Gunicorn with exactly one worker |

`static/`: `index.html`, `app.css`, `app.js` (routing, polling, theme) and `js/`: `api.js`
(local backend only), `dom.js` (DOM helpers), `dashboard.js`, `people.js`, `teams.js`,
`attention.js`, `charts.js` (SVG), `contributions.js` (2D SVG and 3D canvas), `diagnostics.js`,
`statusbar.js`, `about.js`, `format.js`, `ui.js`.

Other places: `tests/` (unit and integration; `tests/e2e/` for Playwright), `tools/`
(`bump_version.py`, `screenshots.py`), `docs/`, `VISION.md`, `plan.md`, `CHANGELOG.md`.

## Setup and commands

Use `uv` for everything (`uv sync`, `uv run …`); do not use pip or a hand-made venv.

```bash
make install     # uv sync --locked
make test        # pytest with coverage, gate 95%
make e2e         # Playwright/Chromium browser tests (pytest -m e2e)
make lint        # ruff check + ruff format --check
make typecheck   # strict mypy over src/
make format      # ruff check --fix + ruff format
make demo        # dashboard against the fake GitLab, http://127.0.0.1:8000
make run         # dashboard against the real GitLab (reads .env)
./localPipeline.sh   # the full gate; CI runs exactly this script
```

`localPipeline.sh` runs uv sync, Ruff lint and format check, mypy, a fresh-database migration,
tests with coverage, E2E, package build, a wheel smoke test in a clean venv, a Docker smoke test
(when Docker is available), then opens the coverage report and launches the demo. Useful flags:
`--noRun` (don't launch the demo), `--noOpen` (don't open the report), `--noE2E`, `--noDocker`,
`--report-dir PATH` (keep logs, `junit.xml`, `coverage.xml`, `summary.txt`). For an unattended
run use `./localPipeline.sh --noRun --noOpen`.

## Quality gate

All of these must stay green; never weaken them to get a change through.

- **Ruff** lint and format (line length 100, rule set in `pyproject.toml`).
- **mypy strict** over `src/` with the pydantic plugin.
- **pytest with a 95% coverage gate** (branch coverage). It is about 99% now; keep it there
  and add tests with every change. Warnings are errors (`filterwarnings = ["error"]`).
- **Playwright E2E** in `tests/e2e/` against real HTTP servers (fake GitLab + app).
- **Migration drift** (`tests/test_db.py::test_models_and_migrations_do_not_drift`): models
  must match the Alembic migrations. Every schema change needs a new numbered migration
  (`0005_…`, `0006_…`); never edit a released migration. Upgrades must never require deleting
  the database.
- **Docs** (`tests/test_docs.py`): relative links in `README.md`, `CHANGELOG.md`, `SECURITY.md`
  and `docs/*.md` must resolve; `docs/ARCHITECTURE.md` must keep the C4 headings
  ("Level 1: System context", "Level 2: Containers", "Level 3: Components") and at least six
  `mermaid` blocks.
- Tests never need a live GitLab: use the `tests/conftest.py` fixtures (fake GitLab over
  `httpx.ASGITransport`, migrated temporary SQLite). They strip all `TEAMPULSE_*` env vars and
  the Docker secret, so a developer `.env` cannot leak into tests.

## Architecture rules

- **One door to GitLab.** Only `gitlab/` makes HTTP calls to GitLab or touches raw JSON.
  Everything else uses the typed records and typed errors (`GitLabAuthError` fails fast,
  rate-limit and unavailable errors retry within bounds, `GitLabCapabilityError` becomes a
  diagnostic).
- **The caller owns the transaction.** `store.py` only stages changes; a sync step commits its
  data, its sync state and the bumped version counter together, or not at all.
- **Reads never trigger crawls.** API handlers read the SQLite cache only; at most they wake
  the scheduler.
- **Last known good wins.** A failed refresh records a diagnostic and marks data stale, but
  never replaces or deletes good data. Retention never removes the only good snapshot.
- **Version counters.** `data_version` (dashboard), `users_version` (directory and selection),
  `teams_version` (teams). Each browser view refetches only when its own counter moves; bump
  the right one when you add a write path.
- **Low GitLab load.** Detailed crawling only for selected users and team members (history
  categories only, capped by `TEAMPULSE_MAX_TRACKED_USERS`); work, activity and timelogs every
  10 minutes; directory, history and milestones hourly; incremental where possible; bounded
  concurrency. See the sync category table in `docs/ARCHITECTURE.md`.
- **GitLab Free only.** Do not use Premium APIs (burndown, weights, iterations, blocking links,
  analytics). Compute the signal from Free endpoints instead, as milestone progress does from
  the milestone's issues. Epics stay optional behind a capability probe.
- **Read-only.** A `read_api` token suffices; never send mutations to GitLab.
- **Single process.** One worker owns the scheduler; no Redis, Celery, external DB or queue
  (VISION §4).

### Frontend rules

- No build step, no Node.js, no CDN, no new runtime libraries. Plain ES modules and native SVG
  or canvas.
- CSP-safe: `style-src 'self'` and `script-src 'self'`. No inline `<script>`, no `style="…"`
  attributes, no inline event handlers. Setting `element.style.x` from JS is fine.
- All GitLab-originated text goes through `textContent` (or the `dom.js` helpers), never
  `innerHTML`.
- The `dom.js` helpers skip `null`/`undefined`/`false` children, but native `append()` and
  `replaceChildren()` do not: never pass them a null child (it renders as "null").
- Data text stays selectable; color is never the only signal (icon or text label too).

## Product principles

- **Facts, not inference.** Time comes from GitLab timelogs only; no estimated hours, no
  40-hour judgment, no productivity scores.
- **No scoring or ranking of people.** Team reports show team totals with deltas. The
  per-person table is off by default (`TEAMPULSE_TEAM_PERSON_BREAKDOWN`); when on, it is
  alphabetical, has no deltas and is not sortable by a number.
- **People first.** People are the top-level concept; projects are context.
- Read `VISION.md` §4 (non-goals) and §5 (principles) before proposing features.

## Fake GitLab and demo

- `fake_gitlab.py` is deterministic (seeded `random.Random`). Tests, the demo and the README
  screenshots depend on its data staying stable.
- New demo fields or history must come from a **separate random stream**, as `enrich()` does
  (`random.Random(seed + 101)`), so existing data does not shift.
- `make demo` (or `uv run gitlab-team-pulse demo`) starts the fake GitLab, pre-selects people,
  creates a sample team and enables the person breakdown. It uses its own `demo.db`.
- Regenerate screenshots with `uv run python tools/screenshots.py` (starts the demo on free
  ports, writes `docs/media/*.png`).

## Running against the real GitLab

- Put `TEAMPULSE_GITLAB_URL`, `TEAMPULSE_GITLAB_TOKEN` and `TEAMPULSE_TIMEZONE` in the ignored
  `.env`; `make run` serves on port 8000. `uv run gitlab-team-pulse doctor` checks the setup.
- The demo also binds 8000: stop one before starting the other.
- Never print, log, echo or commit a token, `.env` or `secrets/`. The token is only ever sent
  to the configured GitLab host.

## Commits and releases

- Conventional Commits, atomic, e.g. `feat(teams): …` or `fix(retention): …`. The release commit
  carries the version: `chore(release): <summary> (vX.Y.Z)`. Never mention AI, Claude or an
  assistant in commit messages.
- Version lives in `src/gitlab_team_pulse/__init__.py`. Bump with
  `uv run python tools/bump_version.py patch` per release commit, `minor` for major features.
  Add a `CHANGELOG.md` entry.
- Workflow: feature branch; full `./localPipeline.sh` green; review the branch with the user's
  `/reviewBranch` skill and fix confirmed findings; pipeline again; fast-forward merge to
  `master`; annotated tag `vX.Y.Z`; push; `gh release create vX.Y.Z dist/*.whl dist/*.tar.gz`
  (after `make build`); wait for the GitHub Actions *Local Pipeline* and *Docker Image* runs.
- Commit, push and release as the user's working instructions say (typically: every commit
  green, push continuously). Without such instructions, do not push, tag or release.
- Dependencies are pinned exactly (`==`) in `pyproject.toml`, with `uv.lock` updated. The uv
  version is pinned in three places that must stay in sync: `Dockerfile`
  (`ghcr.io/astral-sh/uv:X`), `.github/workflows/local-pipeline.yml` (setup-uv `version`) and
  `.gitlab-ci.yml` (`uv==X`).

## Where to document

- `README.md`: features, configuration table (every new `TEAMPULSE_*` setting), HTTP API list.
- `docs/ARCHITECTURE.md`: components, rules, sync categories, data model (keep the C4 headings
  and Mermaid diagrams).
- `docs/features/*.md`: per-feature rules and counting (e.g. `steering-signals.md`).
- `VISION.md`: amend when a product decision changes scope or principles.
- `plan.md`: the current implementation plan; `CHANGELOG.md`: release notes.
- After UI changes, regenerate the screenshots (see above).
