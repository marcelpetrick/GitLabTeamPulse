# Plan — steering signals, teams and team reports (v0.4.0)

**Status: done, released as v0.4.0 on 2026-10-15.**

Implements the ideas the stakeholder accepted from [`ideas20261015.md`](ideas20261015.md) on
2026-10-15. Branch: `feature/steering-signals`. Release: **v0.4.0** (major feature → minor bump).

| Idea | Decision | In this plan |
|---|---|---|
| 1 Review queue and stuck work (+ cross-team waits) | yes, with UI/UX that fits the dashboard; a week without update means something is wrong | A, D |
| 2 Due dates and milestone risk | yes, point them out | A, B, D |
| 3 Named teams | yes, stored on the server like the selection | C, E |
| 4 Planned vs. unplanned work | **not now** | — |
| 5 Estimate quality on work items | yes | A, D |
| 6 Team report (with weekly digest) | yes | B, C, E |
| 7 Knowledge concentration (bus factor) | yes | B, E |

## Constraint: GitLab Free

The instance runs the lowest license. Premium-only features are **not used**; the plan rebuilds the
needed signals from Free REST endpoints:

| Premium feature | Free workaround used here |
|---|---|
| Milestone burndown charts | Fetch the milestone's issues (`GET /projects/:id/milestones/:mid/issues`, `GET /groups/:id/milestones/:mid/issues`), compute progress, remaining estimate and a burn-up from `closed_at` ourselves |
| Issue weight, iterations | Time estimates (`time_stats`, Free) and milestones |
| Epics | Already optional (capability probe); nothing new depends on them |
| Blocking issue links | Not used; "can't merge" uses MR fields available on Free (`has_conflicts`, `detailed_merge_status`, `blocking_discussions_resolved`) |
| Contribution / productivity analytics | Own daily aggregates from the events, timelog and MR/issue APIs |

## A. Work data: keep what we fetch, see old open items

- `parse_work_item` keeps `merged_at`, `time_stats.time_estimate` / `total_time_spent`, all
  `reviewers`, `detailed_merge_status`, `has_conflicts`, `blocking_discussions_resolved` and the
  embedded milestone (id, iid, project/group id, title, state, start/due date, web URL).
- `get_user_work` additionally lists **open** items without the `updated_after` cutoff (issues as
  assignee, MRs as assignee, MRs as reviewer), so items untouched for more than 30 days are no
  longer invisible. +3 small requests per user per sync.
- Migration `0004`: new `work_items` columns; new `milestones` table.

## B. History (daily aggregates, not raw events)

Kept for the contribution-calendar window (53 weeks), trimmed like `contribution_days`:

- `activity_daily(user_id, day, project_id, metric, value)` — MR opened / merged (by merger) /
  approved / commented, issue opened / closed / commented. Built from the **same event stream** the
  calendar already reads; the calendar cursor gets a revision suffix so existing installs refetch
  the year once. No pushes or commit counts (gaming target).
- `timelog_daily(user_id, day, project_id, project_path, seconds)` — new hourly category
  `timelog_history`: one-off backfill of the window, then a 31-day re-sweep (late/edited entries).
- `outcomes(user_id, kind, gitlab_id, project_id, iid, reference, title, web_url, created_at,
  finished_at, time_estimate, time_spent)` — new hourly category `outcomes`: merged MRs by author,
  closed issues by (current) assignee; incremental by `updated_after`; reopened issues are removed.
- `milestones` stats — new global hourly category `milestones`: for up to 12 active milestones that
  monitored people's open items belong to, fetch the milestone's issues and store totals, closed,
  estimate, spent, open without estimate, open overdue, open per assignee and the closed dates
  (burn-up).
- Team members who are not selected are **tracked**: only the hourly history categories run for
  them (cap `TEAMPULSE_MAX_TRACKED_USERS`, default 30). Retention keeps their history, purges their
  short-lived cache, and purges everything once they are neither selected nor in a team.

## C. Teams

- Tables `teams`, `team_members`, `team_scopes(team_id, path)` (project or namespace path prefix).
- Shared and stored in SQLite like the selection; `teams_version` counter for polling;
  `TEAMPULSE_TEAMS_READONLY=true` locks editing.
