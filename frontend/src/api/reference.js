import { apiFetch } from "./client";

/**
 * Fetch typeahead suggestions from the reference search index.
 *
 * Backend: GET /api/v1/reference/suggest?q=<query>&limit=<n>
 * Returns { suggestions: Array<{symbol, name, display, completion, score, record}> }.
 *
 * @param {string} query - User-typed prefix (ticker or company name fragment)
 * @param {{ limit?: number, signal?: AbortSignal }} [opts]
 * @returns {Promise<Array<{symbol: string, name: string, display: string}>>}
 */
export async function getSuggestions(query, opts = {}) {
  const { limit = 5, signal } = opts;
  const trimmed = String(query || "").trim();
  if (!trimmed) return [];
  const params = new URLSearchParams({ q: trimmed, limit: String(limit) });
  const body = await apiFetch(`/api/v1/reference/suggest?${params}`, { signal });
  return Array.isArray(body?.suggestions) ? body.suggestions : [];
}
