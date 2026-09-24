// Persistent footer: crawler state, exact + relative freshness, next refresh and problems.

import { h, icon } from "./dom.js";
import { exactTime } from "./format.js";
import { rel } from "./ui.js";

export function renderStatusbar(app) {
  const left = document.getElementById("status-left");
  const right = document.getElementById("status-right");
  const status = app.status;
  if (app.offline) {
    left.replaceChildren(h("span", { class: "status-item err", role: "status" }, icon("warning"),
      h("strong", {}, "Dashboard server unreachable"), " — showing the last loaded data, retrying…"));
    return;
  }
  if (!status) return;
  const crawler = status.crawler.state;
  const selected = status.selected;
  const crawlerItem = h("span", { class: `status-item ${crawler === "error" ? "err" : crawler}`, "data-crawler": crawler });
  if (crawler === "refreshing") crawlerItem.append(h("span", { class: "spinner", "aria-hidden": "true" }), h("strong", {}, "Refreshing…"));
  else if (crawler === "error") crawlerItem.append(icon("warning"), h("strong", {}, "Error"));
  else crawlerItem.append(icon("check"), h("strong", {}, "Idle"));

  const last = h("span", { class: `status-item${selected.stale ? " warn" : ""}`, title: `Last successful selected-user refresh: ${exactTime(selected.last_success_at)}` });
  if (selected.stale) last.append(icon("warning"), "Stale · ");
  last.append("Last update ");
  if (selected.last_success_at) {
    last.append(h("strong", {}, exactTime(selected.last_success_at)), h("span", {}, "(", rel(selected.last_success_at), ")"));
  }
  else last.append(h("strong", {}, "never"));

  const attempt = h("span", { class: "status-item", title: exactTime(selected.last_attempt_at) }, "Last attempt ", rel(selected.last_attempt_at));
  const next = status.next_selected_refresh_at
    ? h("span", { class: "status-item", title: exactTime(status.next_selected_refresh_at) }, "Next ", rel(status.next_selected_refresh_at))
    : null;
  left.replaceChildren(crawlerItem, last, attempt, ...(next ? [next] : []));

  const unresolved = status.errors.unresolved;
  const problems = h("button", {
    type: "button", class: `link-button status-item${unresolved ? " err" : ""}`, id: "status-problems",
    onclick: () => app.openDiagnostics(),
  }, unresolved ? [icon("warning"), `${unresolved} ${unresolved === 1 ? "problem" : "problems"}`] : [icon("check"), "No problems"]);
  right.replaceChildren(
    h("span", { class: "status-item", title: `Directory: ${exactTime(status.directory.last_success_at)}` }, "Directory ", rel(status.directory.last_success_at)),
    problems,
    h("span", { class: "status-item faint" }, `v${status.version}`),
  );
}
