// Thin fetch wrapper. The key lives in sessionStorage (this tab only) and is sent as a bearer token.

const KEY = "dashboard_api_key";

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export const session = {
  get key() { return sessionStorage.getItem(KEY); },
  set key(value) { if (value) sessionStorage.setItem(KEY, value); else sessionStorage.removeItem(KEY); },
};

export async function api(path, options = {}) {
  const headers = { Authorization: "Bearer " + session.key, ...(options.headers || {}) };
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  if (response.status === 401) throw new ApiError("unauthorized", 401);
  if (!response.ok) {
    let detail = "";
    try { const body = await response.json(); detail = body.detail || body.title || ""; } catch (e) { /* not json */ }
    throw new ApiError("HTTP " + response.status + (detail ? ": " + detail : ""), response.status);
  }
  return response.json();
}

// A section that is allowed to be unavailable (e.g. no portfolio snapshot yet) resolves to null.
export async function optional(path) {
  try { return await api(path); } catch (e) { if (e.status === 401) throw e; return null; }
}

export async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

export async function ready() {
  try { return await (await fetch("/v1/health/ready", { cache: "no-store" })).json(); } catch (e) { return null; }
}
