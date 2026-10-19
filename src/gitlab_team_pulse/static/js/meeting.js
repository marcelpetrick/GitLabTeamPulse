// Meeting summary: the real outcome per person, built only when asked (never on polling).

import { api } from "./api.js";
import { h, icon, safeLink } from "./dom.js";
import { exactTime, formatDuration, plural } from "./format.js";
import { avatar, freshnessPill, rel, toast } from "./ui.js";

const PERIODS = [
  [2, "Yesterday and today", "Stand-up: what happened since yesterday morning"],
  [7, "Last 7 days", "Weekly meeting: the last seven days including today"],
  [14, "Last 14 days", "Sprint review: the last fourteen days including today"],
  [30, "Last 30 days", "Monthly review: the last thirty days including today"],
];
const LEVELS = {
  delivered: ["Delivered", "Issues closed and merge requests merged in the period"],
  unblocked: ["Unblocked others", "No own delivery: merge requests of colleagues this person reviewed got merged, or approvals given"],
  in_progress: ["In progress", "No delivery yet: open work updated or worked on in the period"],
  discussion: ["Discussion", "No delivery or work in progress: issues opened and comments"],
  last_delivery: ["Last delivery", "Nothing in this period: the most recent delivery in the last 90 days"],
  none: ["Nothing in GitLab", "GitLab has no activity of this person in the period (absence or work outside GitLab are possible)"],
};
const REASON_TITLE = "Why this item matters, from GitLab facts (labels, milestone, due date, size, age)";

export class MeetingDialog {
  constructor(app) {
    this.app = app;
    this.dialog = document.getElementById("meeting");
    this.body = document.getElementById("meeting-body");
    this.days = 7;
    this.unchecked = new Set(); // people the user removed from the summary
    this.summary = null;
    this.busy = false;
    document.getElementById("close-meeting").addEventListener("click", () => this.dialog.close());
    this.dialog.addEventListener("click", (event) => { if (event.target === this.dialog) this.dialog.close(); });
  }

  people() {
    return (this.app.dashboard?.cards || []).map((card) => card.user);
  }

  open() {
    this.render();
    if (!this.dialog.open) this.dialog.showModal();
  }

  chosen() {
    return this.people().filter((user) => !this.unchecked.has(user.id));
  }

  async generate() {
    const people = this.chosen();
    if (people.length === 0) {
      toast("Choose at least one person for the summary.", "error");
      return;
    }
    this.busy = true;
    this.render();
    try {
      this.summary = await api.meetingSummary(people.map((user) => user.id), this.days);
    } catch (error) {
      toast(`Could not build the summary: ${error.message}`, "error");
    } finally {
      this.busy = false;
      this.render();
    }
  }

  async copy() {
    if (!this.summary) return;
    try {
      await navigator.clipboard.writeText(this.summary.markdown);
      toast("Copied the meeting summary as Markdown.");
    } catch {
      const area = this.body.querySelector(".meeting-markdown");
      if (area) {
        area.hidden = false;
        area.select();
      }
      toast("The browser blocked the clipboard; the Markdown is shown below to copy by hand.");
    }
  }

  render() {
    const people = this.people();
    const controls = h("div", { class: "meeting-controls" },
      h("label", { class: "meeting-field", title: "Which period the summary covers (local days, today included)" },
        h("span", {}, "Period"),
        h("select", {
          class: "select", id: "meeting-days", "aria-label": "Period of the summary",
          title: "Which period the summary covers (local days, today included)",
          onchange: (event) => { this.days = Number(event.target.value); },
        }, PERIODS.map(([days, label, title]) => h("option", { value: String(days), selected: days === this.days, title }, label)))),
      h("fieldset", { class: "meeting-people", title: "People in the summary; untick someone to leave them out" },
        h("legend", {}, "People"),
        people.length
          ? people.map((user) => h("label", { class: "meeting-person", title: `Include ${user.name} in the summary` },
            h("input", {
              type: "checkbox", checked: !this.unchecked.has(user.id), "data-user": String(user.id),
              title: `Include ${user.name} in the summary`,
              onchange: (event) => { if (event.target.checked) this.unchecked.delete(user.id); else this.unchecked.add(user.id); },
            }), user.name))
          : h("span", { class: "muted" }, "Nobody is monitored on the dashboard.")),
      h("div", { class: "meeting-actions" },
        h("button", {
          type: "button", class: "button primary", id: "meeting-generate", disabled: this.busy || people.length === 0,
          "aria-busy": String(this.busy),
          title: "Build the summary now from the cached GitLab data; it is not updated automatically afterwards",
          onclick: () => this.generate(),
        }, this.busy ? h("span", { class: "spinner", "aria-hidden": "true" }) : icon("refresh"),
        this.busy ? "Building…" : this.summary ? "Regenerate" : "Generate"),
        h("button", {
          type: "button", class: "button", id: "meeting-copy", disabled: !this.summary,
          title: "Copy the summary as Markdown for chat or meeting notes",
          onclick: () => this.copy(),
        }, "Copy as Markdown")));
    this.body.replaceChildren(controls, this.summary ? this.result(this.summary) : this.intro());
  }

