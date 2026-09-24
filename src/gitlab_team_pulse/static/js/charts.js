// Native SVG charts: stacked seven-day activity and daily logged time. Redrawn on resize.

import { h, s } from "./dom.js";
import { dayLabel, formatDuration, plural } from "./format.js";
import { bindTooltip } from "./ui.js";

export const CATEGORIES = [
  ["push", "Pushes"],
  ["comment", "Comments"],
  ["issue", "Issues"],
  ["merge_request", "Merge requests"],
  ["other", "Other"],
];

const HEIGHT = 150;
const PAD = { top: 18, right: 4, bottom: 34, left: 4 };

function niceMax(value, minimum) {
  const target = Math.max(value, minimum);
  const steps = [1, 2, 4, 5, 10, 15, 20, 25, 50, 100, 200, 500, 1000];
  return steps.find((step) => step >= target) || Math.ceil(target / 100) * 100;
}

function observe(container, draw) {
  let lastWidth = 0;
  const render = () => {
    const width = Math.max(220, Math.floor(container.clientWidth || 320));
    if (width === lastWidth) return;
    lastWidth = width;
    container.replaceChildren(draw(width));
  };
  if ("ResizeObserver" in window) new ResizeObserver(render).observe(container);
  requestAnimationFrame(render);
  container.append(draw(Math.max(220, container.clientWidth || 320)));
}

function frame(width, days, today, max, valueOf, labelOf, drawBar, describe, tooltipRows) {
  const svg = s("svg", { width, height: HEIGHT, viewBox: `0 0 ${width} ${HEIGHT}`, role: "img" });
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const slot = (width - PAD.left - PAD.right) / days.length;
  const barW = Math.min(34, slot * 0.62);
  for (const fraction of [0, 0.5, 1]) {
    const y = PAD.top + plotH * (1 - fraction);
    svg.append(s("line", { x1: PAD.left, x2: width - PAD.right, y1: y, y2: y, class: "grid-line" }));
  }
  days.forEach((day, index) => {
    const x = PAD.left + slot * index + (slot - barW) / 2;
    const value = valueOf(day);
    const group = s("g", { class: "bar-group" });
    if (value === 0) {
      group.append(s("rect", { x, y: PAD.top + plotH - 2, width: barW, height: 2, rx: 1, class: "zero-tick" }));
    } else {
      drawBar(group, day, x, barW, plotH, max);
    }
    const labelY = value === 0 ? PAD.top + plotH - 6 : PAD.top + plotH - (value / max) * plotH - 5;
    group.append(s("text", { x: x + barW / 2, y: labelY, "text-anchor": "middle", class: "value-label" }, labelOf(value)));
    const label = dayLabel(day.date);
    const isToday = day.date === today;
    group.append(
      s("text", { x: x + barW / 2, y: HEIGHT - 18, "text-anchor": "middle", class: `axis-label${isToday ? " today" : ""}` }, label.weekday),
      s("text", { x: x + barW / 2, y: HEIGHT - 5, "text-anchor": "middle", class: `axis-label${isToday ? " today" : ""}` }, label.day),
    );
    const hit = s("rect", {
      x: PAD.left + slot * index, y: 0, width: slot, height: HEIGHT, class: "bar-hit",
      tabindex: "0", role: "img", "aria-label": describe(day, label),
    });
    bindTooltip(hit, () => [h("div", { class: "t-title" }, `${label.weekday} ${label.day}${isToday ? " (today)" : ""}`), ...tooltipRows(day)]);
    svg.append(group, hit);
  });
  return svg;
}

export function activityChart(days, today) {
  const container = h("div", { class: "chart" });
  const max = niceMax(Math.max(0, ...days.map((d) => d.total)), 4);
  observe(container, (width) =>
    frame(
      width, days, today, max,
      (day) => day.total,
      (value) => String(value),
      (group, day, x, barW, plotH) => {
        let y = PAD.top + plotH;
        for (const [key] of CATEGORIES) {
          if (!day[key]) continue;
          const barH = (day[key] / max) * plotH;
          y -= barH;
          group.append(s("rect", { x, y, width: barW, height: Math.max(barH - 1, 1), rx: 2, class: `fill-${key}` }));
        }
      },
      (day, label) => {
        const parts = CATEGORIES.filter(([key]) => day[key]).map(([key, name]) => `${day[key]} ${name.toLowerCase()}`);
        return `${label.weekday} ${label.day}: ${plural(day.total, "action")}${parts.length ? ` (${parts.join(", ")})` : ""}`;
      },
      (day) => [
        ...CATEGORIES.map(([key, name]) =>
          h("div", { class: "t-row" }, h("span", {}, h("i", { class: `bg-${key}` }), name), h("strong", {}, String(day[key] || 0)))),
        h("div", { class: "t-row" }, h("span", {}, "Total"), h("strong", {}, String(day.total))),
      ],
    ));
  const legend = h("div", { class: "legend", "aria-hidden": "true" },
    CATEGORIES.map(([key, name]) => h("span", {}, h("i", { class: `bg-${key}` }), name)));
  return h("div", {}, container, legend);
}

export function timeChart(days, today, entriesByDay) {
  const container = h("div", { class: "chart" });
  const maxHours = niceMax(Math.max(0, ...days.map((d) => d.seconds / 3600)), 4);
  const max = maxHours * 3600;
  observe(container, (width) =>
    frame(
      width, days, today, max,
      (day) => day.seconds,
      (value) => (value ? formatDuration(value) : "0"),
      (group, day, x, barW, plotH) => {
        const barH = (day.seconds / max) * plotH;
        group.append(s("rect", { x, y: PAD.top + plotH - barH, width: barW, height: Math.max(barH, 1), rx: 2, class: "fill-time" }));
      },
      (day, label) => `${label.weekday} ${label.day}: ${formatDuration(day.seconds)} logged`,
      (day) => {
        const entries = entriesByDay.get(day.date) || [];
        return [
          h("div", { class: "t-row" }, h("span", {}, "Logged"), h("strong", {}, formatDuration(day.seconds))),
          h("div", { class: "t-row" }, h("span", {}, "Entries"), h("strong", {}, String(entries.length))),
        ];
      },
    ));
  return container;
}
