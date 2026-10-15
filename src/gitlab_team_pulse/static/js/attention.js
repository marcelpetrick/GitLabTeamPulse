// "Needs attention" strip and milestone progress: what is stuck, late or waiting, across people.

import { h, icon, s, safeLink } from "./dom.js";
import { formatDate, formatDuration, plural } from "./format.js";
import { rel } from "./ui.js";

const PREVIEW_ROWS = 6;
const MERGE_PROBLEM_TEXT = {
  conflict: "merge conflicts",
  need_rebase: "needs a rebase",
  pipeline: "pipeline must pass",
  threads: "unresolved threads",
  changes_requested: "changes requested",
  blocked: "blocked by another MR",
};

/** Tile definitions: code, label, tooltip; the order is the order of urgency. */
export const ATTENTION_TILES = [
  ["stalled", "Stalled", (a) => `Open and assigned, but no update for ${a.stalled_days} days or more — something is probably wrong.`],
  ["overdue", "Overdue", () => "Open items past their due date."],
  ["due_soon", "Due soon", (a) => `Open items due within ${a.due_soon_days} days.`],
  ["review", "Waiting for review", () => "Open merge requests where a monitored person is a reviewer."],
  ["cannot_merge", "Can't merge", () => "Conflicts, rebase needed, pipeline must pass, unresolved threads or changes requested."],
  ["no_reviewer", "No reviewer", () => "Open merge requests of monitored people without any reviewer."],
  ["unmonitored_reviewer", "Reviewer not monitored", () => "Open merge requests whose reviewers are all colleagues who are not monitored on this dashboard — not necessarily people outside the company. Useful to see review dependencies on others."],
];

function reasonText(reason) {
  switch (reason.code) {
    case "stalled": return `No update for ${plural(reason.days, "day")}`;
    case "overdue": return reason.days === 0 ? "Due today" : `Overdue by ${plural(reason.days, "day")}`;
    case "due_soon": return reason.days === 0 ? "Due today" : `Due in ${plural(reason.days, "day")}`;
    case "review": return reason.idle_days >= 1
      ? `Review waiting · opened ${reason.days} d ago, idle ${reason.idle_days} d`
      : `Review waiting · opened ${reason.days} d ago`;
    case "cannot_merge": return `Can't merge: ${reason.problems.map((p) => MERGE_PROBLEM_TEXT[p] || p).join(", ")}`;
    case "no_reviewer": return "No reviewer assigned";
    case "unmonitored_reviewer": return `Reviewed by ${reason.reviewers.map((r) => (r.name && r.name !== r.username ? `${r.name} (@${r.username})` : `@${r.username}`)).join(", ")} · not monitored here`;
    default: return reason.code;
  }
}

const SEVERITY_BADGE = { error: "err", warning: "warn", info: "info" };
const REASON_TITLES = {
  stalled: (a) => `Open and assigned, but nothing changed for ${a.stalled_days} days or more: probably blocked or forgotten`,
  overdue: "The due date has passed and the item is still open",
  due_soon: "Due within the next days",
  review: "A monitored person is asked to review this merge request (age since it was opened; idle = days without any change)",
  cannot_merge: "GitLab reports that this merge request cannot be merged as it is",
  no_reviewer: "Nobody is asked to review this merge request",
  unmonitored_reviewer: "The reviewers are colleagues who are not monitored on this dashboard",
};

function reasonTitle(code, attention) {
  const title = REASON_TITLES[code];
  if (typeof title === "function") return title(attention);
  return title || code;
}

export class AttentionPanel {
  constructor() {
    this.filter = "all";
    this.expanded = false;
  }

