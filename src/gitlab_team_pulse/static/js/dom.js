// Tiny DOM helpers. All GitLab-originated text goes through textContent, never innerHTML.

const SVG_NS = "http://www.w3.org/2000/svg";

function applyAttrs(el, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.setAttribute("class", value);
    else if (key === "text") el.textContent = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

const ICONS = {
  push: ["M12 19V5", "M5 12l7-7 7 7"],
  comment: ["M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12Z"],
  issue: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M12 8v5", "M12 16v.01"],
  merge_request: ["M6 3v12", "M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z", "M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z", "M18 9a9 9 0 0 1-9 9"],
  other: ["M5 12h.01", "M12 12h.01", "M19 12h.01"],
  warning: ["M12 3 2 20h20L12 3Z", "M12 10v4", "M12 17.5v.01"],
  check: ["M5 12.5l4.5 4.5L19 7"],
  clock: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M12 7v5l3 2"],
  refresh: ["M20 11a8 8 0 0 0-14.8-4.2L4 8", "M4 4v4h4", "M4 13a8 8 0 0 0 14.8 4.2L20 16", "M20 20v-4h-4"],
  search: ["M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z", "M21 21l-4.3-4.3"],
  users: ["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2", "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M22 21v-2a4 4 0 0 0-3-3.9", "M16 3.1a4 4 0 0 1 0 7.8"],
  globe: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M3 12h18", "M12 3a14 14 0 0 1 0 18", "M12 3a14 14 0 0 0 0 18"],
  arrow: ["M5 12h14", "M13 6l6 6-6 6"],
  external: ["M14 4h6v6", "M10 14 20 4", "M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"],
  bot: ["M5 9h14v10H5z", "M12 5v4", "M9 14h.01", "M15 14h.01"],
};

export function icon(name, cls = "") {
  return s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", class: cls || null },
    ...(ICONS[name] || ICONS.other).map((d) => s("path", { d })));
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

/** Only render links that the server already validated as http(s) GitLab URLs. */
export function safeLink(url, children, attrs = {}) {
  if (typeof url !== "string" || !/^https?:\/\//.test(url)) return h("span", attrs, children);
  return h("a", { href: url, target: "_blank", rel: "noopener noreferrer", ...attrs }, children);
}
