# GitLab Team Activity Dashboard

## Product Vision and Prototype Requirements

**Document status:** Consolidated prototype vision after requirements discovery  
**Primary implementation language:** Python  
**Target environment:** Internal/on-premise deployment  
**Primary source system:** Self-managed GitLab  
**Prototype database:** SQLite, persistent and schema-versioned

---

## 1. Executive Summary

The GitLab Team Activity Dashboard is an internal web application that gives a technical lead or manager a concise cross-project view of what selected GitLab users have recently been doing.

GitLab activity is naturally fragmented across projects, issues, merge requests, epics/work items, comments, pushes, commits, and timelogs. The purpose of this product is to aggregate that information into one attractive, quickly understandable dashboard so that a user does not need to open many projects or repeatedly ask contributors for a basic status overview.

The first deliverable is a deliberately small but polished prototype. It shall:

- discover and cache the GitLab user directory;
- allow any GitLab account, including bot and service accounts, to be selected;
- keep the selected set globally for all dashboard viewers;
- refresh selected users approximately every ten minutes;
- provide a manual **Refresh now** action;
- show recently assigned/touched work regardless of whether the work is currently open, closed, resolved, or otherwise completed;
- show a quick preview of the five most recent actions and make the twelve most recent actions available in the detailed activity view;
- show seven days of GitLab activity visually;
- show factual GitLab timelog entries and totals for the last seven days;
- persist cached data and synchronization state in SQLite;
- continue showing the last known good data when GitLab is unavailable;
- clearly communicate freshness, staleness, refresh-in-progress state, and backend errors;
- provide a polished light and dark user interface;
- remain simple to run locally and later in Docker;
- minimize load on the GitLab server by only crawling detailed data for selected users.

The prototype is an operational overview. It is not an employee productivity scoring system and it shall not infer work from activity timestamps when hard GitLab data is available.

---

## 2. Product Vision

A user opens one internal web application, chooses a small number of GitLab accounts, and receives an immediate answer to:

> What has each selected person recently worked on, what is assigned to them, what changed recently, and how current is the information I am looking at?

The experience should be good enough to leave open on a second monitor throughout the day.

The application should feel like a real internal product, not a developer API demo. Information density can be relatively high, but visual hierarchy, spacing, typography, status feedback, charts, and interaction quality must make the information easy to scan.

---

## 3. Goals

The first prototype has the following goals:

1. **Cross-project visibility**  
   Provide one people-centric view across multiple GitLab projects and namespaces.

2. **Low-friction status discovery**  
   Reduce the need to ask contributors what they are working on when the relevant information already exists in GitLab.

3. **Fact-based activity overview**  
   Use GitLab assignments, events, states, updates, and actual timelog records instead of inferred activity.

4. **Fast UI from a local cache**  
   The browser should read almost entirely from the local SQLite cache. It must not wait for GitLab on every page load.

5. **Low GitLab server load**  
   Crawl the minimum information required. Fetch the directory broadly, but fetch detailed work and activity primarily for selected users.

6. **Resilience**  
   GitLab downtime must degrade the dashboard into a clearly stale cached view, not an empty screen.

7. **Polished user experience**  
   The interface must be visually credible, responsive, readable, copy-friendly, and clear about system state.

8. **Maintainable engineering foundation**  
   Use schema migrations, tests, conventional commits, atomic commits, packaging, logging, and CI from the beginning.

---

## 4. Explicit Non-Goals for the Prototype

The first prototype will not attempt to provide:

- employee performance scoring;
- automated productivity judgments;
- a hard-coded 40-hour compliance alarm;
- inferred work hours from GitLab activity timestamps;
- a full replacement for GitLab analytics;
- project planning or issue editing;
- write operations back to GitLab;
- long-term activity warehousing;
- a project-centric dashboard;
- notifications by email, Slack, Teams, or other external channels;
- complex application-level user authentication or role-based access control;
- multiple GitLab instances;
- Redis, Celery, Kafka, or another external queue;
- PostgreSQL or another external database;
- a separate Node.js frontend build pipeline;
- enterprise-scale distributed crawling;
- a native mobile application.

These may be considered later if the prototype proves useful.

---

## 5. Product Principles

### 5.1 People first

The prototype is organized around people, not projects. Projects provide context for work but are not the top-level navigation concept.

### 5.2 Useful before comprehensive

Implement the smallest feature set that replaces repeated manual GitLab browsing. Do not prematurely reproduce the entire GitLab interface.

### 5.3 Hard facts before inference

Time information must come from GitLab timelogs. Work state, update times, comments, pushes, and other actions must use source data rather than estimation.

### 5.4 Cached first

The local database is the dashboard's immediate read model. GitLab synchronization happens asynchronously.

### 5.5 Never replace good data with failure

A failed refresh may mark data stale, but it must not delete or replace the last successful snapshot with an empty or incomplete result.

### 5.6 Minimal upstream load

Do not enumerate and deeply crawl every project merely because it exists. Discover users broadly, then perform detailed synchronization for selected users.

### 5.7 Observable by default

Freshness, crawler state, errors, and useful logs must be visible without attaching a debugger.

### 5.8 Simple deployment

One Python application and one SQLite database are sufficient for the prototype.

---

## 6. Target Users and Usage Pattern

### 6.1 Primary user

A technical lead, engineering manager, project lead, or other internal user who needs a current cross-project overview of a small number of GitLab users.

### 6.2 Expected use

A typical dashboard selection is approximately four to ten people, although the GitLab directory itself may contain many more accounts.

The product must therefore scale differently in two places:

- the user directory must remain usable with many users;
- expensive detail crawling should remain limited to the currently selected users.

### 6.3 Network trust model

For the prototype, anyone who can reach the application on the trusted internal network may open it. The application does not require its own login screen in the first version.

Network exposure must therefore be intentionally limited through deployment controls such as internal routing, firewall policy, or a reverse proxy.

---

## 7. Resolved Discovery Decisions

The following decisions are considered resolved requirements for the first prototype.

