// Teams view: named groups of people, their report over time and the weekly digest.
// Team totals carry deltas; people are listed alphabetically as facts, never ranked.

import { api } from "./api.js";
import { clear, h, icon, s, safeLink } from "./dom.js";
import { formatDate, formatDuration, plural } from "./format.js";
import { avatar, rel, toast } from "./ui.js";

const PERIODS = [["week", "Week"], ["month", "Month"], ["quarter", "Quarter"]];
const KPIS = [
  ["hours", "Hours logged", (t) => formatDuration(t.seconds), (t) => t.seconds],
  ["mrs_merged", "MRs merged", (t) => String(t.mrs_merged), (t) => t.mrs_merged],
  ["issues_closed", "Issues closed", (t) => String(t.issues_closed), (t) => t.issues_closed],
  ["mrs_opened", "MRs opened", (t) => String(t.mrs_opened), (t) => t.mrs_opened],
  ["approvals", "Approvals", (t) => String(t.approvals), (t) => t.approvals],
  ["review_comments", "Review comments", (t) => String(t.review_comments), (t) => t.review_comments],
];
const collator = new Intl.Collator("en", { sensitivity: "base", numeric: true });

function hoursText(hours) {
  if (hours < 24) return `${Math.round(hours * 10) / 10}h`;
  return `${Math.round((hours / 24) * 10) / 10}d`;
}

function delta(current, previous) {
  if (previous === 0 && current === 0) return h("span", { class: "delta flat" }, "±0");
  if (previous === 0) return h("span", { class: "delta up" }, "▲ new");
  const change = current - previous;
  const pct = Math.round((change / previous) * 100);
  if (change === 0) return h("span", { class: "delta flat" }, "±0%");
  return h("span", { class: `delta ${change > 0 ? "up" : "down"}` }, `${change > 0 ? "▲" : "▼"} ${Math.abs(pct)}%`);
}