  intro() {
    return h("p", { class: "muted meeting-intro" },
      "One crisp line per person about the real outcome: what was delivered, whom it unblocked, what is next and what needs a decision. ",
      "Built on demand from the cached GitLab data with fixed rules (no AI); logged hours are only context.");
  }

  result(summary) {
    return h("div", { class: "meeting-result" },
      h("p", { class: "meeting-meta muted", id: "meeting-meta" },
        h("span", { title: "The period the summary covers" }, summary.period.label), " · ",
        h("span", { title: `Built at ${exactTime(summary.generated_at)}; it is not updated automatically — press Regenerate for newer data` },
          "generated ", rel(summary.generated_at))),
      summary.people.map((person) => this.person(person)),
      h("details", { class: "meeting-rules" },
        h("summary", { title: "How the summary is built and ordered" }, "How it is counted"),
        h("dl", {}, summary.rules.map((rule) => [h("dt", {}, rule.name), h("dd", {}, rule.rule)]))),
      h("textarea", { class: "meeting-markdown digest-text", readonly: true, rows: 12, hidden: true, "aria-label": "Meeting summary as Markdown" }, summary.markdown));
  }

  person(person) {
    const [levelLabel, levelTitle] = LEVELS[person.level] || LEVELS.none;
    const lines = [];
    for (const item of person.delivered) {
      lines.push(this.line("check", "ok", "Delivered", item, [
        item.via.length ? h("span", { class: "muted", title: "Merge request that delivered this issue" }, ` with ${item.via.join(", ")}`) : null,
        item.reasons.map((reason) => h("span", { class: "reason-chip", title: REASON_TITLE }, reason)),
      ]));
    }
    const hidden = person.delivered_total - person.delivered.length;
    if (hidden > 0) lines.push(h("li", { class: "muted", title: "Further delivered items, ordered lower" }, `… and ${hidden} more delivered`));
    for (const row of person.unblocked.merged) {
      lines.push(this.line("users", "accent", "Unblocked", row, [h("span", { class: "muted", title: "Author of the merge request this person reviewed" }, row.author ? ` by ${row.author}, merged` : " merged")]));
    }
    if (person.level !== "in_progress") {
      for (const row of person.progress) lines.push(this.line("clock", "info", "Next", row, [h("span", { class: "muted", title: "Current state of the work" }, ` (${row.status})`)]));
    }
    for (const row of person.risks) {
      lines.push(this.line("warning", row.severity === "error" ? "err" : "warn", "Decide", row,
        [h("span", { class: "risk-text", title: "Why this needs a decision (same rules as Needs attention)" }, `: ${row.reasons.join(", ")}`)]));
    }
    const context = [];
    if (person.unblocked.approvals) context.push(plural(person.unblocked.approvals, "approval"));
    if (person.unblocked.review_comments) context.push(plural(person.unblocked.review_comments, "review comment"));
    if (person.logged_seconds) context.push(`${formatDuration(person.logged_seconds)} logged`);
    return h("section", { class: "meeting-person-block", dataset: { user: String(person.user.id), level: person.level } },
      h("header", { class: "meeting-person-head" },
        avatar(person.user),
        h("strong", { title: `@${person.user.username}` }, person.user.name),
        h("span", { class: `badge level-${person.level}`, title: levelTitle }, levelLabel),
        freshnessPill(person.freshness, { small: true, label: `${person.user.name}'s data` })),
      h("p", { class: "meeting-headline", title: "The headline: the highest level that has GitLab facts" }, person.headline),
      lines.length ? h("ul", { class: "meeting-lines" }, lines) : null,
      context.length ? h("p", { class: "meeting-context muted", title: "Context only, not the result: reviews given and time logged in GitLab" }, `Context: ${context.join(" · ")}`) : null);
  }

  line(iconName, tone, label, row, extra) {
    return h("li", { class: `meeting-line tone-${tone}` },
      h("span", { class: "meeting-tag", title: LINE_TITLES[label] }, icon(iconName), label),
      safeLink(row.web_url, row.reference, { class: "mono", title: `Open ${row.reference} in GitLab (new tab)` }),
      " ", h("span", { class: "meeting-title" }, row.title), extra);
  }
}

const LINE_TITLES = {
  Delivered: "Closed issue or merged merge request in the period",
  Unblocked: "A colleague's merge request this person reviewed, merged in the period",
  Next: "Open work updated or worked on in the period",
  Decide: "Open work that is overdue, stalled, cannot be merged or waits for a review",
};