| Topic | Decision |
|---|---|
| Primary organization | People-centric, not project-centric |
| Main UI structure | Two primary views in one application shell: **People** and **Dashboard** |
| User population | Show all GitLab accounts visible to the configured credentials; do not exclude bots, service accounts, blocked accounts, or inactive accounts by default |
| Bot/service accounts | They remain selectable; account-type filters may be offered where GitLab provides reliable metadata |
| User filtering | Live quick filter while typing; filtering never changes selection state |
| Selection scope | Global/shared selection for all dashboard viewers |
| Selection persistence | Store globally in SQLite and restore after restart |
| Default user ordering | Account creation date as the available proxy for GitLab join date; newest-first default is acceptable, with sorting controls |
| Presence/online state | Do not show an online/recently-active presence indicator |
| Detailed crawl scope | Detailed data is fetched primarily for selected users, not for every account/project |
| Assigned work | Include all relevant assigned work types supported by the GitLab installation, including issues, merge requests, and epic/work-item style records where available |
| Work state | Do not filter work out merely because it is closed/resolved/completed |
| Work default ordering | Last updated, newest first |
| Work sorting | User-selectable sorting, including last-updated ascending/descending and alphanumeric title sorting |
| Activity preview | Show the five latest actions at a glance |
| Activity detail | Retain/expose the twelve latest relevant actions per selected user |
| Activity chart | Show the last seven days |
| Timelog source | Actual GitLab time tracking/timelog entries only; no estimation |
| Timelog window | Last seven days; this supersedes the earlier five-day idea |
| Time expectations | Make totals prominent, but do not hard-code a 40-hour judgment because schedules may differ |
| Automatic detailed refresh | Approximately every ten minutes |
| User-directory refresh | Approximately every hour |
| Manual refresh | A **Refresh now** control triggers an immediate synchronization attempt |
| Refresh feedback | Clearly show when a refresh is running and when resulting data becomes current |
| UI self-update | An open page updates automatically when newer cached data arrives |
| Freshness | Show exact refresh time and a human-readable relative age such as `10 seconds ago` |
| Failed refresh | Keep displaying the last known good data and visibly mark it stale/error |
| Retention | Short rolling cache, approximately one day for superseded historical data |
| Retention exception | Never purge the newest known-good data merely because it is older than one day when no newer successful data exists |
| Database cleanup | Automatic cleanup keeps SQLite compact |
| Theme | Light/dark toggle; browser-session persistence is sufficient |
| Copyability | Ordinary text selection and Ctrl/Cmd+C must work throughout data views |
| External notifications | None in the prototype |
| Dashboard access | Internal-network access without application-level authentication for now |
| Secret delivery | Local environment/.env during development; Docker secrets preferred for container deployment |
| Project crawling | Resolve project metadata only as needed for selected-user data; do not build a project dashboard yet |

---

## 8. Information Architecture and Main Navigation

The prototype should use one application shell with two primary views rather than one extremely long page.

### 8.1 People view

Purpose: select and manage the globally monitored GitLab accounts.

Contains:

- search/filter input;
- user list;
- account metadata;
- selection checkbox;
- optional account type/state badges;
- user sorting controls;
- selected count;
- clear indication that selection is shared/global;
- navigation to the Dashboard view.

### 8.2 Dashboard view

Purpose: show the selected users and their recent work/activity.

Contains:

- overall refresh/status toolbar;
- **Refresh now** button;
- global freshness indicator;
- one summary section/card per selected user;
- recently assigned/touched work;
- recent activity preview and detail;
- seven-day activity visualization;
- seven-day timelog summary;
- backend diagnostics/error access;
- persistent footer/status area.

### 8.3 Empty states

If no users are selected, the Dashboard must not look broken. It should explain that no users are currently monitored and provide a clear action to go to the People view.

If selected users exist but data has not yet been synchronized, the UI should show an explicit first-sync/loading state.

---

## 9. User Directory Requirements

### 9.1 Directory contents

The system shall retrieve all GitLab user accounts visible to the configured API credentials.

The UI must not silently exclude:

- normal users;
- bots;
- service accounts;
- blocked users;
- inactive users;
- other GitLab account types returned by the server.

Where GitLab provides reliable account-type metadata, the UI may expose filters or badges such as:

- Human;
- Bot;
- Service;
- Blocked;
- Inactive;
- Unknown.

The dashboard must avoid pretending that an inferred classification is authoritative. Unknown accounts may simply be shown as `Unknown`.

### 9.2 User fields

Each directory row should contain, where available:

- display name;
- username;
- avatar;
- GitLab user ID;
- account creation date;
- account state;
- account type/bot indicator if authoritative;
- selection checkbox.

### 9.3 Filtering

The quick filter shall update the visible list while the user types.

It should match at least:

- display name;
- username.

Optional matching may include account type or ID if useful.

Filtering is presentation-only. Example:

1. type `alex`;
2. select one matching user;
3. clear the filter;
4. the user remains selected.

### 9.4 Sorting

The default directory sort uses account creation time, which acts as the prototype's practical proxy for when the account appeared in GitLab.

The UI should permit at least:

- created date ascending;
- created date descending;
- display name A-Z;
- display name Z-A;
- username A-Z;
- username Z-A.

### 9.5 Global selection

Selections are global application state.

If one browser selects or deselects a user, other dashboard viewers should observe that changed selection after their next lightweight UI refresh.

The selection is stored in SQLite and survives application restarts.

---

## 10. Selected User Dashboard Card

Each selected user receives a distinct, scan-friendly dashboard section.

The section should show:

- avatar;
- display name;
- username;
- most recent successful data refresh time;
- relative freshness age;
- current refresh status for that user if applicable;
- count of work items shown;
- seven-day logged-time total;
- five-action activity preview;
- seven-day activity visualization;
- recently assigned/touched work table/list;
- access to all twelve retained recent actions.

The card should favor useful density over oversized decorative components.

---

## 11. Assigned and Recently Relevant Work

### 11.1 Scope

The dashboard shall attempt to represent everything currently or recently relevant that is assigned to the selected user and available through the configured GitLab APIs.

At minimum this includes:

- issues;
- merge requests.

Where supported by the installed GitLab version/license/API, the integration should also include:

- epics;
- work items or equivalent assignable planning objects.

The integration layer should normalize those object types behind a common local representation while preserving their original type.

### 11.2 State-independent visibility

