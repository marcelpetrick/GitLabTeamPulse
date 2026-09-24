// People view: live filter, sorting and the global (shared) selection.

import { api } from "./api.js";
import { clear, h, icon, safeLink } from "./dom.js";
import { formatDate, plural } from "./format.js";
import { avatar, freshnessPill, toast } from "./ui.js";

const PAGE = 200;
const TYPE_LABELS = { human: "Human", bot: "Bot", service: "Service", unknown: "Unknown" };
const STATE_CLASS = { active: "ok", blocked: "err", banned: "err", deactivated: "warn", ldap_blocked: "err" };
const SORTS = {
  created_desc: ["Newest accounts first", (a, b) => cmpDate(b.created_at, a.created_at) || b.id - a.id],
  created_asc: ["Oldest accounts first", (a, b) => cmpDate(a.created_at, b.created_at) || a.id - b.id],
  name_asc: ["Name A–Z", (a, b) => collator.compare(a.name, b.name)],
  name_desc: ["Name Z–A", (a, b) => collator.compare(b.name, a.name)],
  username_asc: ["Username A–Z", (a, b) => collator.compare(a.username, b.username)],
  username_desc: ["Username Z–A", (a, b) => collator.compare(b.username, a.username)],
};
const collator = new Intl.Collator("en", { sensitivity: "base", numeric: true });

function cmpDate(a, b) {
  return (a ? Date.parse(a) : 0) - (b ? Date.parse(b) : 0);
}