export class TeamsView {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.teamId = null;
    this.period = "month";
    this.offset = 0;
    this.report = null;
    this.reportKey = null;
    this.loading = false;
    this.editor = new TeamEditor(app, (team) => {
      this.teamId = team.id;
      this.report = null;
      this.app.reloadTeams();
    });
  }

  get teams() {
    return this.app.teams?.teams || [];
  }

  get team() {
    return this.teams.find((t) => t.id === this.teamId) || null;
  }

  render() {
    const data = this.app.teams;
    const readonly = data?.readonly;
    if (!this.team && this.teams.length) this.teamId = this.teams[0].id;
    const head = h("div", { class: "view-head" },
      h("div", {},
        h("h1", { id: "teams-title" }, "Teams"),
        h("div", { class: "subtitle" },
          h("span", { class: "badge info" }, icon("globe"), "Shared"),
          readonly ? "Teams are read-only on this server." : "Teams are stored on this server and shared with everyone using the dashboard.")),
      h("div", { class: "spacer" }),
      readonly ? null : h("button", { type: "button", class: "button primary", id: "new-team", onclick: () => this.editor.open(null) }, icon("users"), "New team"));
    if (!data) {
      this.root.replaceChildren(head, h("div", { class: "empty" }, h("p", {}, "Loading teams…")));
      return;
    }
    if (this.teams.length === 0) {
      this.root.replaceChildren(head, h("div", { class: "empty" },
        h("h2", {}, "No teams yet"),
        h("p", {}, "Group people — for example everyone working on a product — and see how the team is doing for its projects over weeks, months and quarters."),
        readonly ? null : h("button", { type: "button", class: "button primary", onclick: () => this.editor.open(null) }, icon("users"), "Create a team")));
      return;
    }
    const picker = h("div", { class: "chip-group team-picker", role: "group", "aria-label": "Team" },
      this.teams.map((team) => h("button", {
        type: "button", "aria-pressed": String(team.id === this.teamId), dataset: { teamId: team.id },
        onclick: () => { this.teamId = team.id; this.report = null; this.render(); this.loadReport(); },
      }, team.name)));
    this.body = h("div", { class: "team-body" });
    this.root.replaceChildren(head, picker, this.teamHeader(this.team, readonly), this.body);
    this.renderReport();
    this.loadReport();
  }

  teamHeader(team, readonly) {
    const members = h("div", { class: "member-stack", "aria-label": `${plural(team.members.length, "member")}` },
      team.members.slice(0, 12).map((member) => {
        const node = avatar(member);
        node.setAttribute("title", `${member.name} (@${member.username})`);
        return node;
      }),
      team.members.length > 12 ? h("span", { class: "more" }, `+${team.members.length - 12}`) : null);
    return h("section", { class: "team-head card" },
      h("div", { class: "team-identity" },
        h("h2", {}, team.name),
        team.description ? h("p", { class: "muted" }, team.description) : null,
        h("div", { class: "team-meta" },
          members,
          h("span", { class: "muted" }, plural(team.members.length, "person", "people")),
          team.scopes.length
            ? h("span", { class: "scopes" }, h("span", { class: "muted" }, "Scope:"), team.scopes.map((scope) => h("span", { class: "label-chip" }, scope)))
            : h("span", { class: "muted" }, "Scope: all projects"))),
      h("div", { class: "toolbar" },
        h("button", { type: "button", class: "button", onclick: () => this.apply(team) }, icon("arrow"), "Show on dashboard"),
        readonly ? null : h("button", { type: "button", class: "button", onclick: () => this.editor.open(team) }, "Edit"),
        readonly ? null : h("button", { type: "button", class: "button ghost danger", onclick: () => this.remove(team) }, "Delete")));
  }

  async apply(team) {
    if (!window.confirm(`Show team "${team.name}" on the dashboard? This replaces the shared selection for everyone.`)) return;
    try {
      const result = await api.applyTeam(team.id);
      toast(`Showing ${team.name}: ${plural(result.selected, "person", "people")} monitored.`);
      this.app.onSelectionChanged();
      this.app.navigate("dashboard");
    } catch (error) {
      toast(`Could not show the team: ${error.message}`, "error");
    }
  }

  async remove(team) {
    if (!window.confirm(`Delete team "${team.name}"? Its members stay in GitLab and on the dashboard.`)) return;
    try {
      await api.deleteTeam(team.id);
      toast(`Team ${team.name} deleted.`);
      this.teamId = null;
      this.report = null;
      this.app.reloadTeams();
    } catch (error) {
      toast(`Could not delete the team: ${error.message}`, "error");
    }
  }

  async loadReport(force = false) {
    const team = this.team;
    if (!team) return;
    const key = `${team.id}:${this.period}:${this.offset}:${this.app.dataVersion}:${team.updated_at}`;
    if (!force && key === this.reportKey && this.report) return;
    this.loading = true;
    const { period, offset } = this;
    try {
      const report = await api.teamReport(team.id, period, offset);
      // A slower, older answer must not overwrite the report for what is selected now.
      if (this.team?.id !== team.id || this.period !== period || this.offset !== offset) return;
      this.report = report;
      this.reportKey = key;
    } catch (error) {
      toast(`Could not load the team report: ${error.message}`, "error");
    } finally {
      this.loading = false;
    }
    if (this.app.view === "teams") this.renderReport();
  }

  onData() {
    if (this.app.view === "teams") this.loadReport();
  }

  setPeriod(period, offset) {
    this.period = period;
    this.offset = Math.max(0, offset);
    this.report = null;
    this.renderReport();
    this.loadReport(true);
  }

  renderReport() {
    if (!this.body) return;
    const report = this.report;
    const controls = h("div", { class: "report-controls" },
      h("div", { class: "chip-group", role: "group", "aria-label": "Report period" },
        PERIODS.map(([key, label]) => h("button", {
          type: "button", "aria-pressed": String(this.period === key), dataset: { period: key },
          onclick: () => this.setPeriod(key, 0),
        }, label))),
      h("div", { class: "period-nav" },
        h("button", { type: "button", class: "icon-button", title: "Previous period", "aria-label": "Previous period", onclick: () => this.setPeriod(this.period, this.offset + 1) }, "◀"),
        h("strong", { class: "period-label" }, report ? report.period.label : "…"),
        h("button", { type: "button", class: "icon-button", title: "Next period", "aria-label": "Next period", disabled: this.offset === 0, onclick: () => this.setPeriod(this.period, this.offset - 1) }, "▶")),
      report ? h("span", { class: "muted period-note" }, this.periodNote(report)) : null,
      h("div", { class: "spacer" }),
      report ? this.historyPill(report.history) : null);
    if (!report) {
      this.body.replaceChildren(controls, h("div", { class: "card report-loading" }, h("div", { class: "skeleton" }), h("div", { class: "skeleton" })));
      return;
    }
    this.body.replaceChildren(...[
      controls,
      report.period.before_history ? h("div", { class: "banner info" }, icon("clock"),
        h("div", { class: "banner-text" }, `This period starts before the stored history (${formatDate(report.history.from)}); earlier days count as empty.`)) : null,
      this.kpis(report),
      h("div", { class: "report-grid" }, this.projectsPanel(report), this.spreadPanel(report)),
      this.peoplePanel(report),
      this.footer(report),
    ].filter(Boolean));
  }

  periodNote(report) {
    const p = report.period;
    if (p.running) {
      return `so far: day ${p.elapsed_days} of ${p.length_days} · compared with the same ${plural(p.elapsed_days, "day")} of ${p.previous.label}`;
    }
    return `${formatDate(p.start)} – ${formatDate(p.end)} · compared with ${p.previous.label}`;
  }

  historyPill(history) {
    if (!history.complete) {
      return h("span", { class: "pill refreshing small", title: `Still loading: ${history.pending.join(", ")}` },
        h("span", { class: "spinner", "aria-hidden": "true" }), `Loading history for ${plural(history.pending.length, "person", "people")}…`);
    }
    return h("span", { class: "pill fresh small", title: `History is kept from ${formatDate(history.from)}` },
      h("span", { class: "dot", "aria-hidden": "true" }), "History ", rel(history.updated_at, "updated "));
  }

  kpis(report) {
    const t = report.totals;
    const prev = report.previous;
    const tiles = KPIS.map(([key, label, show, raw]) => h("div", { class: "kpi", dataset: { kpi: key } },
      h("span", { class: "kpi-label" }, label),
      h("span", { class: "kpi-value" }, show(t)),
      h("span", { class: "kpi-delta" }, delta(raw(t), raw(prev)), h("span", { class: "muted" }, ` vs ${show(prev)}`))));
    const cycle = t.cycle_time_hours;
    tiles.push(h("div", { class: "kpi", dataset: { kpi: "cycle" } },
      h("span", { class: "kpi-label" }, "MR cycle time"),
      h("span", { class: "kpi-value" }, cycle ? hoursText(cycle.median) : "—"),
      h("span", { class: "kpi-delta muted" }, cycle ? `median of ${cycle.count} · middle half ${hoursText(cycle.p25)}–${hoursText(cycle.p75)}` : "no merged MRs")));
    const est = t.estimate;
    const ratio = est.estimate_seconds ? Math.round((est.spent_seconds / est.estimate_seconds) * 100) : null;
    tiles.push(h("div", { class: "kpi", dataset: { kpi: "estimate" } },
      h("span", { class: "kpi-label" }, "Spent vs estimate"),
      h("span", { class: `kpi-value${ratio !== null && ratio > 120 ? " warn-text" : ""}` }, ratio === null ? "—" : `${ratio}%`),
      h("span", { class: "kpi-delta muted" }, est.issues ? `${est.with_estimate} of ${plural(est.issues, "closed issue")} estimated` : "no closed issues")));
    return h("section", { class: "kpis", "aria-label": "Team totals" }, tiles);
  }

  projectsPanel(report) {
    const panel = h("section", { class: "card report-panel", "aria-labelledby": "share-title" },
      h("div", { class: "panel-head" }, h("h3", { id: "share-title" }, "Share of effort by project")));
    if (report.projects.length === 0) {
      panel.append(h("p", { class: "card-empty" }, "No logged time or finished work in this period."));
      return panel;
    }
    panel.append(h("table", { class: "share-table" },
      h("thead", {}, h("tr", {}, ["Project", "Time", "Share", "MRs merged", "Issues closed"].map((c, i) => h("th", { scope: "col", class: i ? "num" : null }, c)))),
      h("tbody", {}, report.projects.map((project) => {
        const bar = h("div", { class: "share-bar" });
        bar.style.width = `${Math.max(project.share * 100, project.seconds ? 1.5 : 0)}%`;
        return h("tr", {},
          h("td", {}, h("div", {}, project.path), h("div", { class: "share-track" }, bar)),
          h("td", { class: "num" }, formatDuration(project.seconds)),
          h("td", { class: "num" }, `${Math.round(project.share * 100)}%`),
          h("td", { class: "num" }, String(project.mrs_merged)),
          h("td", { class: "num" }, String(project.issues_closed)));
      }))));
    return panel;
  }

  spreadPanel(report) {
    const panel = h("section", { class: "card report-panel", "aria-labelledby": "spread-title" },
      h("div", { class: "panel-head" },
        h("div", {}, h("h3", { id: "spread-title" }, "Knowledge spread · 12 weeks"),
          h("div", { class: "muted small-note" }, "Team members who merged, approved or reviewed MRs per project"))));
    if (report.spread.length === 0) {
      panel.append(h("p", { class: "card-empty" }, "No merge request work in the last 12 weeks."));
      return panel;
    }
    const singles = report.spread.filter((row) => row.single).length;
    if (singles) {
      panel.append(h("p", { class: "spread-warning" }, icon("warning"),
        `${plural(singles, "project")} ${singles === 1 ? "depends" : "depend"} on a single person in this team.`));
    }
    panel.append(h("ul", { class: "spread-list" }, report.spread.map((row) => h("li", { class: row.single ? "is-single" : null },
      h("div", { class: "spread-path" }, row.path),
      h("div", { class: "spread-people" },
        row.single ? h("span", { class: "badge warn" }, icon("warning"), "1 person") : h("span", { class: "badge outline" }, `${row.count} people`),
        h("span", { class: "muted" }, row.people.join(", ")))))));
    return panel;
  }

  peoplePanel(report) {
    if (!report.person_breakdown) {
      return h("p", { class: "muted people-note" }, icon("users"),
        " The per-person breakdown is switched off on this server (TEAMPULSE_TEAM_PERSON_BREAKDOWN). Team totals above are complete.");
    }
    const p = report.period;
    return h("section", { class: "card report-panel", "aria-labelledby": "people-breakdown-title" },
      h("div", { class: "panel-head" },
        h("div", {}, h("h3", { id: "people-breakdown-title" }, "People · alphabetical, not ranked"),
          h("div", { class: "muted small-note" }, "Facts per person to check the totals; an empty time cell usually means no time is logged in GitLab"))),
      h("div", { class: "table-scroll" }, h("table", { class: "people-table" },
        h("thead", {}, h("tr", {}, ["Person", "Time", "Days with time", "MRs merged", "Issues closed", "Approvals", "Review comments"].map((c, i) => h("th", { scope: "col", class: i ? "num" : null }, c)))),
        h("tbody", {}, report.people.map((person) => h("tr", {},
          h("td", {}, person.name, h("span", { class: "muted" }, ` @${person.username}`)),
          h("td", { class: "num" }, person.seconds ? formatDuration(person.seconds) : "—"),
          h("td", { class: "num" }, `${person.days_with_time} / ${p.elapsed_days}`),
          h("td", { class: "num" }, String(person.mrs_merged)),
          h("td", { class: "num" }, String(person.issues_closed)),
          h("td", { class: "num" }, String(person.approvals)),
          h("td", { class: "num" }, String(person.review_comments))))))));
  }

  footer(report) {
    return h("div", { class: "report-footer" },
      h("button", { type: "button", class: "button", id: "copy-digest", onclick: () => this.copyDigest() }, "Copy last week as Markdown"),
      h("details", { class: "counting" },
        h("summary", {}, "How these numbers are counted"),
        h("dl", {}, report.counting.map((row) => [h("dt", {}, row.metric), h("dd", {}, row.rule)]))));
  }

  async copyDigest() {
    try {
      const digest = await api.teamDigest(this.teamId);
      try {
        await navigator.clipboard.writeText(digest.markdown);
        toast(`Copied the digest for ${digest.period.label}.`);
      } catch {
        this.showDigest(digest);
      }
    } catch (error) {
      toast(`Could not build the digest: ${error.message}`, "error");
    }
  }

  showDigest(digest) {
    // Clipboard access can be blocked (plain HTTP on the network): show it for manual copying.
    const area = h("textarea", { class: "digest-text", readonly: true, rows: 16, "aria-label": "Weekly digest" }, digest.markdown);
    this.body.append(h("section", { class: "card report-panel" },
      h("div", { class: "panel-head" }, h("h3", {}, `Digest · ${digest.period.label}`)), area));
    area.focus();
    area.select();
  }
}