A work item must not disappear merely because its state changes from active/open to fixed/closed/resolved/completed.

This is necessary because recently completed work is important context. A user may finish several items in a short period, and filtering by `open` alone would make that progress disappear from the dashboard.

The crawler should query all relevant states where the API permits and retain recently observed records within the rolling cache.

### 11.3 Fields

For each work item, show where available:

- object type;
- title;
- project/group namespace;
- current state;
- labels;
- milestone;
- priority if available;
- due date;
- assignee context;
- last updated timestamp;
- GitLab web URL.

### 11.4 Default ordering

Default: **last updated descending**.

The most recently changed work should be easiest to see.

### 11.5 Interactive sorting

The user should be able to change sorting without reloading data from GitLab.

At minimum:

- last updated newest-first;
- last updated oldest-first;
- title A-Z;
- title Z-A.

Additional inexpensive sorts may be added for:

- project;
- state;
- due date;
- object type.

Sorting is performed against locally cached data.

### 11.6 Copyability

Titles, project names, states, labels, identifiers, timestamps, and descriptions must remain normal selectable browser text.

The UI must not globally disable text selection or intercept ordinary Ctrl+C / Cmd+C behavior.

Buttons, links, badges, and charts should be designed so they do not unnecessarily make nearby content difficult to select.

---

## 12. Recent Activity

### 12.1 Purpose

Recent activity answers: **What did this user recently do in GitLab?**

Relevant actions may include, where exposed by GitLab:

- pushes;
- commits;
- issue creation;
- issue updates;
- merge request creation;
- merge request updates;
- comments/notes;
- state changes;
- other useful user events.

### 12.2 Five-action preview

The user summary section should display the five most recent actions immediately for fast scanning.

### 12.3 Twelve-action detail

The application shall fetch/cache and expose the twelve most recent relevant actions for each selected user.

The additional seven actions may be shown through:

- an expanded area;
- `Show all 12`;
- a compact activity drawer;
- another visually appropriate expansion mechanism.

This reconciles fast overview with the requirement to retain a slightly deeper recent history.

### 12.4 Activity event fields

Each event should display, where available:

- timestamp;
- relative time;
- event/action type;
- project;
- short human-readable description;
- direct GitLab link.

The description should be concise and understandable without exposing raw API JSON.

---

## 13. Seven-Day Activity Visualization

For each selected user, the dashboard shall include a visualization covering the most recent seven days.

The baseline chart should show activity volume per day.

Where reliable classifications are available, distinguish categories such as:

- pushes/commits;
- comments/notes;
- issues/work items;
- merge requests;
- other.

### UX requirements

The chart should:

- be readable in both light and dark themes;
- have useful hover/tooltips;
- show explicit calendar dates;
- avoid implying a productivity score;
- remain understandable when a day has zero activity;
- resize cleanly with the user card.

Chart data comes from the local cache, not directly from GitLab in the browser.

---

## 14. GitLab Timelog Requirements

### 14.1 Source of truth

Logged time must use actual GitLab time-tracking/timelog data entered by users.

The product shall not estimate time from:

- commit timestamps;
- login activity;
- issue updates;
- active browser sessions;
- event counts.

### 14.2 Time window

The default dashboard window is the **last seven days**.

This supersedes the earlier five-day concept from the initial draft.

### 14.3 Display

For each selected user, show:

- total duration logged in the last seven days;
- daily breakdown;
- ticket/work-item references where available;
- optional per-project breakdown if inexpensive to calculate from cached data.

A chart or compact bar visualization may complement the numeric total.

### 14.4 No hard-coded work-hour judgment

The UI should make missing or low totals easy to notice, but the first prototype shall not label a user as compliant/noncompliant against a hard-coded 40-hour rule.

If an explicit expected-hours configuration is later introduced, that can become a separate feature.

---

## 15. Refresh Model

There are two scheduled synchronization classes plus manual refresh.

### 15.1 User-directory refresh

Target frequency: approximately once per hour.

The refresh shall:

- retrieve all user accounts visible to the token;
- add newly discovered users;
- update changed account metadata;
- preserve global selection state;
- record attempt and success timestamps;
- avoid deleting cached users solely because one API call failed.

### 15.2 Selected-user refresh

Target frequency: approximately once every ten minutes.

Only selected users receive this higher-frequency crawl.

For each selected user, synchronize as efficiently as possible:

- assigned/recently relevant work;
- current work states;
- recent activity;
- twelve recent events;
- seven-day activity data;
- seven-day timelogs;
- referenced project/group metadata;
- synchronization timestamps.

### 15.3 Manual refresh

The Dashboard shall provide **Refresh now**.

When invoked:

1. the backend accepts the refresh request;
2. the frontend immediately shows a refresh-in-progress state;
3. duplicate manual refreshes are prevented or coalesced while one is running;
4. the existing cached data remains visible during the refresh;
5. successful results are committed to SQLite;
6. the frontend observes the new cache state and updates;
7. freshness changes to a value such as `Last updated 10 seconds ago`;
8. failures keep the previous good data and expose the error.

Manual refresh must not launch an unbounded number of concurrent GitLab calls.

### 15.4 Browser self-refresh

The open browser page shall automatically poll the local FastAPI backend for lightweight status/data changes.

A target UI polling interval of roughly 15-30 seconds is appropriate for the prototype.

This does **not** mean GitLab is queried every 15-30 seconds. The browser only asks the local application whether cached data changed.

### 15.5 No direct GitLab calls from the browser

All GitLab communication occurs server-side.

---

## 16. Refresh State and Freshness UX

Freshness must always be explicit.

### 16.1 Suggested state model

The UI should distinguish at least:

- **Fresh** — latest scheduled/manual synchronization succeeded and is within the expected interval;
- **Refreshing** — synchronization is currently in progress;
- **Stale** — last known good data is older than the expected refresh interval;
- **Error** — the most recent attempt failed;
- **Never synced** — no successful snapshot exists yet.

A refresh failure may result in both stale data and an error condition. The UI may combine those visually but should preserve the meaning.

### 16.2 Footer/status area

A persistent footer or status bar shall show at least:

