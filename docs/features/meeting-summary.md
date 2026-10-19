# Meeting summary

A crisp, per-person summary for stand-ups, weekly meetings and sprint reviews: **what was the
real outcome**, not how many hours were burned. Built on demand from the local cache with plain
Python rules; no language model, no extra GitLab requests.

## Using it

1. On the dashboard, press **Meeting summary**.
2. Choose the period (*Yesterday and today*, *Last 7 days*, *Last 14 days*, *Last 30 days*) and
   untick anybody who should not be in it (default: everyone on the dashboard).
3. Press **Generate**. The result stays as it is while the dashboard keeps polling; press
   **Regenerate** for newer data. **Copy as Markdown** puts it on the clipboard.

API: `GET /api/meeting-summary?user_ids=2,3&days=7` (`user_ids` optional: default is the
dashboard selection in its order; `days` 1 to 31 local days including today; at most 50 people).
The endpoint only reads SQLite and never wakes the scheduler.

## What counts as value

Per person, the headline is the **first level that has GitLab facts**:

| Level | Facts | Example headline |
| --- | --- | --- |
| Delivered | Issues closed and MRs merged in the period, assigned to the person (or MRs authored, from the stored outcomes) | *Delivered 3: #53 Support custom CA bundles (Prio high); #45 Cache warmup after deploy (incident); !141 Speed up CI. Also unblocked 2 MRs of others.* |
| Unblocked others | Colleagues' MRs this person reviewed that were merged in the period; approvals given | *No own delivery in GitLab; unblocked others: reviewed 2 MRs that got merged, 3 approvals.* |
| In progress | Open assigned items updated or worked on (time logged) in the period; MRs opened, in review, in draft | *In progress: !34 New API (opened, in review); #45 Hard bug (worked on).* |
| Discussion | Issues opened, comments | *Discussion and triage: 2 issues opened, 5 comments.* |
| Last delivery | Nothing above: the most recent delivery within 90 days | *Nothing delivered in GitLab in this period; last delivery: #77 Export, 12 days ago.* |
| Nothing | GitLab has nothing for the person | *No GitLab activity recorded in this period (absence or work outside GitLab are possible).* |

Below the headline, up to five delivered items, then up to three lines each of **Unblocked**
(reviewed and merged), **Next** (in progress) and **Decide** (overdue, stalled, cannot merge, no
reviewer, waiting for this person's review; the same rules as *Needs attention*). Approvals,
review comments and logged hours close the block as *Context*.

## Order of delivered items

Items, never people, are ordered by a weight from facts. Each item shows its reasons as chips;
the weight itself is not shown.

| Fact | Weight | Chip |
| --- | --- | --- |
| Every delivered item | 10 | |
| Priority label `Prio::`/`priority::` 1, high, highest, critical, urgent, blocker | +8 | `Prio 1` |
| Priority 2 or medium | +4 | `Prio medium` |
| Issue type incident | +8 | `incident` |
| Label word security | +6 | `security` |
| Label word bug, defect or regression (not for incidents) | +4 | `bug fix` |
| Label word customer | +3 | `customer` |
| Label word feature, enhancement or story | +3 | `feature` |
| In a milestone (not Backlog, Icebox, Someday or Later) | +3, +2 more when due within 14 days | `milestone 2026.10` |
| Finished on or before the due date | +3 | `on time` |
| Finished after the due date (overdue, but done) | +2 | `done 3 d after due date` |
| 16 h or more spent or estimated / 4 h or more | +4 / +2 | `large: 20h 00m` / `5h 00m` |
| Open 90 days or more / 30 days or more before it was finished | +4 / +2 | `open 119 d` |
| Delivered together with its MR | +2 | `with !12` |

An MR is shown with the issue it delivers when both are delivered in the period, belong to the
same project and the MR title names the issue (`#12`) or is GitLab's default
`Resolve "<issue title>"`.

## Deliberately not done

- No numbers that compare people, no ranking of people, no "productivity".
- No inference of effort from activity timestamps; hours come from GitLab time tracking only and
  are context.
- No automatic refresh: a summary is a snapshot for a meeting.
- No AI: every sentence comes from a template filled with GitLab facts, so it is reproducible and
  explainable (*How it is counted* in the dialog lists the rules).

## Data and limits

- Work items come from the 10-minute work cache (open items, and items updated in the last
  `TEAMPULSE_WORK_WINDOW_DAYS` days); merged MRs authored by the person and closed issues come
  from the hourly outcomes history as well, so a 30-day summary sees them even without an
  assignment.
- Time per item uses the raw time entries (`TEAMPULSE_ACTIVITY_DAYS`, 7 by default); the period
  total uses the daily history when it is larger.
- Approvals, review comments and issues opened come from the daily event history.
