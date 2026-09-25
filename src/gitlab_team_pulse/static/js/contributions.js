// Contribution calendar: GitLab-style 2D heatmap (SVG) and a rotatable, zoomable 3D skyline
// (plain canvas, no libraries). Both render the same 53-week grid of daily counts.

import { h, s } from "./dom.js";
import { plural } from "./format.js";
import { hideTooltip, showTooltip } from "./ui.js";

const CELL = 11;
const GAP = 3;
const STEP = CELL + GAP;
const LEFT = 30;
const TOP = 18;
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** GitLab's intensity buckets: 0, 1–9, 10–19, 20–29, 30+. */
export function level(count) {
  if (count <= 0) return 0;
  if (count < 10) return 1;
  if (count < 20) return 2;
  if (count < 30) return 3;
  return 4;
}

function parseDay(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

function addDays(date, days) {
  return new Date(date.getTime() + days * 86400000);
}

function dayText(date) {
  return date.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

/** Grid cells: week column, weekday row (0 = Sunday), date and count. */
export function cells(calendar) {
  const start = parseDay(calendar.start);
  return calendar.counts.map((count, index) => {
    const date = addDays(start, index);
    return { week: Math.floor(index / 7), weekday: date.getUTCDay(), date, count };
  });
}

function tooltipFor(cell) {
  return [h("div", { class: "t-title" }, dayText(cell.date)), h("div", {}, plural(cell.count, "contribution"))];
}

// ------------------------------------------------------------------ 2D heatmap

export function heatmap(calendar) {
  const grid = cells(calendar);
  const weeks = grid.length ? grid[grid.length - 1].week + 1 : 0;
  const width = LEFT + weeks * STEP;
  const height = TOP + 7 * STEP;
  const svg = s("svg", {
    class: "heatmap", viewBox: `0 0 ${width} ${height}`, role: "img", tabindex: "0",
    "aria-label": `${plural(calendar.total, "contribution")} in the last year. Use the arrow keys to inspect days.`,
  });
  let lastMonth = -1;
  for (const cell of grid) {
    if (cell.weekday === 0 && cell.date.getUTCMonth() !== lastMonth && cell.date.getUTCDate() <= 7) {
      lastMonth = cell.date.getUTCMonth();
      svg.append(s("text", { x: LEFT + cell.week * STEP, y: TOP - 6, class: "heat-label" },
        cell.date.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" })));
    }
  }
  for (const row of [1, 3, 5]) {
    svg.append(s("text", { x: 0, y: TOP + row * STEP + CELL - 2, class: "heat-label" }, WEEKDAYS[row]));
  }
  const rects = grid.map((cell) => {
    const rect = s("rect", {
      x: LEFT + cell.week * STEP, y: TOP + cell.weekday * STEP, width: CELL, height: CELL, rx: 2,
      class: `heat-cell lvl-${level(cell.count)}`, "data-date": cell.date.toISOString().slice(0, 10), "data-count": cell.count,
    });
    rect.addEventListener("mousemove", (event) => showTooltip(tooltipFor(cell), event.clientX, event.clientY));
    rect.addEventListener("mouseleave", hideTooltip);
    svg.append(rect);
    return rect;
  });
  // Keyboard: arrows move a highlighted day (roving focus without 371 tab stops).
  let active = grid.length - 1;
  const highlight = () => {
    rects.forEach((rect, index) => rect.classList.toggle("is-active", index === active));
    const box = rects[active].getBoundingClientRect();
    showTooltip(tooltipFor(grid[active]), box.left + box.width / 2, box.top);
  };
  svg.addEventListener("focus", () => grid.length && highlight());
  svg.addEventListener("blur", () => { rects.forEach((rect) => rect.classList.remove("is-active")); hideTooltip(); });
  svg.addEventListener("keydown", (event) => {
    const moves = { ArrowLeft: -7, ArrowRight: 7, ArrowUp: -1, ArrowDown: 1, Home: -Infinity, End: Infinity };
    if (!(event.key in moves) || !grid.length) return;
    event.preventDefault();
    active = Math.min(grid.length - 1, Math.max(0, active + moves[event.key]));
    highlight();
  });
  return svg;
}

export function legend() {
  return h("div", { class: "heat-legend", "aria-hidden": "true" },
    "Less", [0, 1, 2, 3, 4].map((lvl) => h("i", { class: `lvl-${lvl}` })), "More");
}

// ------------------------------------------------------------------ 3D skyline

const DEFAULT_VIEW = { azimuth: -0.34, elevation: 1.02, zoom: 1 };

function css(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function shade(hex, factor) {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!match) return hex;
  const n = parseInt(match[1], 16);
  const channel = (shift) => Math.max(0, Math.min(255, Math.round(((n >> shift) & 255) * factor)));
  return `rgb(${channel(16)} ${channel(8)} ${channel(0)})`;
}

function pointInPolygon(x, y, points) {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const [xi, yi] = points[i];
    const [xj, yj] = points[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/**
 * Orthographic projection of axis-aligned boxes on a grid, drawn back to front (painter's
 * algorithm). Rotation is around the vertical axis, tilt changes the viewing elevation.
 */
export class Skyline {
  constructor(calendar) {
    this.grid = cells(calendar);
    this.max = Math.max(1, calendar.max || 0);
    this.weeks = this.grid.length ? this.grid[this.grid.length - 1].week + 1 : 0;
    this.view = { ...DEFAULT_VIEW };
    this.faces = [];
    this.pointers = new Map();
    this.canvas = h("canvas", {
      class: "skyline-canvas", tabindex: "0", role: "img",
      "aria-label": `3D skyline of ${plural(calendar.total, "contribution")}. Drag or use the arrow keys to rotate, the mouse wheel or plus and minus to zoom, 0 to reset.`,
    });
    this.resetButton = h("button", { type: "button", class: "button small", onclick: () => this.reset() }, "Reset view");
    this.element = h("div", { class: "skyline" }, this.canvas,
      h("div", { class: "skyline-controls" },
        h("span", { class: "muted" }, "Drag to rotate · wheel or pinch to zoom · arrow keys, + / −, 0"), this.resetButton));
    this.bind();
    if ("ResizeObserver" in window) new ResizeObserver(() => this.draw()).observe(this.canvas);
    requestAnimationFrame(() => this.draw());
  }

  reset() {
    this.view = { ...DEFAULT_VIEW };
    this.draw();
  }

  rotate(dAzimuth, dElevation) {
    this.view.azimuth += dAzimuth;
    this.view.elevation = Math.min(1.45, Math.max(0.25, this.view.elevation + dElevation));
    this.draw();
  }

  zoomBy(factor) {
    this.view.zoom = Math.min(4, Math.max(0.4, this.view.zoom * factor));
    this.draw();
  }

  bind() {
    const c = this.canvas;
    c.addEventListener("pointerdown", (event) => {
      c.setPointerCapture(event.pointerId);
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      hideTooltip();
    });
    c.addEventListener("pointermove", (event) => {
      const previous = this.pointers.get(event.pointerId);
      if (!previous) {
        this.hover(event);
        return;
      }
      if (this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        const before = Math.hypot(a.x - b.x, a.y - b.y);
        this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
        const [a2, b2] = [...this.pointers.values()];
        const after = Math.hypot(a2.x - b2.x, a2.y - b2.y);
        if (before > 0) this.zoomBy(after / before);
        return;
      }
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      this.rotate((event.clientX - previous.x) * 0.01, (event.clientY - previous.y) * 0.006);
    });
    const release = (event) => this.pointers.delete(event.pointerId);
    c.addEventListener("pointerup", release);
    c.addEventListener("pointercancel", release);
    c.addEventListener("pointerleave", hideTooltip);
    c.addEventListener("wheel", (event) => {
      event.preventDefault();
      this.zoomBy(Math.exp(-event.deltaY * 0.0015));
    }, { passive: false });
    c.addEventListener("keydown", (event) => {
      const actions = {
        ArrowLeft: () => this.rotate(-0.12, 0), ArrowRight: () => this.rotate(0.12, 0),
        ArrowUp: () => this.rotate(0, 0.08), ArrowDown: () => this.rotate(0, -0.08),
        "+": () => this.zoomBy(1.15), "=": () => this.zoomBy(1.15), "-": () => this.zoomBy(1 / 1.15),
        0: () => this.reset(),
      };
      if (!(event.key in actions)) return;
      event.preventDefault();
      actions[event.key]();
    });
  }

  hover(event) {
    const rect = this.canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    for (let i = this.faces.length - 1; i >= 0; i--) {
      if (pointInPolygon(x, y, this.faces[i].points)) {
        showTooltip(tooltipFor(this.faces[i].cell), event.clientX, event.clientY);
        return;
      }
    }
    hideTooltip();
  }

  draw() {
    const canvas = this.canvas;
    const width = Math.max(260, canvas.clientWidth || 600);
    const height = Math.max(260, Math.round(width * 0.45));
    const ratio = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      canvas.style.height = `${height}px`;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const { azimuth, elevation, zoom } = this.view;
    const cos = Math.cos(azimuth);
    const sin = Math.sin(azimuth);
    const sinE = Math.sin(elevation);
    const cosE = Math.cos(elevation);
    const maxHeight = 7.5;
    const halfW = this.weeks / 2;
    const toView = (x, y, z) => {
      const xr = x * cos - y * sin;
      const yr = x * sin + y * cos;
      return [xr, yr * sinE - z * cosE];
    };
    // Fit the whole scene (plate plus the tallest possible building) for the current angle,
    // then apply the user's zoom on top.
    const extremes = [];
    for (const x of [-halfW - 0.6, halfW + 0.6]) {
      for (const y of [-4.1, 4.1]) {
        for (const z of [0, maxHeight + 0.25]) extremes.push(toView(x, y, z));
      }
    }
    const xs = extremes.map(([x]) => x);
    const ys = extremes.map(([, y]) => y);
    const fit = Math.min((width - 24) / (Math.max(...xs) - Math.min(...xs)), (height - 24) / (Math.max(...ys) - Math.min(...ys)));
    const scale = fit * zoom;
    const cx = width / 2 - ((Math.max(...xs) + Math.min(...xs)) / 2) * scale;
    const cy = height / 2 - ((Math.max(...ys) + Math.min(...ys)) / 2) * scale;
    const project = (x, y, z) => {
      const [vx, vy] = toView(x, y, z);
      return [cx + vx * scale, cy + vy * scale];
    };
    const depthOf = (x, y) => x * sin + y * cos;

    const colors = [0, 1, 2, 3, 4].map((lvl) => css(`--heat-${lvl}`, ["#ebedf0", "#acd5f2", "#7fa8c9", "#527ba0", "#254e77"][lvl]));

    // Ground plate.
    const plate = [[-halfW - 0.6, -4.1], [halfW + 0.6, -4.1], [halfW + 0.6, 4.1], [-halfW - 0.6, 4.1]].map(([x, y]) => project(x, y, 0));
    ctx.fillStyle = css("--surface-2", "#f2f4f7");
    ctx.strokeStyle = css("--border", "#e1e5ea");
    ctx.beginPath();
    plate.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    const boxes = this.grid.map((cell) => {
      const x = cell.week - halfW + 0.5;
      const y = cell.weekday - 3;
      const z = cell.count > 0 ? 0.25 + (cell.count / this.max) * maxHeight : 0.06;
      return { cell, x, y, z, depth: depthOf(x, y) };
    }).sort((a, b) => a.depth - b.depth);

    const half = 0.38;
    const sides = [
      { corners: [[half, -half], [half, half]], normal: [1, 0] },
      { corners: [[-half, half], [-half, -half]], normal: [-1, 0] },
      { corners: [[half, half], [-half, half]], normal: [0, 1] },
      { corners: [[-half, -half], [half, -half]], normal: [0, -1] },
    ];
    this.faces = [];
    for (const box of boxes) {
      const base = colors[level(box.cell.count)];
      for (const side of sides) {
        // Visible when the rotated outward normal points toward the viewer.
        if (depthOf(side.normal[0], side.normal[1]) <= 0) continue;
        const [[ax, ay], [bx, by]] = side.corners;
        const quad = [
          project(box.x + ax, box.y + ay, 0), project(box.x + bx, box.y + by, 0),
          project(box.x + bx, box.y + by, box.z), project(box.x + ax, box.y + ay, box.z),
        ];
        const light = 0.62 + 0.18 * (side.normal[0] * cos - side.normal[1] * sin);
        ctx.fillStyle = shade(base, light);
        ctx.beginPath();
        quad.forEach(([px, py], i) => (i ? ctx.lineTo(px, py) : ctx.moveTo(px, py)));
        ctx.closePath();
        ctx.fill();
      }
      const top = [[-half, -half], [half, -half], [half, half], [-half, half]].map(([dx, dy]) => project(box.x + dx, box.y + dy, box.z));
      ctx.fillStyle = base;
      ctx.beginPath();
      top.forEach(([px, py], i) => (i ? ctx.lineTo(px, py) : ctx.moveTo(px, py)));
      ctx.closePath();
      ctx.fill();
      this.faces.push({ cell: box.cell, points: top });
    }
  }
}