- last successful selected-user refresh;
- last attempted refresh;
- relative age, e.g. `10 seconds ago`, `12 minutes ago`;
- current crawler state: idle/refreshing/error;
- unresolved backend error count or diagnostic indicator.

### 16.3 Staleness marker

When data has exceeded the expected ten-minute refresh interval and no newer successful snapshot exists, the UI should make this obvious.

A warning icon such as a red or high-contrast exclamation marker is appropriate, but color must not be the only signal.

The marker should provide a tooltip or nearby text explaining the condition.

### 16.4 Per-user freshness

Because partial failures are possible, each selected user section should also carry its own last successful refresh timestamp when useful.

One user's failure must not make all other users look stale if their data refreshed successfully.

---

## 17. Failure and Last-Known-Good Behavior

This is a critical invariant.

### 17.1 Last-known-good rule

**If a refresh cannot retrieve valid new data, the application shall keep the most recent successful data even if that data is older than the normal retention window.**

The retention cleanup process must never cause the dashboard to become blank solely because GitLab has been unreachable for more than one day.

### 17.2 Atomic refresh semantics

Where practical, synchronization should stage or transactionally write a coherent result.

A failed or partially invalid response must not replace a complete prior snapshot with incomplete data.

### 17.3 Partial success

If one data category succeeds and another fails, record category-specific status where feasible.

For example:

- assignments refreshed successfully;
- activity failed;
- timelogs retained from the previous snapshot.

The frontend should show the most useful available information and expose the partial failure.

---

## 18. Rolling Retention and Self-Cleaning Database

The database is not intended as a permanent analytics warehouse.

### 18.1 Default retention

Superseded event/snapshot data should normally be retained for approximately one day unless a slightly longer window is required to compute the seven-day dashboard efficiently.

Because the dashboard itself needs seven days of activity/timelog context, the database may retain the minimal normalized data required for those seven-day calculations while avoiding redundant historical snapshots.

The distinction is:

- **snapshot/version history:** short-lived, approximately one day;
- **source facts required for the seven-day view:** retain only as long as necessary for that seven-day view plus a small synchronization overlap;
- **latest known-good record:** retain until a newer known-good record exists, regardless of age.

### 18.2 Cleanup

A periodic cleanup task should:

- delete superseded snapshots outside retention;
- delete event/timelog facts no longer needed for the visible window;
- remove obsolete error records according to a modest retention policy;
- never delete selection state;
- never delete the only known-good snapshot for a selected user;
- keep SQLite compact.

### 18.3 Cleanup after failure

Cleanup should be conservative after synchronization failures.

A good rule is to purge replaceable data only after the relevant newer synchronization has succeeded.

---

## 19. Startup and Restart Behavior

On startup the application shall:

1. open the existing SQLite database if present;
2. apply database migrations;
3. load global selection state;
4. make cached dashboard data available immediately;
5. start the background synchronization scheduler;
6. determine which datasets are stale;
7. asynchronously refresh stale data;
8. continue serving the previous cache while the refresh is running.

A restart must not force a full crawl from zero.

A restart must not erase:

- selected users;
- last known good work data;
- recent activity required for the current view;
- timelog data required for the current view;
- synchronization metadata;
- unresolved diagnostics.

---

## 20. GitLab Load-Minimization Strategy

The application should deliberately minimize requests to the on-premise GitLab server.

### 20.1 Broad but cheap

The user directory is refreshed globally, but only at the lower hourly frequency.

### 20.2 Narrow but richer

Detailed crawling occurs only for globally selected users.

### 20.3 On-demand project metadata

Do not pre-crawl every project merely to populate a project table.

When selected-user data references a project/group not yet known locally, resolve only the metadata required to render that work item/activity.

### 20.4 Incremental synchronization

Use timestamps, watermarks, identifiers, conditional behavior, or other API-supported strategies to avoid repeatedly downloading unchanged history.

### 20.5 Bounded concurrency

GitLab calls must use a modest configurable concurrency limit.

Do not fan out hundreds of simultaneous requests when multiple selected users are refreshed.

### 20.6 Retry discipline

Transient failures may use bounded exponential backoff with jitter.

Authentication/authorization failures should fail fast and produce a clear diagnostic rather than repeatedly retrying.

---

## 21. High-Level Architecture

The prototype consists of four logical components within one deployable Python application.

### 21.1 GitLab integration layer

Responsibilities:

- authentication;
- REST API access;
- GraphQL where required for data not suitably available through REST;
- pagination;
- timeouts;
- retries;
- normalization;
- version/license capability handling;
- clear exceptions.

### 21.2 Synchronization service

Responsibilities:

- scheduled user refresh;
- scheduled selected-user refresh;
- manual refresh;
- incremental fetch strategy;
- preprocessing;
- atomic/local persistence;
- cleanup;
- error recording.

### 21.3 SQLite persistence layer

Responsibilities:

- cache;
- global selected-user state;
- synchronization metadata;
- error history;
- migration state;
- compact seven-day facts required by the dashboard.

### 21.4 FastAPI web application

Responsibilities:

- serve the browser assets;
- expose internal JSON endpoints;
- read dashboard data from SQLite;
- expose refresh controls;
- expose health/status/diagnostics;
- never expose the GitLab token.

### 21.5 Deployment process model

Because the prototype runs an in-process scheduler and writes to one SQLite database, deployment should use a **single application worker** initially.

Local development can run with Uvicorn.

Container/server deployment can run FastAPI through Gunicorn with a suitable ASGI worker, configured as one worker for the prototype.

This avoids duplicate schedulers and duplicate crawls. Horizontal/multi-worker scaling can be designed later if needed.

---

## 22. Frontend Technology Constraints

The product is Python-first, but a browser necessarily uses HTML/CSS and a small amount of client-side JavaScript.

The prototype should avoid a separate Node.js toolchain.

Recommended constraints:

- HTML served by FastAPI;
- CSS bundled with the package;
- small plain JavaScript modules for polling, filtering, sorting, theme, and interactions;
- charts rendered with a small locally bundled chart library or lightweight native SVG/canvas implementation;
- no CDN dependency required at runtime;
- all static assets deploy with the Python package/container.

The exact browser library is an implementation decision as long as these constraints remain true.

---

