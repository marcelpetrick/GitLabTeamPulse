// Shared UI pieces: tooltip, toasts, avatars, live relative times and freshness pills.

import { h, icon } from "./dom.js";
import { colorFor, exactTime, initials, relativeTime } from "./format.js";

const tooltip = () => document.getElementById("tooltip");

export function showTooltip(content, x, y) {
  const tip = tooltip();
  tip.replaceChildren(...content);
  tip.hidden = false;
  const rect = tip.getBoundingClientRect();
  const left = Math.min(x + 14, window.innerWidth - rect.width - 8);
  const top = y + rect.height + 22 > window.innerHeight ? y - rect.height - 12 : y + 14;
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}

export function hideTooltip() {
  tooltip().hidden = true;
}

/** Attach a tooltip to an element for mouse and keyboard users. */
export function bindTooltip(el, build) {
  el.addEventListener("mousemove", (event) => showTooltip(build(), event.clientX, event.clientY));
  el.addEventListener("mouseleave", hideTooltip);
  el.addEventListener("focus", () => {
    const rect = el.getBoundingClientRect();
    showTooltip(build(), rect.left + rect.width / 2, rect.top);
  });
  el.addEventListener("blur", hideTooltip);
}

export function toast(message, kind = "info", timeout = 4200) {
  const node = h("div", { class: `toast ${kind === "error" ? "err" : ""}`, role: kind === "error" ? "alert" : "status" }, message);
  document.getElementById("toasts").append(node);
  setTimeout(() => node.remove(), timeout);
}

export function avatar(user, large = false) {
  const cls = `avatar${large ? " large" : ""}`;
  const fallback = () => {
    const node = h("span", { class: cls, "aria-hidden": "true" }, initials(user.name || user.username));
    node.style.backgroundColor = colorFor(user.username);
    return node;
  };
  if (!user.avatar_url) return fallback();
  const img = h("img", { class: cls, src: user.avatar_url, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
  img.addEventListener("error", () => img.replaceWith(fallback()), { once: true });
  return img;
}

/** A span whose text is kept current by the global ticker ("10 seconds ago"). */
export function rel(ts, prefix = "") {
  const node = h("span", { class: "rel", title: ts ? exactTime(ts) : "never" }, prefix + relativeTime(ts));
  if (ts) {
    node.dataset.ts = ts;
    node.dataset.prefix = prefix;
  }
  return node;
}

export function tickRelativeTimes(root = document) {
  const now = new Date();
  for (const node of root.querySelectorAll(".rel[data-ts]")) {
    node.textContent = (node.dataset.prefix || "") + relativeTime(node.dataset.ts, now);
  }
}

const FRESHNESS_TEXT = {
  fresh: "Updated ",
  stale: "Stale · updated ",
  error: "Refresh failed · data from ",
  refreshing: "Refreshing… data from ",
};

export function freshnessTitle(info) {
  const lines = [
    `Status: ${info.status}`,
    `Last successful refresh: ${exactTime(info.last_success_at)}`,
    `Last attempt: ${exactTime(info.last_attempt_at)}`,
  ];
  if (info.stale) lines.push("Data is older than the expected refresh interval.");
  if (info.message) lines.push(`Problem: ${info.message}`);
  return lines.join("\n");
}

/** Freshness pill: icon + text + color, so meaning never depends on color alone. */
export function freshnessPill(info, { small = false, label = "" } = {}) {
  let status = info.status;
  if (status === "refreshing" && !info.last_success_at) status = "refreshing-first";
  const cls = `pill ${info.status}${small ? " small" : ""}`;
  const pill = h("span", { class: cls, title: freshnessTitle(info), "data-status": info.status });
  if (info.status === "refreshing") pill.append(h("span", { class: "spinner", "aria-hidden": "true" }));
  else if (info.status === "stale" || info.status === "error") pill.append(icon("warning", "status-icon"));
  else if (info.status === "fresh") pill.append(h("span", { class: "dot", "aria-hidden": "true" }));
  else pill.append(icon("clock", "status-icon"));
  if (label) pill.append(h("span", { class: "visually-hidden" }, `${label}: `));
  if (status === "never") pill.append("Not synced yet");
  else if (status === "refreshing-first") pill.append("First sync running…");
  else pill.append(rel(info.last_success_at, FRESHNESS_TEXT[info.status] || ""));
  return pill;
}