  render(attention, people) {
    this.attention = attention;
    const section = h("section", { class: "attention", "aria-labelledby": "attention-title" });
    const items = attention.items;
    const head = h("div", { class: "attention-head" },
      h("div", {},
        h("h2", { id: "attention-title" }, icon("warning"), "Needs attention"),
        h("div", { class: "muted" },
          items.length
            ? `${plural(items.length, "item")} across ${plural(people, "person", "people")} · stalled means no update for ${attention.stalled_days}+ days`
            : `Across ${plural(people, "person", "people")}`)));
    section.append(head);
    if (items.length === 0) {
      section.append(h("p", { class: "all-clear" }, icon("check"),
        "All clear — nothing stalled, overdue or waiting for review."));
      return section;
    }
    const list = h("ol", { class: "attention-list" });
    const more = h("div", { class: "attention-more" });
    const tiles = h("div", { class: "attention-tiles", role: "group", "aria-label": "Filter attention items" },
      this.tile("all", "All", items.length, "Everything that needs attention.", "neutral"),
      ATTENTION_TILES.map(([code, label, tip]) => {
        const count = attention.counts[code] || 0;
        const severity = ["stalled", "overdue"].includes(code) ? "error" : ["unmonitored_reviewer"].includes(code) ? "info" : "warning";
        return this.tile(code, label, count, tip(attention), count ? severity : "zero");
      }));
    const renderList = () => {
      for (const tile of tiles.children) tile.setAttribute("aria-pressed", String(tile.dataset.code === this.filter));
      const chosen = items.filter((item) => this.filter === "all" || item.reasons.some((r) => r.code === this.filter));
      const shown = this.expanded ? chosen : chosen.slice(0, PREVIEW_ROWS);
      list.replaceChildren(...shown.map((item) => this.row(item)));
      more.replaceChildren();
      if (chosen.length > PREVIEW_ROWS) {
        more.append(h("button", {
          type: "button", class: "button ghost small", "aria-expanded": String(this.expanded),
          title: "Show every item of this filter or only the most urgent ones",
          onclick: () => { this.expanded = !this.expanded; renderList(); },
        }, this.expanded ? "Show fewer" : `Show all ${chosen.length}`));
      }
    };
    for (const tile of tiles.children) {
      tile.addEventListener("click", () => {
        this.filter = this.filter === tile.dataset.code ? "all" : tile.dataset.code;
        renderList();
      });
    }
    if (this.filter !== "all" && !(attention.counts[this.filter] > 0)) this.filter = "all";
    renderList();
    section.append(tiles, list, more);
    return section;
  }

  tile(code, label, count, title, tone) {
    return h("button", {
      type: "button", class: `attention-tile tone-${tone}`, dataset: { code }, title,
      "aria-pressed": String(this.filter === code), disabled: count === 0 && code !== "all",
    }, h("span", { class: "count" }, String(count)), h("span", { class: "label" }, label));
  }

  row(item) {
    const typeText = item.kind === "merge_request" ? "MR" : item.kind === "epic" ? "Epic" : "Issue";
    const people = item.people.map((p) => `${p.name}${p.relations.includes("reviewer") && !p.relations.includes("assignee") ? " (reviewer)" : ""}`);
    return h("li", { class: `attention-row sev-${item.severity}`, dataset: { reference: item.reference } },
      h("span", { class: `badge ${item.kind === "merge_request" ? "accent" : "outline"}`, title: item.kind === "merge_request" ? "Merge request" : item.kind === "epic" ? "Epic" : "Issue" }, typeText),
      h("div", { class: "attention-main" },
        h("div", { class: "title" }, safeLink(item.web_url, item.title)),
        h("div", { class: "meta" },
          h("span", { class: "ref" }, item.reference),
          item.draft ? h("span", { class: "badge warn", title: "Draft: not ready to be merged yet" }, "Draft") : null,
          h("span", { class: "faint" }, "·"),
          h("span", {}, people.join(", ")),
          item.milestone ? [h("span", { class: "faint" }, "·"), h("span", {}, item.milestone)] : null)),
      h("div", { class: "attention-reasons" },
        item.reasons.map((reason) => h("span", { class: `badge ${SEVERITY_BADGE[reason.severity]}`, dataset: { reason: reason.code }, title: reasonTitle(reason.code, this.attention) },
          reason.severity === "error" ? icon("warning") : null, reasonText(reason)))),
      h("div", { class: "attention-updated muted" }, "updated ", rel(item.updated_at)));
  }
}

