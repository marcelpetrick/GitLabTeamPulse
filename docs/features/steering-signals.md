# Steering signals, teams and team reports

Added in v0.4.0 for the head of department as the main stakeholder: see what is stuck, late or
waiting across the monitored people, follow milestones, and look at named teams over weeks,
months and quarters. Background: [`ideas20261015.md`](../../ideas20261015.md) and
[`plan.md`](../../plan.md).

Everything works on **GitLab Free**. Premium-only APIs are not used; where Premium has a
feature (milestone burndown, contribution analytics), Team Pulse computes the same facts from
Free REST and GraphQL endpoints.

## Needs attention (dashboard)

One strip above the cards, across all monitored people, one row per item even when several
people share it. The tiles count and filter:

| Signal | Rule | Severity |
| --- | --- | --- |
| Stalled | Open item assigned to a monitored person, no update for `TEAMPULSE_STALLED_DAYS` (7) days | error: something is probably wrong |
| Overdue | Open assigned item past its due date | error |
| Due soon | Open assigned item due within `TEAMPULSE_DUE_SOON_DAYS` (7) days | warning |
| Waiting for review | Open, non-draft MR with a monitored reviewer (age and idle days) | warning after 2 days |
| Can't merge | `has_conflicts`, or `detailed_merge_status` conflict, need_rebase, ci_must_pass, discussions_not_resolved, requested_changes, blocked_status, or unresolved blocking threads | warning |
| No reviewer | Open, non-draft MR of a monitored person without any reviewer | warning |
| Reviewer not monitored | Open, non-draft MR whose reviewers are all people who are not monitored on this dashboard. These are usually colleagues, not people outside the company; GitLab offers no reliable affiliation. Useful to see review dependencies on others | info |

Open items are listed **regardless of age**: besides the work updated in the last
`TEAMPULSE_WORK_WINDOW_DAYS`, every sync also lists all open issues and MRs of each person
(three small extra requests), so stalled work cannot fall out of view.

The cards show red *stalled* and *overdue* chips, stalled rows are marked in the work table,
and each item shows `spent / estimate` from GitLab time tracking (overruns highlighted). The
**No estimate** filter lists open issues without an estimate.

## Milestones

Active milestones that monitored people's open items belong to, soonest due first (at most
`TEAMPULSE_MILESTONES_MAX`, default 12). Each tile shows the due date, progress, remaining
estimate, open issues without estimate or past their due date, and a burn-up line against the
straight line from start to due date.

GitLab Free has no burndown API, so once an hour Team Pulse reads the milestone's issues
(`GET /projects/:id/milestones/:milestone_id/issues` or the group equivalent) and computes these
numbers itself. A milestone that cannot be read keeps its last numbers and shows up in
Diagnostics.

## Teams

Named groups of people with an optional project scope (a project path or a namespace, which
includes everything below it). Teams are stored in SQLite and shared, like the selection.
`TEAMPULSE_TEAMS_READONLY=true` locks editing.

- **Show on dashboard** (Teams view or the *Show team…* selector) replaces the shared
  selection with the team's members.
- Team members do **not** have to be on the dashboard. Their history is refreshed hourly
  (`TEAMPULSE_MAX_TRACKED_USERS`, default 30, caps how many people are tracked in total).
  Leaving every team and the selection removes their stored history.

### Team report

Week, month or quarter, browsable backwards. A running period is compared with the **same
number of days** of the previous period, so the comparison is fair on day 3 as well as on
day 30.

| Number | How it is counted |
| --- | --- |
| Hours logged | GitLab time entries of team members, by the day they were spent |
| MRs merged | Merged MRs authored by a team member, each MR once |
| Issues closed | Closed issues currently assigned to a team member, each issue once |
| MRs opened, approvals, review comments | GitLab events of team members |
| MR cycle time | Creation to merge of the merged MRs: median and middle half (IQR) |
| Spent vs estimate | Closed issues with an estimate: total time spent / total estimate, with coverage |
| Share of effort | Hours per project, with MRs merged and issues closed there |
| Knowledge spread | Team members who authored a merged MR, approved or reviewed MRs per project in the last 12 weeks; one person is flagged |

With a scope, only work in the scoped projects counts. Work outside GitLab (support,
meetings, mentoring) is invisible; the report says so.

**Copy last week as Markdown** produces a short report of the last completed calendar week
(totals, time per project, merged MRs and closed issues with links), ready to paste.

### People, not rankings

Totals and deltas are team-level. The per-person table is switched off by default
(`TEAMPULSE_TEAM_PERSON_BREAKDOWN=true` enables it). When enabled it is alphabetical, not
sortable by a number, shows no deltas, and shows how many days have time logged, so an empty
cell reads as "doesn't log time" rather than "did nothing". Ranking people by MR or issue
counts invites gaming the numbers and says little about the work. In Germany, storing and
comparing per-person activity is also subject to works-council co-determination
(§87(1) no. 6 BetrVG) and needs a GDPR purpose. Agree on it before enabling the table.

## Stored history and GitLab load

| Data | Kept | Refresh | Requests |
| --- | --- | --- | --- |
| `activity_daily` (counts per day, project and metric) | 53 weeks | hourly, from the events the contribution calendar already reads | none extra |
| `timelog_daily` (seconds per day and project) | 53 weeks | hourly: backfill once, then the last 31 days | 1+ GraphQL page per person |
| `outcomes` (merged MRs, closed issues) | 53 weeks | hourly, incremental by update time | 2 lists per person |
| milestone statistics | while referenced | hourly | 1+ per milestone |

The first sync after the upgrade refetches each person's year of events once to build the
per-project history (about as long as the first contribution-calendar backfill). Raw events are
still kept only for the short dashboard window.
