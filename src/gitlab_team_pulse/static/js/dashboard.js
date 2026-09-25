// Dashboard view: one dense card per selected user, rendered from the local cache.

import { api } from "./api.js";
import { activityChart, timeChart } from "./charts.js";
import { Skyline, heatmap, legend } from "./contributions.js";
import { clear, h, icon, s, safeLink } from "./dom.js";
import { exactTime, formatDate, formatDuration, plural, todayIso } from "./format.js";
import { avatar, freshnessPill, rel, toast } from "./ui.js";

const WORK_SORTS = {
  updated_desc: ["Updated · newest first", (a, b) => cmp(b.updated_at, a.updated_at)],
  updated_asc: ["Updated · oldest first", (a, b) => cmp(a.updated_at, b.updated_at)],
  title_asc: ["Title A–Z", (a, b) => collator.compare(a.title, b.title)],
  title_desc: ["Title Z–A", (a, b) => collator.compare(b.title, a.title)],
  project: ["Project", (a, b) => collator.compare(a.project?.path || "", b.project?.path || "") || cmp(b.updated_at, a.updated_at)],
  state: ["State", (a, b) => collator.compare(a.state, b.state) || cmp(b.updated_at, a.updated_at)],
  due: ["Due date", (a, b) => (a.due_date ? 0 : 1) - (b.due_date ? 0 : 1) || cmp(a.due_date, b.due_date)],
  type: ["Type", (a, b) => collator.compare(typeLabel(a), typeLabel(b)) || cmp(b.updated_at, a.updated_at)],
};
const collator = new Intl.Collator("en", { sensitivity: "base", numeric: true });
const OPEN = new Set(["opened", "open", "locked"]);
const WORK_ROWS = 10;
const STATE_BADGE = { opened: ["Open", "info"], closed: ["Closed", ""], merged: ["Merged", "accent"], locked: ["Locked", "warn"] };

function cmp(a, b) {
  return String(a || "").localeCompare(String(b || ""));
}

function typeLabel(item) {
  if (item.kind === "merge_request") return "Merge request";
  if (item.kind === "epic") return "Epic";
  const type = item.issue_type || "issue";
  return type.charAt(0).toUpperCase() + type.slice(1).replace("_", " ");
}

function labelChip(label) {
  const [scope, value] = label.includes("::") ? label.split("::", 2) : [null, label];
  return h("span", { class: "label-chip" }, scope ? [h("span", { class: "scope" }, `${scope}::`), value] : label);
}

const emptyArt = () =>
  s("svg", { class: "art", viewBox: "0 0 120 90", "aria-hidden": "true" },
    s("rect", { x: 10, y: 14, width: 100, height: 62, rx: 10, fill: "none", stroke: "currentColor", "stroke-width": 2, opacity: 0.35 }),
    s("path", { d: "M22 52h14l7-16 10 26 7-14h18", fill: "none", stroke: "currentColor", "stroke-width": 3, "stroke-linecap": "round", "stroke-linejoin": "round", opacity: 0.7 }),
    s("circle", { cx: 92, cy: 30, r: 6, fill: "currentColor", opacity: 0.35 }));