- API: `GET/POST /api/teams`, `PUT/DELETE /api/teams/{id}`, `POST /api/teams/{id}/apply`
  (replace the dashboard selection with the members), `GET /api/teams/{id}/report?period=&offset=`,
  `GET /api/teams/{id}/digest`.
- Report: period week / month / quarter with ◀ ▶; team totals with deltas against the same
  elapsed span of the previous period (fair for a running period): hours, MRs merged, issues closed,
  MRs opened, approvals, review comments, MR cycle time (median, IQR), estimate vs spent with
  coverage; share of effort per project (hours, MRs, issues); knowledge spread per project over 12
  weeks (bus factor, flag at one person); history completeness. Person breakdown only when
  `TEAMPULSE_TEAM_PERSON_BREAKDOWN=true` (alphabetical, facts, no deltas, not sortable).
- Digest: Markdown for the last completed calendar week (totals, hours per project, merged MRs,
  closed issues), copied with one click.

## D. Dashboard UI

- **Needs attention** strip between the header and the cards, across all monitored people,
  deduplicated per item. Summary tiles that filter the list:
  - **Stalled** — open items without any update for ≥ `TEAMPULSE_STALLED_DAYS` (7): shown as an
    error ("something is wrong"), with days since the last update;
  - **Overdue** (error) and **Due soon** within `TEAMPULSE_DUE_SOON_DAYS` (7) (warning);
  - **Waiting for review** — open, non-draft MRs with a monitored reviewer, age and inactivity;
  - **Can't merge** — conflicts, rebase needed, pipeline must pass, unresolved threads,
    requested changes;
  - **No reviewer in the team** — open MRs of monitored people with no reviewer or only reviewers
    outside the monitored set.
  "All clear" state when nothing needs attention.
- **Milestones** panel: active milestones of monitored people's open items — due date with days
  left/overdue, progress bar, remaining estimate, open without estimate, burn-up sparkline, and
  how many open items belong to monitored people.
- Cards: red "stalled" / "overdue" chips in the head; stalled rows highlighted in the work table;
  `spent / estimate` chip with overrun marker; filter "Open without estimate".
- Header: "Show team…" select to apply a team to the shared selection.

## E. Teams UI

New **Teams** tab: team list, team header (members, scope, *Show on dashboard*, *Edit*, *Delete*),
period controls, KPI tiles with deltas, share of effort table, knowledge spread, cycle time and
estimate tiles, optional people table, *Copy last week as Markdown*, "How these numbers are counted".
Editor dialog: name, description, member picker with filter, project/namespace scope with
suggestions from known projects.

## F. Fake GitLab, tests, docs

- Fake GitLab: state/author filters, time stats, reviewers, merge status, milestones with ids and
  dates, milestone issue endpoints, stalled and overdue demo items; demo enables the person breakdown
  and creates a sample team.
- Tests: parsers, client, sync (history categories, cursor revision, tracked team members),
  retention, teams store/API/report/digest, attention and milestones read model, browser E2E for the
  attention strip and the Teams view; keep the 95 % coverage gate green.
- Docs: README (features, configuration), ARCHITECTURE (tables, categories, load), VISION
  amendment (§4 aggregates, §43 answers), CHANGELOG, regenerated screenshots incl. new ones.

## G. Delivery

1. Implement A–F in atomic conventional commits on the branch.
2. Full `./localPipeline.sh` green.
3. `/reviewBranch` against `master`; fix every confirmed finding; pipeline again.
4. Bump to 0.4.0, changelog, merge to `master`, tag `v0.4.0`, push, GitHub release with wheel and
   sdist, wait for CI.
5. Run it and show the result in Firefox.

## Out of scope (this release)

Planned vs. unplanned work (idea 4), releases shipped per scope, team-vs-team comparison,
per-person timelog hygiene, locally entered absences.

---

# Plan — v0.4.1: diagnostics fix, About dialog, showcase

**Status: done, released as v0.4.1 on 2026-10-15.**

Requested 2026-10-15 after the v0.4.0 release. Branch: `feature/about-and-showcase`.

1. **Stale diagnostics** (done in `c4d9fe7`): open problems of people who are neither selected
   nor in a team are resolved by the hourly cleanup; before, a deselected account's errors (e.g.
   `docker_imagecheck`, events 404) stayed in Diagnostics forever.