## 23. UI / UX Design Requirements

The visual design is a product requirement, not cosmetic cleanup for later.

### 23.1 Overall character

The interface should feel like a polished engineering operations dashboard:

- modern;
- calm;
- clear hierarchy;
- compact but not cramped;
- strong use of whitespace where it improves grouping;
- visually consistent;
- not overloaded with ornamental effects.

### 23.2 Light and dark themes

Provide a visible light/dark toggle.

Persistence for the current browser session is sufficient. `sessionStorage` may be used; there is no requirement to persist theme choice across unrelated future browser sessions.

### 23.3 Typography and content

Use readable type sizes and predictable hierarchy.

Text-heavy data must remain easy to copy.

Avoid excessive truncation. When truncation is necessary, make the full value available by tooltip, expansion, or detail view.

### 23.4 Status semantics

Use consistent badges/icons for:

- work type;
- work state;
- fresh/stale/error/refreshing status;
- user account type where available.

Never rely on color alone for critical meaning.

### 23.5 Loading behavior

Do not blank the dashboard during refresh.

Use a subtle spinner/progress indicator and explicit `Refreshing…` state while leaving cached content visible.

### 23.6 Responsive behavior

The primary target is a desktop/laptop browser, but the layout should remain functional at narrower widths.

Cards may stack vertically and work tables may allow horizontal scrolling when required.

### 23.7 Accessibility baseline

The prototype should include:

- semantic headings;
- sufficient contrast;
- keyboard-accessible controls;
- visible focus states;
- labels/tooltips for icons;
- no color-only error signaling.

---

## 24. Backend HTTP API

Indicative FastAPI endpoints:

- `GET /api/health`
- `GET /api/status`
- `GET /api/users`
- `PATCH /api/users/{id}/selection`
- `GET /api/dashboard`
- `GET /api/users/{id}/work`
- `GET /api/users/{id}/activity`
- `GET /api/users/{id}/time-summary`
- `GET /api/errors`
- `POST /api/refresh`

### 24.1 Refresh endpoint behavior

`POST /api/refresh` should return quickly after scheduling/coalescing the refresh rather than holding the HTTP connection for the full GitLab crawl.

The client can observe progress through `/api/status` or dashboard polling.

### 24.2 Selection endpoint behavior

Changing selection updates SQLite immediately.

Newly selected users should become eligible for an immediate/near-immediate detail synchronization rather than waiting almost a full ten-minute interval.

### 24.3 Read endpoints

Read endpoints should return cached data and freshness metadata together so the frontend cannot accidentally render stale content as if it were current.

---

## 25. Suggested Local Data Model

The final schema may evolve, but the following model is appropriate for the prototype.

### `users`

- local primary key;
- GitLab user ID;
- username;
- display name;
- avatar URL;
- account creation timestamp;
- account state;
- account type/bot metadata where available;
- selected flag;
- first seen timestamp;
- last metadata refresh timestamp.

### `projects`

- GitLab project ID;
- path with namespace;
- display name;
- web URL;
- last refreshed timestamp.

### `work_items`

- local primary key;
- GitLab object identity;
- object type;
- project/group reference;
- title;
- state;
- labels;
- milestone;
- priority if available;
- due date;
- updated timestamp;
- web URL;
- cache refreshed timestamp.

### `work_item_assignees`

- work-item reference;
- user reference;
- observed timestamp.

This supports multiple assignees cleanly.

### `activity_events`

- stable GitLab event ID or synthetic stable key;
- user reference;
- project reference;
- event type;
- action name;
- human-readable summary;
- event timestamp;
- web URL;
- minimal raw metadata JSON where useful;
- fetched timestamp.

### `timelogs`

- stable GitLab timelog identity where available;
- user reference;
- project/work-item reference;
- spent-at timestamp;
- duration seconds;
- summary/reference fields;
- fetched timestamp.

### `sync_state`

- synchronization category;
- user reference where applicable;
- last attempt timestamp;
- last success timestamp;
- next due timestamp;
- cursor/watermark data;
- status;
- run identifier.

### `sync_runs`

- run ID;
- trigger source: scheduled/manual/startup;
- started timestamp;
- completed timestamp;
- status;
- users attempted;
- users succeeded;
- concise summary.

### `errors`

- primary key;
- occurred timestamp;
- severity;
- subsystem/operation;
- related user/project if applicable;
- human-readable message;
- optional technical details;
- resolved timestamp.

### Schema versioning

Use Alembic's version table and committed migration scripts. Do not maintain ad-hoc schema integers when Alembic already provides migration history.

---

## 26. SQLite Requirements

SQLite is both the prototype cache and application-state store.

Requirements:

- schema-versioned with Alembic;
- survives restarts;
- database path configurable;
- default location outside the source tree;
- persistent volume mount in Docker;
- appropriate indexes on GitLab IDs, user IDs, project IDs, state, and timestamps;
- foreign keys enabled;
- transactions used for coherent refresh writes;
- cleanup prevents unbounded growth;
- migrations never require deleting the database as normal upgrade behavior.

The application should use a database access layer such as SQLAlchemy.

---

## 27. GitLab Integration Requirements

The rest of the codebase must not manipulate raw HTTP payloads directly.

Provide an explicit client/service abstraction with conceptual operations such as:

- `list_users()`;
- `get_user_work(user_id, ...)`;
- `get_user_recent_activity(user_id, ...)`;
- `get_user_timelogs(user_id, start, end)`;
- `get_project(project_id)`;
- capability checks for optional GitLab object types.

The client shall handle:

- base URL configuration;
- authentication;
- pagination;
- request timeouts;
- bounded retries;
- rate limiting responses;
- authorization failures;
- invalid/unexpected payloads;
- relevant GitLab version differences;
- API capability differences;
- test fixtures/mocks.

### 27.1 REST and GraphQL

Use GitLab REST API where it provides the needed data cleanly.

GraphQL may be used when it materially reduces requests or provides the required date-bounded data more correctly, especially for timelog-style queries or newer work-item models.

The chosen integration should be hidden behind the same Python abstraction.

### 27.2 Account completeness

The product requirement is to display all accounts the installation is expected to expose.

