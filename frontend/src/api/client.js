const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

export class ConsentRequiredError extends Error {
  constructor(pendingDocuments) {
    super("Legal consent required");
    this.name = "ConsentRequiredError";
    this.pendingDocuments = pendingDocuments;
  }
}

export async function apiFetch(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
    credentials: "include",
  });

  if (response.status === 451) {
    const body = await response.json().catch(() => ({}));
    const pending = body.detail?.documents || [];
    window.dispatchEvent(
      new CustomEvent("legal:consent-required", { detail: pending })
    );
    throw new ConsentRequiredError(pending);
  }

  if (!response.ok) {
    throw new Error(`API error: ${response.status}`);
  }

  return response.json();
}
