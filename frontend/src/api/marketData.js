import { getAccessToken } from "./auth";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function request(path) {
  const headers = { "Content-Type": "application/json" };
  const token = getAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(`${API}${path}`, { headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const message = body.detail || body.message || `Request failed (${res.status})`;
    const err = new Error(message);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return res.json();
}

function buildQuery({ provider, assetClass, period, interval } = {}) {
  const params = new URLSearchParams();
  if (period) params.set("period", period);
  if (interval) params.set("interval", interval);
  if (provider) params.set("provider", provider);
  if (assetClass) params.set("asset_class", assetClass);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export async function getBars(symbol, opts = {}) {
  const trimmed = String(symbol || "").trim();
  if (!trimmed) throw new Error("Symbol is required");
  return request(
    `/api/v1/market-data/${encodeURIComponent(trimmed)}/bars${buildQuery(opts)}`,
  );
}

export async function getQuote(symbol, opts = {}) {
  const trimmed = String(symbol || "").trim();
  if (!trimmed) throw new Error("Symbol is required");
  return request(
    `/api/v1/market-data/${encodeURIComponent(trimmed)}/quote${buildQuery(opts)}`,
  );
}
