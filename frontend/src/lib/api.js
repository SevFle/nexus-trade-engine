const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

export async function apiFetch(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (res.status === 451) {
    const pending = await res.json();
    const error = new Error("LEGAL_CONSENT_REQUIRED");
    error.status = 451;
    error.pendingDocuments = pending.documents || [];
    throw error;
  }
  if (!res.ok) {
    const error = new Error(res.statusText);
    error.status = res.status;
    throw error;
  }
  return res.json();
}