If the configured credentials do not have sufficient rights to retrieve the full instance user population, the application should surface a diagnostic rather than silently implying the list is complete.

---

## 28. Configuration and Secrets

Configuration shall not require source-code changes.

At minimum support:

- GitLab base URL;
- GitLab API token/secret source;
- SQLite path;
- hourly user refresh interval;
- ten-minute selected-user refresh interval;
- UI polling interval;
- concurrency limit;
- retention/cleanup settings;
- bind host;
- port;
- log level;
- TLS verification/custom CA settings where needed for on-premise GitLab.

### 28.1 Local development

Environment variables and an optional ignored `.env` file are acceptable.

### 28.2 Docker

Docker secrets are preferred for the GitLab credential.

The application should support reading the secret from a mounted secret file path, with environment-variable fallback for local/simple deployments.

### 28.3 Credential rules

The GitLab token must never:

- be committed to Git;
- be stored in the SQLite database;
- be rendered in the browser;
- appear in logs;
- be included in exception text returned to the frontend.

Use read-only/minimum practical GitLab permissions consistent with the required instance-wide visibility.

---

## 29. Error Handling and Diagnostics

### 29.1 stdout/stderr logging

Backend logs must be human-readable and useful during immediate debugging.

Each useful log event should contain:

- timestamp;
- severity;
- subsystem;
- operation;
- related user/project identifier where useful;
- concise message;
- duration for major sync operations;
- retry count where relevant.

Machine-readable structured logging may be added, but readability in `docker logs` remains important.

### 29.2 Persisted errors

Important crawler/backend errors should also be persisted in SQLite so the frontend can show them after the failing request finishes.

Examples:

- authentication failure;
- GitLab unreachable;
- timeout;
- rate limiting;
- authorization failure;
- malformed API response;
- database exception;
- migration failure;
- partial selected-user refresh;
- cleanup failure.

### 29.3 Frontend diagnostics

Provide an operator-friendly diagnostics area such as a drawer or panel.

It should show:

- timestamp;
- subsystem;
- affected user/project where relevant;
- human-readable problem;
- whether cached data is still being shown;
- last successful synchronization related to the problem.

Technical stack traces remain in backend logs, not dumped directly into the normal dashboard UI.

---

## 30. Health and Status

### `/api/health`

Should report enough information for process/container monitoring:

- application alive;
- SQLite reachable;
- schema migrated;
- GitLab configuration present.

A GitLab outage does not necessarily mean the web process is unhealthy; distinguish service health from upstream synchronization status.

### `/api/status`

Should expose non-secret operational state such as:

- crawler idle/running;
- active refresh run ID;
- refresh trigger source;
- last successful user-directory refresh;
- last attempted user-directory refresh;
- last successful selected-user refresh;
- last attempted selected-user refresh;
- next scheduled refresh;
- known user count;
- selected user count;
- unresolved error count;
- GitLab upstream status summary;
- data freshness classification.

---

## 31. Performance Requirements

### 31.1 Cached dashboard response

The main dashboard should render from local cached data in well under one second under normal internal deployment conditions.

### 31.2 No network blocking on page render

Opening the page must not synchronously crawl GitLab before useful content is shown.

### 31.3 Bounded fan-out

Refreshing five selected users may perform parallel work, but concurrency must remain bounded and configurable.

### 31.4 Efficient database access

Avoid N+1 queries in dashboard assembly.

Use indexes and joined/batched queries appropriate for the SQLite dataset.

### 31.5 Directory scale

Filtering and sorting the user list should remain responsive with a substantially larger directory than the normal selected set.

### 31.6 Frontend polling

UI polling must use compact responses or change/version metadata so dozens of open browser tabs do not create meaningful load.

---

## 32. Security Model

The prototype is intentionally simple but must protect credentials and upstream data responsibly.

Requirements:

- internal-network-only deployment by default;
- no application login required in the prototype;
- reverse-proxy authentication can be added later without redesigning the app;
- token never exposed client-side;
- GitLab-originated text escaped/sanitized before HTML rendering;
- external links restricted to valid configured GitLab URLs where practical;
- state-changing internal endpoints use appropriate HTTP methods;
- manual refresh endpoint includes basic protection against accidental request storms;
- CORS disabled/restricted unless explicitly needed;
- container runs as a non-root user where practical;
- SQLite volume permissions restricted to the application account.

---

## 33. Packaging and Deployment

The prototype should support both developer-friendly package execution and container deployment.

### 33.1 Python package

Use `pyproject.toml` and expose a CLI entry point such as:

```text
gitlab-teamboard
```

Potential commands:

```text
gitlab-teamboard serve
gitlab-teamboard migrate
gitlab-teamboard refresh
gitlab-teamboard doctor
```

The package should be installable with `pipx` for simple internal/local execution.

### 33.2 Docker image

The Docker image shall:

- contain the Python application and static frontend assets;
- expose the HTTP port;
- run a single application worker for the prototype;
- log to stdout/stderr;
- read configuration externally;
- support Docker secrets;
- mount SQLite through a persistent volume;
- run migrations during an explicit startup step or documented command;
- avoid embedding credentials.

### 33.3 No separate frontend deployment

The browser UI ships with the backend package/container.

---

## 34. Development Tooling

Recommended baseline:

- Python 3.12+;
- `pyproject.toml`;
- `uv` for dependency/environment management;
- FastAPI;
- SQLAlchemy;
- Alembic;
- HTTPX;
- Pydantic Settings;
- pytest;
- pytest-asyncio where useful;
- Ruff;
- mypy or pyright;
- pre-commit;
- Gunicorn for container/server execution;
- Uvicorn for local ASGI execution;
- Docker/BuildKit.

A `Makefile`, `justfile`, or equivalent command wrapper should provide a stable developer interface:

- `install`;
- `run`;
- `test`;
- `lint`;
- `format`;
- `typecheck`;
- `migrate`;
- `refresh`;
- `build`;
- `docker-run`.

---

## 35. Git and Commit Discipline

Development shall follow:

- atomic commits;
- Conventional Commits;
- no secrets in history;
- migrations committed together with the model/code change that requires them;
- tests committed with behavior changes;
- generated runtime database files excluded from Git.

Examples:

```text
feat(sync): add selected-user activity refresh
feat(ui): add stale-data warning state
fix(cache): preserve last good snapshot after failed refresh
chore(db): add timelog indexes
```

---

## 36. CI

### 36.1 GitLab CI

GitLab CI is the primary pipeline for the repository.

The initial pipeline should include:

1. dependency/install validation;
2. Ruff lint/format check;
3. type check;
4. unit tests;
5. integration tests using mocked GitLab responses;
6. migration test from an empty database;
7. migration upgrade test from representative prior schema versions once they exist;
8. Python package build;
9. Docker image build where the runner environment permits.

### 36.2 GitHub Actions

A GitHub Actions workflow may mirror the same validation if the repository is mirrored to GitHub.

It should call the same underlying project commands rather than duplicate logic.

---

## 37. Testing Strategy

### 37.1 Unit tests

Cover:

- GitLab response normalization;
- pagination;
- sorting/filter helpers;
- freshness classification;
- retention decisions;
- last-known-good logic;
- timelog aggregation;
- configuration/secrets resolution.

### 37.2 Integration tests

Use recorded/synthetic GitLab fixtures to test:

- user sync;
- selected-user sync;
- partial failure;
- manual refresh;
- retry behavior;
- database writes;
- cleanup;
- API responses.

### 37.3 Critical resilience test

A required test scenario:

1. perform a successful refresh;
2. advance beyond normal retention age;
3. make GitLab unavailable;
4. run cleanup and refresh;
5. verify the last successful dashboard data still exists;
6. verify the UI/API marks it stale/error.

### 37.4 UI/browser tests

At minimum cover the critical flow with a lightweight browser automation test if practical:

- filter users;
- select users;
- clear filter;
- selection remains;
- open dashboard;
- trigger manual refresh;
- observe refreshing state;
- switch theme;
- verify stale/error status can be surfaced.

---

## 38. Functional Requirements Catalogue

### Users

- **FR-U01** Retrieve all users visible to configured GitLab credentials.
- **FR-U02** Do not exclude bots/service/blocked/inactive users by default.
- **FR-U03** Filter users live by name and username.
- **FR-U04** Preserve selection while filtering.
- **FR-U05** Persist global selection in SQLite.
- **FR-U06** Make selection changes visible to all browser clients.
- **FR-U07** Sort users by creation date and alphabetical fields.

### Synchronization

- **FR-S01** Refresh user directory approximately hourly.
- **FR-S02** Refresh selected-user details approximately every ten minutes.
- **FR-S03** Trigger selected-user refresh manually.
- **FR-S04** Report refresh-in-progress state.
- **FR-S05** Prevent/coalesce overlapping refresh storms.
- **FR-S06** Resume from persisted sync state after restart.
- **FR-S07** Minimize GitLab calls with incremental/detail-on-selection behavior.

### Work

- **FR-W01** Show assigned/recently relevant issues.
- **FR-W02** Show assigned/recently relevant merge requests.
- **FR-W03** Support additional assignable GitLab work types where API capabilities allow.
- **FR-W04** Include closed/resolved/completed states.
- **FR-W05** Default sort by updated time descending.
- **FR-W06** Support user-selectable ascending/descending and alphabetical sorts.
- **FR-W07** Link back to GitLab.

### Activity

- **FR-A01** Show five most recent actions immediately.
- **FR-A02** Make twelve most recent actions available.
- **FR-A03** Show seven days of activity trend.
- **FR-A04** Distinguish useful action categories when reliable.

### Time

- **FR-T01** Use actual GitLab timelogs only.
- **FR-T02** Show seven-day total per selected user.
- **FR-T03** Show daily time breakdown.
- **FR-T04** Keep source references where available.
- **FR-T05** Do not estimate or fabricate missing hours.

### Freshness and resilience

- **FR-F01** Show exact last successful refresh timestamp.
- **FR-F02** Show relative data age.
- **FR-F03** Show stale state clearly.
- **FR-F04** Show error state clearly.
- **FR-F05** Preserve last known good data after failed refresh.
- **FR-F06** Do not purge the only good snapshot due to retention age.
- **FR-F07** Auto-update the browser when local cached data changes.

### UI

- **FR-UI01** Provide People and Dashboard views.
- **FR-UI02** Support light and dark themes.
- **FR-UI03** Theme persistence only needs to last for the browser session.
- **FR-UI04** Keep dashboard data text selectable/copyable.
- **FR-UI05** Provide polished loading, empty, stale, and error states.
- **FR-UI06** Provide charts with useful tooltips.

### Diagnostics

- **FR-D01** Log human-readable messages to stdout/stderr.
- **FR-D02** Persist important sync errors.
- **FR-D03** Show recent errors in the frontend.
- **FR-D04** Never log or display credentials.

---

## 39. Non-Functional Requirements

- **NFR-01 Performance:** cached dashboard responses should normally complete well under one second.
- **NFR-02 Upstream protection:** bounded concurrency and sensible retry behavior are mandatory.
- **NFR-03 Durability:** restart must preserve selections and last known good data.
- **NFR-04 Maintainability:** API integration, sync logic, persistence, and web layers remain modular.
- **NFR-05 Testability:** live GitLab access is not required for normal unit/integration tests.
- **NFR-06 Observability:** operators can distinguish application health, upstream failure, staleness, and crawler progress.
- **NFR-07 Portability:** local Python/pipx and Docker execution are both supported.
- **NFR-08 Security:** secrets stay out of code, logs, browser responses, and SQLite.
- **NFR-09 UX quality:** the dashboard must be visually credible enough for routine daily use.
- **NFR-10 Data minimization:** the local database should contain only the short-term operational data required by the dashboard.

---

## 40. Prototype Acceptance Criteria

The first prototype is successful when all of the following work end-to-end.