export class DashboardView {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.cardState = new Map(); // per user: sort, state filter, expansion flags
    this.refreshing = false;
  }

  uiState(userId) {
    if (!this.cardState.has(userId)) {
      this.cardState.set(userId, { sort: "updated_desc", filter: "all", allActivity: false, allWork: false, entries: false, calendar: "2d" });
    }
    return this.cardState.get(userId);
  }

  render() {
    const data = this.app.dashboard;
    const status = this.app.status;
    this.refreshButton = h("button", {
      type: "button", class: "button primary", id: "refresh-now", onclick: () => this.refresh(),
    });
    this.updateRefreshButton();
    const selected = status?.selected;
    const head = h("div", { class: "view-head" },
      h("div", {},
        h("h1", { id: "dashboard-title" }, "Dashboard"),
        h("div", { class: "subtitle" },
          data ? `${plural(data.cards.length, "person", "people")} monitored · last 7 days` : "Loading…")),
      h("div", { class: "spacer" }),
      h("div", { class: "toolbar" },
        selected ? freshnessPill(selected, { label: "Selected users data" }) : null,
        this.refreshButton));
    const nodes = [head];
    if (selected && (selected.status === "error" || selected.status === "stale" || (selected.status === "refreshing" && selected.stale))) {
      nodes.push(this.banner(selected));
    }
    if (!data) {
      nodes.push(h("div", { class: "cards" }, this.skeletonCard()));
    } else if (data.cards.length === 0) {
      nodes.push(h("div", { class: "empty" }, emptyArt(),
        h("h2", {}, "Nobody is monitored yet"),
        h("p", {}, "Choose the GitLab accounts you want to follow. The selection is shared by everyone using this dashboard."),
        h("button", { type: "button", class: "button primary", onclick: () => this.app.navigate("people") }, icon("users"), "Choose people")));
    } else {
      const today = todayIso(data.timezone);
      nodes.push(h("div", { class: "cards" }, data.cards.map((card) => this.card(card, today))));
    }
    this.root.replaceChildren(...nodes);
  }

  banner(info) {
    const isError = info.error;
    const text = isError
      ? ["Showing cached data — the latest refresh failed. ", info.message ? `(${info.message}) ` : "", "Last good data: "]
      : ["Data is older than the expected refresh interval. Last good data: "];
    return h("div", { class: `banner ${isError ? "err" : "warn"}`, role: "status" },
      icon("warning"),
      h("div", { class: "banner-text" }, h("strong", {}, isError ? "Refresh problem. " : "Stale data. "), text,
        info.last_success_at ? h("span", {}, exactTime(info.last_success_at), " (", rel(info.last_success_at), ")") : "none yet"),
      h("button", { type: "button", class: "button small", onclick: () => this.app.openDiagnostics() }, "View diagnostics"));
  }

  updateRefreshButton() {
    if (!this.refreshButton) return;
    const crawler = this.app.status?.crawler;
    const busy = this.refreshing || crawler?.state === "refreshing";
    this.refreshButton.disabled = busy;
    this.refreshButton.setAttribute("aria-busy", String(busy));
    this.refreshButton.replaceChildren(
      busy ? h("span", { class: "spinner", "aria-hidden": "true" }) : icon("refresh"),
      busy ? "Refreshing…" : "Refresh now");
  }

  async refresh() {
    this.refreshing = true;
    this.updateRefreshButton();
    try {
      const result = await api.refresh();
      toast(result.status === "coalesced" ? "A refresh is already running — joined it." : "Refresh started. Cached data stays visible meanwhile.");
      this.app.pollSoon();
    } catch (error) {
      this.refreshing = false;
      if (error.status === 429) {
        const wait = Math.ceil(error.body?.retry_after_seconds || 5);
        toast(`A refresh was just requested. Try again in ${wait}s.`);
      } else {
        toast(`Refresh failed to start: ${error.message}`, "error");
      }
      this.updateRefreshButton();
    }
  }

  onStatus(status) {
    if (this.refreshing && status.crawler.state !== "refreshing" && !status.crawler.refresh_pending) {
      this.refreshing = false;
    }
    this.updateRefreshButton();
  }

  skeletonCard() {
    return h("article", { class: "card", "aria-busy": "true" },
      h("div", { class: "card-head" }, h("div", { class: "avatar large" }), h("div", { class: "skeleton" })),
      h("div", { class: "card-body" }, [0, 1, 2].map(() => h("div", { class: "panel" },
        h("div", { class: "skeleton" }), h("div", { class: "skeleton" }), h("div", { class: "skeleton" })))));
  }

  card(card, today) {
    const { user, freshness, counts } = card;
    const state = this.uiState(user.id);
    const neverSynced = freshness.status === "never" || (freshness.status === "refreshing" && !freshness.last_success_at);
    const classes = ["card"];
    if (freshness.stale) classes.push("is-stale");
    if (freshness.error) classes.push("is-error");
    const lowTime = counts.time_seconds_7d === 0;
    const article = h("article", { class: classes.join(" "), "aria-labelledby": `card-${user.id}`, dataset: { userId: user.id } },
      h("header", { class: "card-head" },
        avatar(user, true),
        h("div", { class: "identity" },
          h("div", { class: "line" },
            h("h2", { id: `card-${user.id}` }, user.name),
            user.account_type !== "human" ? h("span", { class: "badge accent" }, user.account_type) : null,
            user.state !== "active" ? h("span", { class: "badge warn" }, user.state) : null),
          h("div", { class: "line" },
            h("span", { class: "username" }, safeLink(user.web_url, `@${user.username}`)),
            freshnessPill(freshness, { small: true, label: `${user.name} data` }))),
        h("div", { class: "stats" },
          this.stat(counts.work_total, "work items", `${counts.work_open} open`),
          this.stat(counts.events_7d, "actions · 7 days"),
          this.stat(formatDuration(counts.time_seconds_7d), "logged · 7 days", null, lowTime))));
    if (neverSynced) {
      article.append(h("div", { class: "card-body" },
        h("div", { class: "panel" }, h("p", { class: "card-empty" },
          h("span", { class: "spinner", "aria-hidden": "true" }), " Waiting for the first synchronization of this user…"))));
      return article;
    }
    article.append(
      h("div", { class: "card-body" },
        this.activityPanel(card, state),
        h("section", { class: "panel", "aria-label": "Activity in the last 7 days" },
          h("div", { class: "panel-head" }, h("h3", {}, "Activity · last 7 days")),
          activityChart(card.activity_days, today)),
        this.timePanel(card, state, today)),
      this.contributionPanel(card, state),
      this.workPanel(card, state));
    return article;
  }

  stat(value, label, extra = null, warn = false) {
    return h("div", { class: `stat${warn ? " warn" : ""}`, title: warn ? "No time logged in GitLab during the last 7 days" : null },
      h("span", { class: "value" }, warn ? [icon("warning"), " ", String(value)] : String(value)),
      h("span", { class: "label" }, extra ? `${label} · ${extra}` : label));
  }

  activityPanel(card, state) {
    const events = card.activity;
    const preview = card.activity_preview_count || 5;
    const list = h("ol", { class: "activity-list" });
    events.forEach((event, index) => {
      const item = h("li", { class: `activity-item${index >= preview ? " activity-extra" : ""}`, hidden: index >= preview && !state.allActivity },
        h("span", { class: `cat-icon cat-${event.category}`, title: event.category.replace("_", " ") }, icon(event.category)),
        h("div", {},
          h("div", { class: "summary" }, safeLink(event.web_url, event.summary)),
          h("div", { class: "meta" },
            event.project ? safeLink(event.project.web_url, event.project.path || event.project.name) : null,
            event.project ? h("span", { class: "faint" }, "·") : null,
            rel(event.occurred_at))));
      list.append(item);
    });
    const panel = h("section", { class: "panel", "aria-label": "Recent activity" },
      h("div", { class: "panel-head" }, h("h3", {}, "Recent activity")));
    if (events.length === 0) {
      panel.append(h("p", { class: "card-empty" }, "No recent GitLab activity."));
      return panel;
    }
    panel.append(list);
    if (events.length > preview) {
      const toggle = h("button", {
        type: "button", class: "button ghost small", "aria-expanded": String(state.allActivity),
        onclick: () => {
          state.allActivity = !state.allActivity;
          for (const node of list.querySelectorAll(".activity-extra")) node.hidden = !state.allActivity;
          toggle.setAttribute("aria-expanded", String(state.allActivity));
          toggle.textContent = state.allActivity ? "Show fewer" : `Show all ${events.length}`;
        },
      }, state.allActivity ? "Show fewer" : `Show all ${events.length}`);
      panel.append(toggle);
    }
    return panel;
  }

  timePanel(card, state, today) {
    const time = card.time;
    const byDay = new Map();
    for (const entry of time.entries) byDay.set(entry.date, [...(byDay.get(entry.date) || []), entry]);
    const panel = h("section", { class: "panel", "aria-label": "Logged time in the last 7 days" },
      h("div", { class: "panel-head" }, h("h3", {}, "Logged time · last 7 days")),
      h("div", { class: "time-total" },
        h("span", { class: "big" }, formatDuration(time.total_seconds)),
        h("span", { class: "muted" }, time.total_seconds ? `from ${plural(time.entries.length, "GitLab timelog")}` : "no time logged in GitLab")),
      timeChart(time.days, today, byDay));
    if (time.projects.length) {
      const top = time.projects[0].seconds || 1;
      panel.append(h("ul", { class: "time-projects", "aria-label": "Logged time per project" },
        time.projects.slice(0, 4).map((project) => {
          const bar = h("div", { class: "bar" });
          bar.style.width = `${Math.max(4, (project.seconds / top) * 100)}%`;
          return h("li", {}, h("div", { class: "grow" }, h("span", {}, project.path), bar), h("strong", {}, formatDuration(project.seconds)));
        })));
    }
    if (time.entries.length) {
      const table = h("table", { class: "time-entries", hidden: !state.entries },
        h("tbody", {}, time.entries.map((entry) => h("tr", {},
          h("td", { class: "nowrap", title: exactTime(entry.spent_at) }, formatDate(entry.spent_at)),
          h("td", { class: "nowrap mono" }, formatDuration(entry.seconds)),
          h("td", {}, entry.target_title ? safeLink(entry.web_url, entry.target_title) : h("span", { class: "muted" }, entry.project_path || "—"),
            entry.summary ? h("div", { class: "muted" }, entry.summary) : null)))));
      const toggle = h("button", {
        type: "button", class: "button ghost small", "aria-expanded": String(state.entries),
        onclick: () => {
          state.entries = !state.entries;
          table.hidden = !state.entries;
          toggle.setAttribute("aria-expanded", String(state.entries));
          toggle.textContent = state.entries ? "Hide entries" : `Show ${plural(time.entries.length, "entry", "entries")}`;
        },
      }, state.entries ? "Hide entries" : `Show ${plural(time.entries.length, "entry", "entries")}`);
      panel.append(toggle, table);
    }
    return panel;
  }

  contributionPanel(card, state) {
    const calendar = card.contributions;
    const section = h("section", { class: "contributions", "aria-label": "Contributions in the last 12 months" });
    if (!calendar) return section;
    const body = h("div", { class: "contributions-body" });
    const render = () => {
      clear(body);
      if (state.calendar === "3d") {
        const skyline = new Skyline(calendar);
        body.append(skyline.element);
      } else {
        body.append(h("div", { class: "heatmap-scroll" }, heatmap(calendar)), legend());
      }
    };
    const toggle = h("div", { class: "chip-group small", role: "group", "aria-label": "Contribution view" },
      [["2d", "2D calendar"], ["3d", "3D skyline"]].map(([key, label]) =>
        h("button", {
          type: "button", "aria-pressed": String(state.calendar === key), dataset: { calendarView: key },
          onclick: () => {
            state.calendar = key;
            for (const b of toggle.children) b.setAttribute("aria-pressed", String(b.dataset.calendarView === key));
            render();
          },
        }, label)));
    const busiest = calendar.busiest
      ? ` · busiest day ${formatDate(calendar.busiest.date)} (${calendar.busiest.count})`
      : "";
    section.append(
      h("div", { class: "panel-head" },
        h("div", {},
          h("h3", {}, "Contributions · last 12 months"),
          h("div", { class: "muted contributions-summary" },
            `${plural(calendar.total, "contribution")}${busiest}`, " ",
            freshnessPill(calendar.freshness, { small: true, label: "Contribution calendar" }))),
        toggle),
      body);
    render();
    return section;
  }

  workPanel(card, state) {
    const section = h("section", { class: "work", "aria-label": "Assigned and recently relevant work" });
    const body = h("div", {});
    const filterGroup = h("div", { class: "chip-group small", role: "group", "aria-label": "Work state filter" },
      [["all", "All"], ["open", "Open"], ["done", "Closed / merged"]].map(([key, label]) =>
        h("button", {
          type: "button", "aria-pressed": String(state.filter === key), dataset: { filter: key },
          onclick: () => {
            state.filter = key;
            for (const b of filterGroup.children) b.setAttribute("aria-pressed", String(b.dataset.filter === key));
            renderTable();
          },
        }, label)));
    const sortSelect = h("select", {
      class: "select", "aria-label": `Sort work of ${card.user.name}`, dataset: { role: "work-sort" },
      onchange: (e) => { state.sort = e.target.value; renderTable(); },
    }, Object.entries(WORK_SORTS).map(([key, [label]]) => h("option", { value: key, selected: key === state.sort }, label)));
    const heading = h("h3", {});
    section.append(h("div", { class: "panel-head" }, heading, h("div", { class: "toolbar" }, filterGroup, sortSelect)), body);

    const renderTable = () => {
      const items = card.work
        .filter((w) => state.filter === "all" || (state.filter === "open" ? OPEN.has(w.state) : !OPEN.has(w.state)))
        .sort(WORK_SORTS[state.sort][1]);
      heading.textContent = `Work · ${items.length} of ${card.work.length} items`;
      clear(body);
      if (items.length === 0) {
        body.append(h("p", { class: "card-empty" }, card.work.length ? "No items match this filter." : "Nothing assigned or updated recently."));
        return;
      }
      const shown = state.allWork ? items : items.slice(0, WORK_ROWS);
      const todayStr = new Date().toISOString().slice(0, 10);
      body.append(h("div", { class: "table-scroll" }, h("table", { class: "work-table" },
        h("thead", {}, h("tr", {}, ["Type", "Title", "Project", "State", "Milestone", "Due", "Updated"].map((c) => h("th", { scope: "col" }, c)))),
        h("tbody", {}, shown.map((item) => {
          const [stateText, stateCls] = STATE_BADGE[item.state] || [item.state, ""];
          const overdue = item.due_date && item.due_date < todayStr && OPEN.has(item.state);
          return h("tr", { dataset: { kind: item.kind, state: item.state } },
            h("td", { class: "nowrap" },
              h("span", { class: `badge ${item.kind === "merge_request" ? "accent" : "outline"}` }, typeLabel(item)),
              item.relations.includes("reviewer") ? h("div", {}, h("span", { class: "badge info" }, "Reviewer")) : null),
            h("td", {},
              h("div", { class: "title" }, safeLink(item.web_url, item.title)),
              h("div", {}, h("span", { class: "ref" }, item.reference), item.draft ? [" ", h("span", { class: "badge warn" }, "Draft")] : null),
              item.labels.length ? h("div", {}, item.labels.map(labelChip)) : null),
            h("td", {}, item.project ? safeLink(item.project.web_url, item.project.path || item.project.name) : h("span", { class: "muted" }, "—")),
            h("td", { class: "nowrap" }, h("span", { class: `badge ${stateCls}` }, stateText),
              item.priority ? h("div", {}, h("span", { class: "label-chip" }, `priority ${item.priority}`)) : null),
            h("td", {}, item.milestone || h("span", { class: "muted" }, "—")),
            h("td", { class: `nowrap${overdue ? " overdue" : ""}` }, item.due_date ? [formatDate(item.due_date), overdue ? " · overdue" : ""] : h("span", { class: "muted" }, "—")),
            h("td", { class: "nowrap" }, rel(item.updated_at)));
        })))));
      if (items.length > WORK_ROWS) {
        body.append(h("button", {
          type: "button", class: "button ghost small",
          onclick: () => { state.allWork = !state.allWork; renderTable(); },
        }, state.allWork ? "Show fewer" : `Show all ${items.length}`));
      }
    };
    renderTable();
    return section;
  }
}