class TeamEditor {
  constructor(app, onSaved) {
    this.app = app;
    this.onSaved = onSaved;
    this.root = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-labelledby": "team-editor-title", hidden: true, id: "team-editor" });
    this.root.addEventListener("click", (event) => { if (event.target === this.root) this.close(); });
    this.root.addEventListener("keydown", (event) => { if (event.key === "Escape") this.close(); });
    document.body.append(this.root);
  }

  async open(team) {
    this.team = team;
    this.members = new Set(team ? team.members.map((m) => m.id) : []);
    this.scopes = new Set(team ? team.scopes : []);
    this.filter = "";
    this.error = null;
    this.users = this.app.users?.users || null;
    this.suggestions = { projects: [], namespaces: [] };
    this.root.hidden = false;
    this.build();
    this.name.focus();
    try {
      const [users, suggestions] = await Promise.all([this.users ? null : api.users(), api.projects()]);
      if (users) {
        this.app.users = users;
        this.users = users.users;
      }
      this.suggestions = suggestions;
      this.renderMembers();
      this.renderSuggestions();
    } catch (error) {
      this.showError(`Could not load people: ${error.message}`);
    }
  }

  close() {
    this.root.hidden = true;
    clear(this.root);
  }

  build() {
    this.name = h("input", { class: "input", id: "team-name", required: true, maxlength: 120, value: this.team?.name || "", autocomplete: "off" });
    this.description = h("textarea", { class: "input textarea", id: "team-description", rows: 2, maxlength: 2000 }, this.team?.description || "");
    this.memberList = h("div", { class: "member-picker", role: "group", "aria-label": "Team members" });
    this.memberCount = h("span", { class: "badge accent" });
    this.memberFilter = h("input", {
      class: "input", type: "search", placeholder: "Filter people…", "aria-label": "Filter people", autocomplete: "off",
      oninput: (event) => { this.filter = event.target.value; this.renderMembers(); },
    });
    this.scopeInput = h("input", {
      class: "input", id: "team-scope", list: "team-scope-options", placeholder: "group/project or group", autocomplete: "off",
      onkeydown: (event) => { if (event.key === "Enter") { event.preventDefault(); this.addScope(); } },
    });
    this.scopeOptions = h("datalist", { id: "team-scope-options" });
    this.scopeChips = h("div", { class: "scope-chips" });
    this.errorBox = h("p", { class: "form-error", role: "alert", hidden: true });
    const form = h("form", {
      class: "team-form",
      onsubmit: (event) => { event.preventDefault(); this.save(); },
    },
    h("label", { for: "team-name" }, "Name"), this.name,
    h("label", { for: "team-description" }, "Description ", h("span", { class: "muted" }, "(optional)")), this.description,
    h("div", { class: "form-row" }, h("span", { class: "form-label" }, "Members"), this.memberCount),
    this.memberFilter, this.memberList,
    h("div", { class: "form-row" }, h("label", { for: "team-scope", class: "form-label" }, "Project scope"),
      h("span", { class: "muted" }, "empty = all projects")),
    h("div", { class: "scope-add" }, this.scopeInput, h("button", { type: "button", class: "button small", onclick: () => this.addScope() }, "Add")),
    this.scopeOptions, this.scopeChips,
    h("p", { class: "muted small-note" }, "A namespace includes all projects below it. The report only counts work in the scope."),
    this.errorBox,
    h("div", { class: "form-actions" },
      h("button", { type: "button", class: "button", onclick: () => this.close() }, "Cancel"),
      h("button", { type: "submit", class: "button primary", id: "save-team" }, this.team ? "Save team" : "Create team")));
    this.root.replaceChildren(h("div", { class: "drawer-panel" },
      h("div", { class: "drawer-head" },
        h("h2", { id: "team-editor-title" }, this.team ? `Edit ${this.team.name}` : "New team"),
        h("button", { type: "button", class: "icon-button", title: "Close", onclick: () => this.close() },
          s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: "M6 6l12 12M18 6 6 18" })),
          h("span", { class: "visually-hidden" }, "Close"))),
      h("div", { class: "drawer-body" }, form)));
    this.renderMembers();
    this.renderScopes();
  }

  renderMembers() {
    this.memberCount.textContent = `${this.members.size} selected`;
    clear(this.memberList);
    if (!this.users) {
      this.memberList.append(h("p", { class: "muted" }, "Loading people…"));
      return;
    }
    const needle = this.filter.trim().toLowerCase();
    const users = this.users
      .filter((u) => this.members.has(u.id) || (u.state === "active" && u.account_type === "human"))
      .filter((u) => !needle || u.name.toLowerCase().includes(needle) || u.username.toLowerCase().includes(needle))
      .sort((a, b) => Number(this.members.has(b.id)) - Number(this.members.has(a.id)) || collator.compare(a.name, b.name));
    for (const user of users.slice(0, 200)) {
      const box = h("input", {
        type: "checkbox", class: "checkbox", checked: this.members.has(user.id), id: `member-${user.id}`,
        onchange: (event) => {
          if (event.target.checked) this.members.add(user.id); else this.members.delete(user.id);
          this.memberCount.textContent = `${this.members.size} selected`;
        },
      });
      this.memberList.append(h("label", { class: "member-option", for: `member-${user.id}` },
        box, avatar(user), h("span", {}, user.name, h("span", { class: "muted" }, ` @${user.username}`))));
    }
    if (users.length === 0) this.memberList.append(h("p", { class: "muted" }, "No active people match."));
  }

  renderSuggestions() {
    this.scopeOptions.replaceChildren(...[...this.suggestions.namespaces, ...this.suggestions.projects].map((path) => h("option", { value: path })));
  }

  addScope() {
    const value = this.scopeInput.value.trim().replace(/^\/+|\/+$/g, "");
    if (!value) return;
    this.scopes.add(value);
    this.scopeInput.value = "";
    this.renderScopes();
  }

  renderScopes() {
    this.scopeChips.replaceChildren(...[...this.scopes].sort().map((scope) => h("span", { class: "scope-chip" }, scope,
      h("button", { type: "button", class: "chip-remove", "aria-label": `Remove ${scope}`, onclick: () => { this.scopes.delete(scope); this.renderScopes(); } }, "×"))));
  }

  showError(message) {
    this.errorBox.hidden = false;
    this.errorBox.textContent = message;
  }

  async save() {
    this.addScope();
    const body = {
      name: this.name.value,
      description: this.description.value,
      member_ids: [...this.members],
      scopes: [...this.scopes],
    };
    if (!body.name.trim()) {
      this.showError("Give the team a name.");
      this.name.focus();
      return;
    }
    try {
      const team = this.team ? await api.updateTeam(this.team.id, body) : await api.createTeam(body);
      toast(this.team ? `Team ${team.name} saved.` : `Team ${team.name} created — its history loads in the background.`);
      this.close();
      this.onSaved(team);
    } catch (error) {
      this.showError(error.message);
    }
  }
}
