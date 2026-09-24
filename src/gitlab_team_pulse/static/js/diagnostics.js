// Diagnostics drawer: operator-friendly list of persisted backend problems.

import { api } from "./api.js";
import { h, icon } from "./dom.js";
import { exactTime } from "./format.js";
import { rel } from "./ui.js";

export class Diagnostics {
  constructor(app) {
    this.app = app;
    this.drawer = document.getElementById("diagnostics");
    this.body = document.getElementById("diagnostics-body");
    this.includeResolved = false;
    this.opener = null;
    document.getElementById("close-diagnostics").addEventListener("click", () => this.close());
    this.drawer.addEventListener("click", (event) => { if (event.target === this.drawer) this.close(); });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && this.isOpen) this.close(); });
  }

  get isOpen() {
    return !this.drawer.hidden;
  }

  async open() {
    this.opener = document.activeElement;
    this.drawer.hidden = false;
    document.getElementById("close-diagnostics").focus();
    await this.load();
  }

  close() {
    this.drawer.hidden = true;
    if (this.opener && this.opener.focus) this.opener.focus();
  }

  async load() {
    if (!this.isOpen) return;
    let data;
    try {
      data = await api.errors(this.includeResolved);
    } catch (error) {
      this.body.replaceChildren(h("p", { class: "banner err" }, `Could not load diagnostics: ${error.message}`));
      return;
    }
    this.render(data);
  }

  render(data) {
    const status = this.app.status;
    const summary = status ? h("div", { class: "diag-summary" },
      this.box("Crawler", status.crawler.state + (status.crawler.active_trigger ? ` (${status.crawler.active_trigger})` : "")),
      this.box("GitLab upstream", status.upstream.status + (status.upstream.message ? ` — ${status.upstream.message}` : "")),
      this.box("Selected users", status.selected.last_success_at ? [exactTime(status.selected.last_success_at), " (", rel(status.selected.last_success_at), ")"] : "never synced"),
      this.box("User directory", status.directory.last_success_at ? [exactTime(status.directory.last_success_at), " (", rel(status.directory.last_success_at), ")"] : "never synced"),
      this.box("GitLab", status.gitlab.configured ? status.gitlab.url : "not configured"),
      this.box("Accounts", `${status.users.known} known · ${status.users.selected} selected`),
    ) : null;
    const toggle = h("label", { class: "muted" },
      h("input", { type: "checkbox", checked: this.includeResolved, onchange: (e) => { this.includeResolved = e.target.checked; this.load(); } }),
      " Show resolved problems");
    const list = h("ul", { class: "diag-list" });
    if (data.errors.length === 0) {
      list.append(h("li", { class: "banner info" }, icon("check"), h("span", { class: "banner-text" }, "No problems recorded. Synchronization is healthy.")));
    }
    for (const error of data.errors) {
      const cls = ["diag-item", error.severity === "warning" ? "warning" : "", error.resolved_at ? "resolved" : ""].join(" ");
      list.append(h("li", { class: cls },
        h("div", { class: "head" },
          h("span", { class: `badge ${error.resolved_at ? "ok" : error.severity === "warning" ? "warn" : "err"}` },
            error.resolved_at ? icon("check") : icon("warning"), error.resolved_at ? "resolved" : error.severity),
          h("strong", {}, `${error.subsystem} · ${error.operation}`),
          error.user ? h("span", { class: "muted" }, `${error.user.name} (@${error.user.username})`) : null),
        h("div", { class: "msg" }, error.message),
        h("div", { class: "meta" },
          h("span", {}, "Last seen ", h("strong", {}, exactTime(error.occurred_at)), " (", rel(error.occurred_at), ")",
            error.occurrences > 1 ? ` · ${error.occurrences}× since ${exactTime(error.first_occurred_at)}` : ""),
          h("span", {}, error.cached_data_shown
            ? ["Cached data is still shown · last successful sync ", h("strong", {}, exactTime(error.last_success_at))]
            : "No successful data for this dataset yet"),
          error.resolved_at ? h("span", {}, `Resolved ${exactTime(error.resolved_at)}`) : null)));
    }
    this.body.replaceChildren(...[summary, h("div", { class: "panel-head" }, h("h3", {}, "Problems"), toggle), list].filter(Boolean));
  }

  box(key, value) {
    return h("div", { class: "box" }, h("div", { class: "k" }, key), h("div", { class: "v" }, value));
  }
}
