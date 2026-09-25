# Architecture

This document describes GitLab Team Pulse with the [C4 model](https://c4model.com/): system
context (level 1), containers (level 2) and components (level 3). It adds a dynamic view of
the refresh flow, the freshness state model and the data model. All diagrams are
[Mermaid](https://mermaid.js.org/), so they render on GitHub and GitLab and stay reviewable as
text next to the code. The diagrams use C4 notation (people, software systems, containers,
components, boundaries, the usual C4 colours and `[Type: technology]` labels) drawn as Mermaid
flowcharts. Mermaid's experimental `C4Context` layout overlaps labels, and flowcharts render
cleanly everywhere Mermaid is supported.

Related documents: [`README.md`](../README.md) (usage and configuration),
[`GRAPHQL.md`](GRAPHQL.md) (why GraphQL is used for timelogs and epics),
[`VISION.md`](../VISION.md) (requirements) and [`CHANGELOG.md`](../CHANGELOG.md).

## Architectural drivers

The decisions below follow from a few requirements in the vision:

| Driver | Consequence in the design |
| --- | --- |
| Fast UI, never blocked by GitLab (§5.4, §31) | The browser reads only the local SQLite cache; GitLab is crawled in the background |
| Resilience: never replace good data with failure (§5.5, §17) | Per-user, per-dataset transactions; failures record diagnostics but keep the last good snapshot |
| Minimal GitLab load (§5.6, §20) | Detailed crawling only for selected users, incremental activity, on-demand project metadata, bounded concurrency |
| Observable by default (§5.7, §29) | Freshness on every payload, persisted diagnostics, readable stdout logs |
| Simple deployment (§5.8, §21.5) | One Python process, one SQLite file, exactly one worker (single scheduler owner) |
| No Node.js toolchain, no CDN (§22) | Plain HTML/CSS/ES modules and native SVG charts shipped inside the Python package |

## Level 1: System context

```mermaid
flowchart TB
    lead(["<b>Technical lead / manager</b><br/>[Person]<br/>wants a current cross-project<br/>view of a few GitLab users"])
    operator(["<b>Operator</b><br/>[Person]<br/>deploys the container,<br/>reads logs and diagnostics"])

    pulse["<b>GitLab Team Pulse</b><br/>[Software System]<br/>people-centric activity dashboard<br/>served from a local cache"]

    gitlab["<b>Self-managed GitLab</b><br/>[Software System]<br/>REST v4 and GraphQL: users, work,<br/>events, timelogs"]
    avatars["<b>Avatar host</b><br/>[Software System]<br/>GitLab uploads or Gravatar; images only"]

    lead -->|"selects people, reads the dashboard<br/>(HTTPS, internal network)"| pulse
    operator -->|"configures and monitors<br/>(env vars, Docker secret, /api/health)"| pulse
    pulse -->|"reads, never writes<br/>(HTTPS, read_api token)"| gitlab
    lead -.->|"browser loads avatar images"| avatars

    classDef person fill:#08427b,stroke:#052e56,color:#fff
    classDef system fill:#1168bd,stroke:#0b4884,color:#fff
    classDef external fill:#999999,stroke:#6b6b6b,color:#fff
    class lead,operator person
    class pulse system
    class gitlab,avatars external
```

- **Only the server talks to GitLab.** The browser never calls GitLab, and the token never
  leaves the server.
- **Read-only.** A `read_api` token is enough, and no mutations are ever sent.
- **Trust model.** Anyone on the internal network can open the app (VISION §6.3); access is
  limited by deployment (firewall, reverse proxy), not by a login screen.

## Level 2: Containers

```mermaid
flowchart TB
    lead(["<b>Technical lead / manager</b><br/>[Person]"])

    subgraph pulse["GitLab Team Pulse: one Python process, one worker [Software System]"]
        direction TB
        spa["<b>Browser UI</b><br/>[Container: HTML, CSS, ES modules, SVG]<br/>People and Dashboard views;<br/>polls /api/status for version changes"]
        api["<b>Web application</b><br/>[Container: FastAPI on Uvicorn;<br/>Gunicorn with 1 worker in Docker]<br/>JSON API and static assets;<br/>reads only the cache"]
        worker["<b>Background synchronization</b><br/>[Container: asyncio scheduler + SyncService]<br/>directory hourly · selected users every 10 min<br/>cleanup hourly · manual refresh"]
        db[("<b>Cache and state</b><br/>[Container: SQLite WAL,<br/>SQLAlchemy, Alembic]<br/>users, work, events, timelogs,<br/>projects, sync state, diagnostics")]
    end

    gitlab["<b>Self-managed GitLab</b><br/>[Software System]<br/>REST v4 and GraphQL"]

    lead -->|"uses (HTTPS)"| spa
    spa -->|"status, dashboard, users,<br/>selection, refresh (JSON)"| api
    api -->|"reads cache,<br/>writes selection (SQL)"| db
    api -->|"wakes for refresh and<br/>new selections (in-process)"| worker
    worker -->|"commits snapshots<br/>atomically (SQL)"| db
    worker -->|"reads only: read_api<br/>(HTTPS, bounded concurrency)"| gitlab

    classDef person fill:#08427b,stroke:#052e56,color:#fff
    classDef container fill:#438dd5,stroke:#2e6295,color:#fff
    classDef database fill:#438dd5,stroke:#2e6295,color:#fff
    classDef external fill:#999999,stroke:#6b6b6b,color:#fff
    classDef boundary fill:none,stroke:#444,stroke-dasharray:6 4,color:#444
    class lead person
    class spa,api,worker container
    class db database
    class gitlab external
    class pulse boundary
```

| Container | Technology | Responsibility | Scaling note |
| --- | --- | --- | --- |
| Browser UI | Static files in `static/` | Rendering, filtering, sorting and theme, all client-side | Polling is cheap: a compact `/api/status` plus version counters |
| Web application | FastAPI (`app.py`) | HTTP API, security headers (CSP, no CORS), static assets | Stateless reads from SQLite |
| Background synchronization | `scheduler.py`, `sync.py` | Everything that talks to GitLab | **Exactly one instance**: it owns the scheduler |
| Cache and state | SQLite (`models.py`, `migrations/`) | Read model and application state | One file on a persistent volume (`/data`) |

The web application and the synchronization run in **one process**. That is why deployments
use a single worker: several workers would start several schedulers against the same database
(VISION §21.5, §44.1).

## Level 3: Components (inside the Python process)

```mermaid
flowchart TB
    subgraph proc["Python process [Container]"]
        direction TB
        subgraph web["Web side: reads only"]
            routes["<b>API router</b><br/>[Component: app.py]<br/>health, status, users, selection,<br/>dashboard, errors, refresh"]
            reader["<b>Read model</b><br/>[Component: dashboard.py]<br/>batched queries, URL sanitizing"]
            fresh["<b>Freshness classifier</b><br/>[Component: freshness.py]<br/>fresh / refreshing / stale / error / never"]
        end
        subgraph bg["Background side: talks to GitLab"]
            sched["<b>Scheduler</b><br/>[Component: scheduler.py]<br/>due checks, coalescing,<br/>throttling, crash containment"]
            sync["<b>SyncService</b><br/>[Component: sync.py]<br/>per-dataset last-known-good"]
            ret["<b>Retention</b><br/>[Component: retention.py]<br/>never removes the only good snapshot"]
            client["<b>GitLabClient</b><br/>[Component: gitlab/client.py]<br/>REST + GraphQL, pagination,<br/>retries, bounded concurrency"]
            norm["<b>Normalizers</b><br/>[Component: gitlab/models.py]<br/>raw JSON to typed records"]
        end
        store["<b>Store</b><br/>[Component: store.py]<br/>snapshots, sync state, runs,<br/>errors, version counters"]
        config["<b>Settings</b><br/>[Component: config.py]<br/>env, .env, Docker secret;<br/>read by all components"]
    end

    db[("<b>SQLite</b><br/>[Container]")]
    gitlab["<b>Self-managed GitLab</b><br/>[Software System]"]

    routes --> reader
    reader --> fresh
    routes -->|"request_refresh,<br/>request_user_sync"| sched
    sched -->|runs jobs| sync
    sched -->|"hourly, worker thread"| ret
    sync --> client
    client --> norm
    client -->|HTTPS| gitlab
    sync -->|"one transaction<br/>per dataset"| store
    reader -->|reads| db
    store -->|writes| db
    ret -->|deletes superseded data| db

    classDef component fill:#85bbf0,stroke:#5d82a8,color:#000
    classDef database fill:#438dd5,stroke:#2e6295,color:#fff
    classDef external fill:#999999,stroke:#6b6b6b,color:#fff
    classDef boundary fill:none,stroke:#444,stroke-dasharray:6 4,color:#444
    class routes,reader,fresh,sched,sync,ret,client,norm,store,config component
    class db database
    class gitlab external
    class proc,web,bg boundary
```

### Component rules

- **One door to GitLab.** Only `gitlab/` speaks HTTP to GitLab or sees raw JSON (VISION §27).
  Everything else works with typed `GitLabUser`, `WorkItem`, `ActivityEvent` and `Timelog`
  records. GitLab failures surface as typed errors: `GitLabAuthError` (fail fast),
  `GitLabRateLimitError` / `GitLabUnavailableError` (bounded retries), `GitLabCapabilityError`
  (feature missing, shown as a diagnostic) and `GitLabResponseError` (contract violation).
- **The caller owns the transaction.** `store.py` only stages changes, so each sync step
  commits its data, its sync state and the bumped version counter together, or not at all.
- **Reads never trigger crawls.** The API router uses the read model only; the most it can do
  to the scheduler is wake it.
- **Two version counters.** `data_version` changes on every sync step (the dashboard);
  `users_version` changes only with the directory or the selection (the People view). Each
  browser view refetches only when its own counter moves.

## Dynamic view: manual "Refresh now"

```mermaid
sequenceDiagram
    autonumber
    actor Lead as Lead (browser)
    participant UI as Browser UI
    participant API as FastAPI
    participant Sch as Scheduler
    participant Sync as SyncService
    participant GL as GitLab
    participant DB as SQLite

    Lead->>UI: click "Refresh now"
    UI->>API: POST /api/refresh
    API->>Sch: request_refresh()
    alt a refresh is already running or pending
        Sch-->>API: coalesced
    else requested moments ago
        Sch-->>API: throttled (429 + Retry-After)
    else
        Sch-->>API: started (wakes the loop)
    end
    API-->>UI: 202 Accepted (returns immediately)
    UI->>UI: show "Refreshing…", keep cached data visible
    Sch->>Sync: sync_selected("manual")
    par for each selected user, per dataset
        Sync->>GL: issues, MRs, reviews, epics (GraphQL)
        Sync->>GL: events since the cursor (incremental)
        Sync->>GL: timelogs, last 7 days (GraphQL)
        Sync->>GL: events for the contribution calendar (hourly at most, incremental)
    end
    alt dataset fetched
        Sync->>DB: one transaction: replace snapshot, mark success, bump data_version
    else dataset failed
        Sync->>DB: record diagnostic, mark failure (last good data untouched)
    end
    loop every 2 s while refreshing (otherwise every 20 s)
        UI->>API: GET /api/status
        API-->>UI: crawler state, freshness, data_version
    end
    UI->>API: GET /api/dashboard (data_version changed)
    API->>DB: batched reads
    API-->>UI: cards with freshness metadata
    UI->>Lead: "Updated just now", or a stale/error banner with the last good data
```

## Freshness state model

Each dataset (the directory, the selected-user refresh, and every user's work, activity,
timelogs and contribution calendar) is classified independently, and the UI combines the
results without hiding them. The calendar uses its own, longer refresh interval, so it is not
reported as stale between its hourly refreshes.

```mermaid
stateDiagram-v2
    [*] --> Never: no successful sync yet
    Never --> Refreshing: sync starts
    Refreshing --> Fresh: success
    Refreshing --> Error: failure (last good data kept)
    Fresh --> Stale: older than interval + grace
    Fresh --> Refreshing: scheduled or manual sync
    Stale --> Refreshing: sync starts
    Error --> Refreshing: retry
    Error --> Error: repeated failure (folded into one diagnostic)
    note right of Error
        Stale and error flags are kept alongside
        the status, so "refreshing on top of stale data"
        or "error with 2 h old data" stays visible.
    end note
```

## Data model

```mermaid
erDiagram
    users ||--o{ work_item_assignees : "assigned / reviewer"
    work_items ||--o{ work_item_assignees : "links"
    users ||--o{ activity_events : authored
    users ||--o{ timelogs : logged
    users ||--o{ sync_state : "per dataset"
    users ||--o{ contribution_days : "calendar"
    projects |o--o{ work_items : "project_id (resolved on demand)"
    projects |o--o{ activity_events : "project_id"
    projects |o--o{ timelogs : "project_id"

    users {
        int id PK "GitLab user id"
        string username
        string account_type "human, bot, service, unknown"
        string state "active, blocked, deactivated"
        bool selected "global selection"
    }
    work_items {
        int id PK
        string kind "issue, merge_request, epic"
        int gitlab_id "unique with kind"
        string state "any state is kept"
        datetime updated_at
    }
    work_item_assignees {
        int work_item_id FK
        int user_id FK
        string relation "assignee, reviewer"
    }
    activity_events {
        int id PK "GitLab event id"
        string category "push, comment, issue, merge_request, other"
        datetime occurred_at
    }
    timelogs {
        int id PK "GitLab timelog id"
        int seconds "actual logged time"
        datetime spent_at
    }
    projects {
        int id PK
        string path_with_namespace
    }
    contribution_days {
        int user_id PK
        date day PK "local calendar day"
        int count "GitLab contribution rule"
    }
    sync_state {
        string key PK "e.g. activity:42, directory, selected"
        string status "never, ok, error, partial"
        datetime last_success_at "last known good"
        string cursor "incremental watermark"
    }
    sync_runs {
        string id PK
        string trigger "startup, scheduled, manual, selection"
    }
    errors {
        int id PK
        string subsystem
        int occurrences "repeats folded"
        datetime resolved_at
    }
    app_state {
        string key PK "data_version, users_version"
    }
```

The schema is versioned with Alembic (`src/gitlab_team_pulse/migrations/`) and migrated on
startup. Upgrades never require deleting the database, and tests check both a fresh
migration and the upgrade from the first revision with existing data.

## Deployment view

```mermaid
flowchart LR
    subgraph net["Internal network"]
        browser["Browser"]
        proxy["Reverse proxy (optional: TLS, SSO)"]
        subgraph host["Docker host"]
            subgraph ctr["Container: ghcr.io/marcelpetrick/gitlabteampulse (uid 10001)"]
                entry["docker-entrypoint.sh: migrate, then Gunicorn (1 Uvicorn worker)"]
            end
            vol[("Volume /data: teampulse.db")]
            secret[/"Docker secret /run/secrets/gitlab_token"/]
        end
    end
    gitlab["Self-managed GitLab"]

    browser --> proxy --> ctr
    ctr --- vol
    secret -.read by the app.-> ctr
    ctr -->|HTTPS read_api| gitlab
```

- **Image:** non-root, with a healthcheck on `/api/health`. It logs to stdout and ships for
  `linux/amd64` and `linux/arm64`.
- **State:** lives only in `/data`, so restarts reuse the cache and the selection (no crawl
  from zero).
- **Scaling:** one replica. Scaling out would need an external scheduler owner and database,
  which VISION §4 lists as non-goals for the prototype.

## Quality attributes and how they are verified

| Attribute | Mechanism | Verified by |
| --- | --- | --- |
| Last-known-good resilience | Per-dataset transactions, conservative retention | `tests/test_retention.py` (VISION §37.3 scenario), `tests/test_sync_selected.py` |
| Low upstream load | Selection-driven crawl, incremental events, project negative cache | Request-count assertions in `tests/test_sync_selected.py` |
| Bounded concurrency and retries | Semaphore, exponential backoff with jitter, `Retry-After` | `tests/test_gitlab_client.py` |
| Secrets never leak | `SecretStr`, log redaction, same-host pagination | `tests/test_api.py::test_token_never_leaks`, Docker smoke test |
| UI behaviour | Plain ES modules, polling on version counters | Playwright suite in `tests/e2e/` |
| Maintainability | Strict mypy, Ruff, 95 % coverage gate, Alembic drift check | `localPipeline.sh` in CI |
