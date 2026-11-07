// JSON client for the local Team Pulse backend (never GitLab directly).

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

// A request stuck on a half-open connection (laptop sleep, VPN switch) must not freeze polling.
const TIMEOUT_MS = 20000;

/** FastAPI sends validation errors as a list of {loc, msg}; anything else as text. */
export function describeDetail(detail) {
  if (Array.isArray(detail)) {
    return detail.map((d) => (d && typeof d === "object" ? d.msg || JSON.stringify(d) : String(d))).join("; ");
  }
  return typeof detail === "object" && detail !== null ? JSON.stringify(detail) : String(detail);
}

async function request(method, path, body) {
  const options = { method, headers: { Accept: "application/json" }, cache: "no-store" };
  if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) options.signal = AbortSignal.timeout(TIMEOUT_MS);
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    const timedOut = error?.name === "TimeoutError" || error?.name === "AbortError";
    throw new ApiError(timedOut ? "The dashboard server did not answer in time" : "The dashboard server is unreachable", 0, null);
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok && response.status !== 202) {
    const detail = payload && (payload.detail || payload.status);
    throw new ApiError(detail ? describeDetail(detail) : `HTTP ${response.status}`, response.status, payload);
  }
  return payload;
}

export const api = {
  status: () => request("GET", "/api/status"),
  about: () => request("GET", "/api/about"),
  users: () => request("GET", "/api/users"),
  dashboard: () => request("GET", "/api/dashboard"),
  errors: (includeResolved) => request("GET", `/api/errors?include_resolved=${includeResolved ? "true" : "false"}`),
  select: (id, selected) => request("PATCH", `/api/users/${encodeURIComponent(id)}/selection`, { selected }),
  refresh: () => request("POST", "/api/refresh"),
  teams: () => request("GET", "/api/teams"),
  createTeam: (team) => request("POST", "/api/teams", team),
  updateTeam: (id, team) => request("PUT", `/api/teams/${encodeURIComponent(id)}`, team),
  deleteTeam: (id) => request("DELETE", `/api/teams/${encodeURIComponent(id)}`),
  applyTeam: (id) => request("POST", `/api/teams/${encodeURIComponent(id)}/apply`),
  teamReport: (id, period, offset) => request("GET", `/api/teams/${encodeURIComponent(id)}/report?period=${encodeURIComponent(period)}&offset=${encodeURIComponent(offset)}`),
  teamDigest: (id) => request("GET", `/api/teams/${encodeURIComponent(id)}/digest`),
  projects: () => request("GET", "/api/projects"),
  meetingSummary: (userIds, days) => request("GET", `/api/meeting-summary?user_ids=${encodeURIComponent(userIds.join(","))}&days=${encodeURIComponent(days)}`),
};