export class PeopleView {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.filter = "";
    this.type = "all";
    this.state = "all";
    this.sort = "created_desc";
    this.selectedOnly = false;
    this.limit = PAGE;
    this.pending = new Set();
    this.built = false;
  }

  build() {
    this.search = h("input", {
      class: "input", type: "search", id: "people-filter", placeholder: "Filter by name or username…",
      autocomplete: "off", spellcheck: "false", "aria-label": "Filter users by name or username",
      oninput: (e) => { this.filter = e.target.value; this.limit = PAGE; this.renderList(); },
    });
    const typeGroup = h("div", { class: "chip-group", role: "group", "aria-label": "Account type" },
      ["all", "human", "bot", "service", "unknown"].map((type) =>
        h("button", {
          type: "button", "aria-pressed": String(type === this.type), dataset: { type },
          onclick: () => {
            this.type = type;
            for (const b of typeGroup.children) b.setAttribute("aria-pressed", String(b.dataset.type === type));
            this.renderList();
          },
        }, type === "all" ? "All" : TYPE_LABELS[type])));
    this.stateSelect = h("select", {
      class: "select", "aria-label": "Account state",
      onchange: (e) => { this.state = e.target.value; this.renderList(); },
    }, h("option", { value: "all" }, "All states"));
    const sortSelect = h("select", {
      class: "select", "aria-label": "Sort users",
      onchange: (e) => { this.sort = e.target.value; this.renderList(); },
    }, Object.entries(SORTS).map(([key, [label]]) => h("option", { value: key, selected: key === this.sort }, label)));
    this.selectedToggle = h("button", {
      type: "button", class: "button small", "aria-pressed": "false",
      onclick: () => {
        this.selectedOnly = !this.selectedOnly;
        this.selectedToggle.setAttribute("aria-pressed", String(this.selectedOnly));
        this.selectedToggle.classList.toggle("primary", this.selectedOnly);
        this.renderList();
      },
    });
    this.count = h("span", { class: "badge accent", id: "selected-count" });
    this.directoryInfo = h("span", { class: "people-directory" });
    this.meta = h("div", { class: "people-meta" });
    this.list = h("div", { class: "people-list", role: "table", "aria-label": "GitLab users" });

    this.root.replaceChildren(
      h("div", { class: "view-head" },
        h("div", {},
          h("h1", { id: "people-title" }, "People"),
          h("div", { class: "subtitle" },
            h("span", { class: "badge info" }, icon("globe"), "Shared selection"),
            "Choices here apply to everyone who opens this dashboard.")),
        h("div", { class: "spacer" }),
        h("button", { type: "button", class: "button primary", onclick: () => this.app.navigate("dashboard") },
          "Open dashboard", icon("arrow"))),
      h("div", { class: "toolbar people-toolbar" },
        h("div", { class: "search" }, icon("search"), this.search),
        typeGroup, this.stateSelect, sortSelect, this.selectedToggle, this.count),
      this.meta,
      this.list,
    );
    this.built = true;
  }

  render() {
    if (!this.built) this.build();
    const data = this.app.users;
    const states = new Set((data?.users || []).map((u) => u.state));
    const current = this.stateSelect.value;
    this.stateSelect.replaceChildren(
      h("option", { value: "all" }, "All states"),
      ...[...states].sort().map((state) => h("option", { value: state, selected: state === current }, state)));
    if (!states.has(current)) this.state = "all";
    this.renderList();
  }

  visibleUsers() {
    const users = this.app.users?.users || [];
    const needle = this.filter.trim().toLowerCase();
    return users
      .filter((u) => this.type === "all" || u.account_type === this.type)
      .filter((u) => this.state === "all" || u.state === this.state)
      .filter((u) => !this.selectedOnly || u.selected)
      .filter((u) => !needle || u.name.toLowerCase().includes(needle) || u.username.toLowerCase().includes(needle) || String(u.id) === needle)
      .sort(SORTS[this.sort][1]);
  }

  renderList() {
    const data = this.app.users;
    const all = data?.users || [];
    const selected = all.filter((u) => u.selected).length;
    this.count.textContent = `${selected} selected`;
    this.selectedToggle.textContent = this.selectedOnly ? "Showing selected" : "Show selected only";
    const users = this.visibleUsers();

    clear(this.meta);
    if (data) {
      this.meta.append(
        h("span", {}, `${plural(users.length, "account")} shown of ${all.length}`),
        h("span", { class: "faint" }, "·"),
        h("span", {}, "Directory "), freshnessPill(data.directory, { small: true, label: "User directory" }),
      );
    }

    clear(this.list);
    if (!data) {
      this.list.append(h("div", { class: "empty" }, h("p", {}, "Loading the user directory…")));
      return;
    }
    if (all.length === 0) {
      this.list.append(h("div", { class: "empty" },
        h("h2", {}, "No users cached yet"),
        h("p", {}, "The directory is loaded from GitLab in the background. Check Diagnostics if this persists.")));
      return;
    }
    this.list.append(h("div", { class: "people-row head", role: "row" },
      h("span", { role: "columnheader" }, h("span", { class: "visually-hidden" }, "Selected")),
      h("span", { role: "columnheader" }, h("span", { class: "visually-hidden" }, "Avatar")),
      h("span", { role: "columnheader" }, "Name"),
      h("span", { role: "columnheader" }, "Type & state"),
      h("span", { role: "columnheader" }, "ID"),
      h("span", { role: "columnheader" }, "Created")));
    if (users.length === 0) {
      this.list.append(h("div", { class: "empty" }, h("p", {}, "No accounts match the current filter.")));
      return;
    }
    for (const user of users.slice(0, this.limit)) this.list.append(this.row(user));
    if (users.length > this.limit) {
      this.list.append(h("div", { class: "show-more" },
        h("button", { type: "button", class: "button small", onclick: () => { this.limit += PAGE; this.renderList(); } },
          `Show ${Math.min(PAGE, users.length - this.limit)} more of ${users.length - this.limit}`)));
    }
  }

  row(user) {
    const checkbox = h("input", {
      type: "checkbox", class: "checkbox", checked: user.selected, disabled: this.pending.has(user.id),
      id: `select-${user.id}`, "aria-label": `Monitor ${user.name} (@${user.username})`,
      onchange: (e) => this.toggle(user, e.target.checked),
    });
    return h("div", { class: `people-row${user.selected ? " is-selected" : ""}`, role: "row", dataset: { userId: user.id } },
      h("span", { class: "select-cell", role: "cell" }, checkbox),
      h("span", { role: "cell" }, avatar(user)),
      h("span", { class: "who", role: "cell" },
        h("div", { class: "name" }, user.name),
        h("div", { class: "username" }, safeLink(user.web_url, `@${user.username}`))),
      h("span", { class: "tags", role: "cell" },
        h("span", { class: `badge ${user.account_type === "human" ? "outline" : "accent"}` },
          user.account_type === "bot" ? icon("bot") : null, TYPE_LABELS[user.account_type] || user.account_type),
        h("span", { class: `badge ${STATE_CLASS[user.state] || ""}` }, user.state)),
      h("span", { class: "num mono", role: "cell" }, `#${user.id}`),
      h("span", { class: "num", role: "cell", title: user.created_at || "" }, formatDate(user.created_at)));
  }

  async toggle(user, selected) {
    this.pending.add(user.id);
    user.selected = selected; // optimistic; filtering never changes the selection
    this.renderList();
    try {
      await api.select(user.id, selected);
      toast(selected ? `${user.name} is now monitored — first sync starts right away.` : `${user.name} is no longer monitored.`);
      this.app.onSelectionChanged();
    } catch (error) {
      user.selected = !selected;
      toast(`Could not update the selection: ${error.message}`, "error");
    } finally {
      this.pending.delete(user.id);
      this.renderList();
    }
  }

  focusFilter() {
    this.search?.focus();
  }
}
