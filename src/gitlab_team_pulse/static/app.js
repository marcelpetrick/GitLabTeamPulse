// GitLab Team Pulse browser app: routing, polling of the local backend, theme and wiring.
// The browser never talks to GitLab; it only asks this server whether cached data changed.

import { api } from "./js/api.js";
import { DashboardView } from "./js/dashboard.js";
import { Diagnostics } from "./js/diagnostics.js";
import { PeopleView } from "./js/people.js";
import { renderStatusbar } from "./js/statusbar.js";
import { tickRelativeTimes, toast } from "./js/ui.js";

const FAST_POLL_MS = 2000;
const VIEWS = ["dashboard", "people"];

class App {
  constructor() {
    this.status = null;
    this.users = null;
    this.dashboard = null;
    this.loadedVersion = { users: -1, dashboard: -1 };
    this.dataVersion = null;
    this.usersVersion = null;
    this.offline = false;
    this.view = null;
    this.pollTimer = null;
    this.peopleView = new PeopleView(document.getElementById("view-people"), this);
    this.dashboardView = new DashboardView(document.getElementById("view-dashboard"), this);
    this.diagnostics = new Diagnostics(this);
  }

  start() {
    for (const tab of document.querySelectorAll(".tab")) {
      tab.addEventListener("click", () => this.navigate(tab.dataset.view));
    }
    document.getElementById("open-diagnostics").addEventListener("click", () => this.openDiagnostics());
    this.setupTheme();
    window.addEventListener("hashchange", () => this.route());
    document.addEventListener("keydown", (event) => {
      const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
      if (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey) {
        event.preventDefault();
        this.navigate("people");
        this.peopleView.focusFilter();
      }
    });
    setInterval(() => tickRelativeTimes(), 1000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) this.pollSoon(); });
    this.poll().then(() => this.route());
  }

  setupTheme() {
    const toggle = document.getElementById("theme-toggle");
    const apply = (theme) => {
      document.documentElement.setAttribute("data-theme", theme);
      toggle.setAttribute("aria-pressed", String(theme === "dark"));
      toggle.title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    };
    apply(document.documentElement.getAttribute("data-theme") || "light");
    toggle.addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      apply(next);
      try { sessionStorage.setItem("teampulse-theme", next); } catch { /* storage may be disabled */ }
    });
  }

  route() {
    const requested = window.location.hash.replace(/^#\/?/, "");
    let view = VIEWS.includes(requested) ? requested : null;
    if (!view) view = this.status && this.status.users.selected === 0 ? "people" : "dashboard";
    this.show(view);
  }

  navigate(view) {
    if (window.location.hash !== `#/${view}`) window.location.hash = `#/${view}`;
    else this.show(view);
  }

  async show(view) {
    this.view = view;
    for (const name of VIEWS) {
      document.getElementById(`view-${name}`).hidden = name !== view;
      const tab = document.getElementById(`tab-${name}`);
      if (name === view) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    document.title = `${view === "people" ? "People" : "Dashboard"} · GitLab Team Pulse`;
    this.renderView();
    await this.loadViewData();
  }

  renderView() {
    if (this.view === "people") this.peopleView.render();
    else if (this.view === "dashboard") this.dashboardView.render();
  }

  async loadViewData(force = false) {
    const key = this.view === "people" ? "users" : "dashboard";
    const version = key === "users" ? this.usersVersion : this.dataVersion;
    if (!force && this.loadedVersion[key] === version && this[key]) return;
    try {
      this[key] = key === "users" ? await api.users() : await api.dashboard();
      this.loadedVersion[key] = version;
      this.renderView();
    } catch (error) {
      toast(`Could not load ${key}: ${error.message}`, "error");
    }
  }

  async poll() {
    clearTimeout(this.pollTimer);
    let next = 20000;
    try {
      const status = await api.status();
      const wasOffline = this.offline;
      this.offline = false;
      this.status = status;
      next = (status.ui_poll_interval_seconds || 20) * 1000;
      const busy = status.crawler.state === "refreshing" || status.crawler.refresh_pending || this.dashboardView.refreshing;
      if (busy) next = FAST_POLL_MS;
      // Each view reloads only when its own version moves: the directory changes hourly, the
      // dashboard data on every sync step.
      const changed = this.view === "people"
        ? this.usersVersion !== status.users_version
        : this.dataVersion !== status.data_version;
      const anyChange = changed || this.dataVersion !== status.data_version;
      this.dataVersion = status.data_version;
      this.usersVersion = status.users_version;
      this.renderChrome();
      this.dashboardView.onStatus(status);
      // Before the first route() there is no view yet; show() loads the right data itself.
      if ((changed || wasOffline) && this.view) await this.loadViewData();
      if (anyChange && this.diagnostics.isOpen) this.diagnostics.load();
      if (!changed && this.view === "dashboard") this.dashboardView.updateRefreshButton();
    } catch {
      this.offline = true;
      next = 5000;
      renderStatusbar(this);
    }
    this.pollTimer = setTimeout(() => this.poll(), next);
  }

  pollSoon() {
    clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => this.poll(), 300);
  }

  renderChrome() {
    renderStatusbar(this);
    const count = document.getElementById("diagnostics-count");
    const unresolved = this.status.errors.unresolved;
    count.hidden = unresolved === 0;
    count.textContent = String(unresolved);
    const peopleCount = document.getElementById("tab-people-count");
    peopleCount.hidden = this.status.users.selected === 0;
    peopleCount.textContent = String(this.status.users.selected);
    if (this.view === "dashboard" && this.dashboard) {
      // Keep the header freshness pill current without refetching the whole dashboard.
      this.dashboard.status = this.status;
    }
  }

  onSelectionChanged() {
    this.loadedVersion.dashboard = -1;
    this.pollSoon();
  }

  openDiagnostics() {
    this.diagnostics.open();
  }
}

window.teamPulse = new App();
window.teamPulse.start();
