// JSON client for the local Team Pulse backend (never GitLab directly).

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(method, path, body) {
  const options = { method, headers: { Accept: "application/json" }, cache: "no-store" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new ApiError("The dashboard server is unreachable", 0, null);
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok && response.status !== 202) {
    const detail = payload && (payload.detail || payload.status);
    throw new ApiError(detail ? String(detail) : `HTTP ${response.status}`, response.status, payload);
  }
  return payload;
}

export const api = {
  status: () => request("GET", "/api/status"),
  users: () => request("GET", "/api/users"),
  dashboard: () => request("GET", "/api/dashboard"),
  errors: (includeResolved) => request("GET", `/api/errors?include_resolved=${includeResolved ? "true" : "false"}`),
  select: (id, selected) => request("PATCH", `/api/users/${encodeURIComponent(id)}/selection`, { selected }),
  refresh: () => request("POST", "/api/refresh"),
};