function burnup(stats, milestone) {
  // Closed issues over time against a straight line from start to due date (GitLab Free has no
  // burndown chart; this one is computed from the milestone's issues).
  const width = 220;
  const height = 46;
  const total = stats.total || 0;
  const dates = stats.closed_dates || [];
  const start = milestone.start_date || dates[0] || null;
  const end = milestone.due_date || null;
  if (!total || !start || !end || end <= start) return null;
  const t0 = Date.parse(start);
  const t1 = Date.parse(end);
  const now = Date.now();
  const x = (t) => Math.max(0, Math.min(width, ((t - t0) / (t1 - t0)) * width));
  const y = (n) => height - 2 - (n / total) * (height - 4);
  let count = dates.filter((d) => Date.parse(d) < t0).length;
  const points = [[x(t0), y(count)]];
  for (const d of dates) {
    const t = Date.parse(d);
    if (t < t0) continue;
    points.push([x(t), y(count)]);
    count += 1;
    points.push([x(t), y(count)]);
  }
  points.push([x(Math.min(now, t1)), y(count)]);
  return s("svg", { class: "burnup", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${stats.closed} of ${total} issues closed` },
    s("title", {}, `Burn-up: closed issues over time (green) against an even pace from start to due date (dashed); the vertical line is today. ${stats.closed} of ${total} closed.`),
    s("line", { x1: 0, y1: y(0), x2: width, y2: y(total), class: "burnup-ideal" }),
    now >= t0 && now <= t1 ? s("line", { x1: x(now), y1: 0, x2: x(now), y2: height, class: "burnup-today" }) : null,
    s("polyline", { points: points.map((p) => p.join(",")).join(" "), class: "burnup-line" }));
}

function dueText(milestone) {
  if (milestone.days_left === null || milestone.days_left === undefined) return ["No due date", ""];
  const date = formatDate(milestone.due_date);
  if (milestone.days_left < 0) return [`Overdue since ${date}`, "overdue"];
  if (milestone.days_left === 0) return [`Due today (${date})`, "soon"];
  return [`Due ${date} · in ${plural(milestone.days_left, "day")}`, milestone.days_left <= 7 ? "soon" : ""];
}

const MILESTONE_PREVIEW = 5;

export class MilestonesPanel {
  constructor() {
    this.expanded = false;
  }

  render(milestones) {
    if (!milestones || milestones.length === 0) return null;
    const grid = h("div", { class: "milestone-grid" });
    const toggle = h("button", { type: "button", class: "button ghost small", title: "Show all milestones or only the soonest due" });
    const draw = () => {
      const shown = this.expanded ? milestones : milestones.slice(0, MILESTONE_PREVIEW);
      grid.replaceChildren(...shown.map((m) => milestoneTile(m)));
      toggle.hidden = milestones.length <= MILESTONE_PREVIEW;
      toggle.textContent = this.expanded ? "Show fewer" : `Show all ${milestones.length}`;
      toggle.setAttribute("aria-expanded", String(this.expanded));
    };
    toggle.addEventListener("click", () => { this.expanded = !this.expanded; draw(); });
    draw();
    return h("section", { class: "milestones", "aria-labelledby": "milestones-title" },
      h("div", { class: "panel-head" },
        h("div", {},
          h("h3", { id: "milestones-title" }, "Milestones"),
          h("div", { class: "muted small-note" }, "Active milestones of open work, soonest due first · progress computed from each milestone's issues"))),
      grid, toggle);
  }
}

function milestoneTile(m) {
  const stats = m.stats;
  const [due, dueCls] = dueText(m);
  const tile = h("article", { class: `milestone${dueCls ? ` is-${dueCls}` : ""}`, dataset: { milestoneId: m.id } },
    h("div", { class: "milestone-title" },
      h("strong", {}, safeLink(m.web_url, m.title)),
      m.project ? h("span", { class: "muted" }, m.project.path || m.project.name) : null),
    h("div", { class: `milestone-due ${dueCls}`, title: "Due date of the milestone in GitLab" }, dueCls === "overdue" ? icon("warning") : icon("clock"), due));
  if (stats) {
    const progress = h("div", { class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(stats.total), "aria-valuenow": String(stats.closed), title: `${stats.closed} of ${stats.total} issues of the milestone are closed (all issues, not only the monitored people's)` },
      h("div", { class: "progress-fill" }));
    progress.firstChild.style.width = `${stats.total ? (stats.closed / stats.total) * 100 : 0}%`;
    const chart = burnup(stats, m);
    tile.append(h("div", { class: "milestone-progress" }, progress, h("span", {}, `${stats.closed} of ${plural(stats.total, "issue")} closed`)));
    if (chart) tile.append(chart);
    tile.append(
      h("ul", { class: "milestone-facts" },
        stats.estimate
          ? h("li", {}, "Remaining estimate ", h("strong", {}, formatDuration(stats.remaining)))
          : h("li", { class: "muted" }, "No time estimates"),
        stats.open_unestimated ? h("li", { class: "warn-text" }, plural(stats.open_unestimated, "open issue"), " without estimate") : null,
        stats.open_overdue ? h("li", { class: "err-text" }, plural(stats.open_overdue, "open issue"), " overdue") : null));
  } else {
    tile.append(h("p", { class: "muted" }, "Progress is computed on the next refresh."));
  }
  tile.append(h("div", { class: "milestone-people muted" },
    `${plural(m.monitored_open, "open item")} of ${m.monitored_people.join(", ")}`));
  return tile;
}
