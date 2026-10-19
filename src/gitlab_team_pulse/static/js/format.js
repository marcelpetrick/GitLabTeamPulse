// Formatting helpers for times, durations and identities.

const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const UNITS = [
  ["year", 31536000], ["month", 2592000], ["week", 604800],
  ["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1],
];

export function toDate(value) {
  if (!value) return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function relativeTime(value, now = new Date()) {
  const d = toDate(value);
  if (!d) return "never";
  const seconds = Math.round((d.getTime() - now.getTime()) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 5) return "just now";
  if (abs < 60) return seconds < 0 ? `${abs} seconds ago` : `in ${abs} seconds`;
  for (const [unit, size] of UNITS) {
    if (abs >= size) return rtf.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

const pad = (n) => String(n).padStart(2, "0");

export function exactTime(value) {
  const d = toDate(value);
  if (!d) return "—";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function shortDateTime(value) {
  const d = toDate(value);
  if (!d) return "—";
  return d.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
// Formatters are expensive to create; toLocaleDateString with options builds one per call.
const DATE_UTC = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
const DATE_LOCAL = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric" });
const WEEKDAY_UTC = new Intl.DateTimeFormat("en-GB", { weekday: "short", timeZone: "UTC" });
const DAY_MONTH_UTC = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
const dayLabels = new Map();

export function formatDate(value) {
  if (typeof value === "string" && DATE_ONLY.test(value)) {
    // A calendar day (due date, period bounds) has no time zone: never shift it by the
    // browser's offset (new Date("2026-10-15") is UTC midnight, the 14th west of UTC).
    const [y, m, d] = value.split("-").map(Number);
    return DATE_UTC.format(new Date(Date.UTC(y, m - 1, d)));
  }
  const d = toDate(value);
  if (!d) return "—";
  return DATE_LOCAL.format(d);
}

/** "2026-09-22" -> { weekday: "Tue", day: "22 Sep" } without timezone shifting. */
export function dayLabel(isoDate) {
  let label = dayLabels.get(isoDate);
  if (!label) {
    const [y, m, d] = isoDate.split("-").map(Number);
    const date = new Date(Date.UTC(y, m - 1, d));
    label = Object.freeze({ weekday: WEEKDAY_UTC.format(date), day: DAY_MONTH_UTC.format(date) });
    dayLabels.set(isoDate, label);
  }
  return label;
}

export function formatDuration(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  if (total === 0) return "0m";
  const hours = Math.floor(total / 3600);
  const minutes = Math.round((total % 3600) / 60);
  if (hours === 0) return `${minutes}m`;
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

export function plural(count, word, many = `${word}s`) {
  return `${count} ${count === 1 ? word : many}`;
}

export function initials(name) {
  const parts = String(name || "?").trim().split(/\s+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] || "?").slice(0, 2);
  return letters.toUpperCase();
}

export function colorFor(text) {
  let hash = 0;
  for (const ch of String(text)) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
  return `hsl(${Math.abs(hash) % 360} 45% 48%)`;
}

export function todayIso(timeZone) {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  } catch {
    return new Date().toISOString().slice(0, 10);
  }
}