2. **About dialog**: an *About* button in the top bar opens a small dialog with name, version,
   author and contact, license (GPL-3.0-or-later), repository link, Python version, the runtime
   dependencies with their versions and licenses (read from the installed package metadata via
   `GET /api/about`), and the note that the project is generated with AI.
3. **Screenshots**: regenerate all README screenshots and add the new features: milestones,
   the team editor and the About dialog.
4. **README showcase**: a "New in 0.4" section with the attention strip, milestones, team report
   and team editor.
5. **"Reviewer outside" was misleading** (reported for colleagues such as @ERamirez): the signal
   only means "no reviewer among the people monitored on this dashboard", not "outside the
   company" — GitLab gives no reliable affiliation without admin e-mail access. Rename it to
   *Reviewer not monitored*, explain it in the tooltip, and show reviewers with their full name
   from the user directory.
6. **Versions**: bump the app to 0.4.1 and re-check every dependency against its latest stable
   release.
7. Tests (API and browser), full pipeline, `/reviewBranch`, fixes, changelog, v0.4.1 release,
   restart the dashboard and show it in Firefox.

---

# Plan — v0.4.2: reviewable alerts, one diagnostics entry

**Status: done, released as v0.4.2 on 2026-10-15.**

Requested 2026-10-15. Branch: `feature/clickable-alerts`.

1. **Stalled / overdue chips open the affected items.** Clicking the red chip on a person's card
   filters that card's work table to exactly those items (all rows, not only the first ten),
   scrolls it into view and focuses it; each row links to GitLab. "Stalled" and "Overdue" are
   also regular work-table filters. The table's overdue marker uses the dashboard's local day,
   like the server's counts (it used the browser's UTC date before).
2. **One diagnostics entry instead of two.** The warning icon with a counter in the top bar and
   the "No problems" link in the status bar opened the same drawer. Keep the status-bar entry
   (text and colour, next to the freshness facts), labelled "Diagnostics · …"; remove the
   top-bar icon. The outage banner keeps its contextual "View diagnostics" button.
3. **Profiling**: measure startup, a full sync run (real instance log and fake GitLab with
   simulated latency) and the API reads on a copy of the real database; find what is slow or
   could run in parallel; fix the worthwhile items; document the numbers in
   `docs/PERFORMANCE.md`.
4. **Bug audit**: a read-only audit of the whole code base (not only recent diffs) for real,
   confirmed bugs; fix them, with tests.
5. Browser tests, screenshots and docs updated; full pipeline; `/reviewBranch`; release v0.4.2;
   CI green; dashboard restarted and shown in Firefox.

## Follow-ups (not scheduled)

- **Decision 2026-10-15: stay on Python 3.14** (`.python-version`, Docker image, CI) for a
  while; do not move to Python 3.15 until the user decides otherwise.

---

# Plan — v0.4.3: tooltips everywhere

**Status: done, released as v0.4.3 on 2026-10-15.**

Requested 2026-10-15: every element should explain itself on hover, so users can find out what
something means without documentation. Branch: `feature/tooltips`.

1. **Inventory**: a browser script lists every visible control (buttons, selects, inputs,
   links, checkboxes) and every information element (stats, pills, badges, chips, KPI tiles,
   attention tiles and reasons, milestone parts, table headers, status-bar items) without a
   tooltip, in all views (Dashboard, Teams, People) and dialogs (Diagnostics, About, team
   editor).
2. **Tooltips**: add a native `title` with a short explanation of what the element is, how it
   is counted and what clicking does; links say where they go (GitLab, e-mail). Charts and the
   calendar keep their richer custom tooltips.
3. **Guard**: the inventory becomes a browser test that fails when a visible element has no
   tooltip, so new UI cannot regress.
4. Pipeline, `/reviewBranch`, release v0.4.3, CI green, dashboard restarted.

---

# Plan — v0.4.4: lighter epic sync

**Status: done, released as v0.4.4 on 2026-10-15.**

The follow-up from the v0.4.2 review. Epics are fetched with two small GraphQL queries per
top-level group (recently updated, and open ones however old) instead of every epic ever
assigned. The `state` filter is used when the instance accepts it; otherwise the sync falls back
to local filtering once and stays there. Only Premium instances have epics; GitLab Free is not
affected.