1. Application starts from a clean checkout using documented commands.
2. Application connects to configurable self-managed GitLab credentials.
3. SQLite is created/migrated automatically or through one documented command.
4. Existing SQLite state is reused after restart.
5. User directory is fetched and stored.
6. Bots/service/blocked/inactive accounts are not silently excluded.
7. Directory refreshes approximately hourly.
8. User list can be filtered while typing.
9. Selection made under a filter survives clearing the filter.
10. Selections are global and survive restart.
11. Newly selected users are synchronized without waiting an excessive interval.
12. Selected users refresh approximately every ten minutes.
13. Manual **Refresh now** triggers an immediate attempt.
14. UI visibly indicates `Refreshing` while the attempt runs.
15. Dashboard continues showing old data during refresh.
16. Successful refresh updates visible content without full browser reload.
17. Footer shows exact last-success timestamp and relative age.
18. Stale data is clearly marked.
19. Failed refresh leaves the last known good data visible.
20. Cleanup does not delete the only known-good snapshot during GitLab outage.
21. Work list includes relevant closed/resolved/completed records and is not open-state-only.
22. Work list defaults to last-updated descending.
23. Work list supports ascending/descending and alphabetical sorting.
24. Dashboard shows the five newest actions immediately.
25. Dashboard makes twelve newest actions available.
26. Dashboard shows a seven-day activity chart.
27. Dashboard shows factual seven-day GitLab timelog totals.
28. Timelog totals are based on actual entered time, not inference.
29. Important text can be selected and copied using normal browser behavior.
30. Light and dark mode both work and remain usable.
31. Backend logs readable diagnostics to stdout/stderr.
32. Recent backend/crawler errors can be inspected from the UI.
33. Token values never appear in logs or browser API responses.
34. GitLab detail crawling is limited primarily to selected users.
35. The page renders useful cached data without waiting for a live GitLab round trip.
36. Unit/integration tests cover core synchronization, retention, persistence, and API behavior.
37. Local lint/test/type-check commands are documented.
38. GitLab CI runs the primary quality pipeline.
39. Python package can be built and installed.
40. Docker image can be built with persistent SQLite and secret injection.
41. The interface is polished enough for routine internal use.

---

## 41. Recommended Implementation Sequence

### Phase 1 — Application skeleton

- Python package;
- FastAPI;
- settings/configuration;
- SQLAlchemy;
- SQLite;
- Alembic;
- logging;
- health/status endpoints;
- minimal application shell.

### Phase 2 — User directory and selection

- GitLab client abstraction;
- user pagination;
- user persistence;
- hourly synchronization;
- People view;
- live filter;
- sorting;
- global selected-user state.

This phase already validates authentication, persistence, API access, and core UI direction.

### Phase 3 — Selected-user data

- work-item normalization;
- state-independent work retrieval;
- project metadata on demand;
- activity retrieval;
- twelve-event cache;
- timelog retrieval;
- seven-day aggregation;
- incremental sync state.

### Phase 4 — Dashboard UX

- user summary cards;
- five-action preview;
- twelve-action expansion;
- work sorting controls;
- activity chart;
- timelog visualization;
- freshness footer;
- manual refresh;
- auto-update;
- dark/light mode;
- empty/loading/stale/error states.

### Phase 5 — Resilience and cleanup

- last-known-good transaction semantics;
- stale state;
- partial failure handling;
- cleanup/retention;
- frontend diagnostics;
- retry/backoff;
- performance tuning.

### Phase 6 — Engineering quality and delivery

- unit/integration/browser tests;
- Ruff/type checks;
- pre-commit;
- GitLab CI;
- optional GitHub Actions mirror;
- `pipx` packaging;
- Docker image;
- Docker secrets;
- concise operator documentation.

---

## 42. Prototype Definition of Done

A demonstrable prototype is a single, well-structured Python application that can be pointed at the self-managed GitLab instance, stores state in a persistent versioned SQLite database, and serves a polished internal web dashboard.

A user can:

1. open the People view;
2. search the complete available GitLab user directory;
3. select several users, including bot/service accounts if desired;
4. open the Dashboard;
5. immediately see cached data;
6. see each selected user's recently relevant work regardless of open/closed state;
7. sort that work;
8. see five latest actions and expand to twelve;
9. see seven days of activity;
10. see seven days of actual entered GitLab time;
11. see exactly when the information was last refreshed;
12. trigger a manual refresh;
13. watch the refresh state change without losing existing data;
14. continue using the last known good data if GitLab becomes unavailable;
15. inspect visible diagnostics when something goes wrong;
16. switch between light and dark mode;
17. copy normal text directly from the page.

The crawler automatically resumes after restart, uses the existing cache instead of starting from zero, keeps the database compact, and minimizes requests by prioritizing selected users.

---

## 43. Deferred Product Questions

The following are intentionally deferred and are **not blockers** for implementation of the first prototype:

- Should users later be able to save named selection presets/teams?
- Should selections eventually become per-viewer rather than global?
- Should a project-centric secondary dashboard be added?
- Should expected work hours be configurable for optional timelog completeness warnings?
- Should activity/timelog history be retained longer for trend reporting?
- Should external notifications be introduced?
- Should SSO/reverse-proxy identity eventually personalize the dashboard?
- Should multiple GitLab instances be supported?
- Should operators be able to acknowledge/resolve errors from the UI?
- Should refresh intervals become configurable from the UI?

Implementation can begin without answering these questions.

---

## 44. Engineering Notes for the First Prototype

A few implementation constraints are worth treating as guardrails from day one:

1. **Single scheduler owner**  
   Do not run multiple background schedulers against the same SQLite database accidentally. Keep one web worker for the prototype.

2. **Cache before crawl**  
   All main UI reads come from SQLite. A page request does not become a GitLab crawl.

3. **Refresh transaction boundary**  
   New data should become the active known-good state only after the relevant synchronization completes successfully enough to be useful.

4. **Last good data outranks retention**  
   Retention rules may remove redundant history but never the only usable snapshot.

5. **Selection drives cost**  
   Selecting a user opts that account into the ten-minute detailed synchronization loop. Deselecting a user stops high-frequency crawling, while short-lived cached data can remain until ordinary cleanup.

6. **No silent incompleteness**  
   If token permissions or GitLab capabilities prevent a requirement from being fulfilled completely, expose that fact through diagnostics.

7. **No CDN dependency**  
   The internal tool should remain usable in an isolated environment as long as it can reach GitLab.

8. **UI quality is part of acceptance**  
   Functional correctness alone is not enough. A cluttered or visually weak interface is not considered a finished prototype.
