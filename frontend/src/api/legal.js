import { apiFetch } from "./client";

export function fetchLegalDocuments() {
  return apiFetch("/api/v1/legal/documents");
}

export function fetchLegalDocument(slug) {
  return apiFetch(`/api/v1/legal/documents/${slug}`);
}

export function acceptLegalDocuments(acceptances) {
  return apiFetch("/api/v1/legal/accept", {
    method: "POST",
    body: JSON.stringify({ acceptances }),
  });
}

export function fetchMyAcceptances() {
  return apiFetch("/api/v1/legal/acceptances/me");
}

export function fetchAttributions() {
  return apiFetch("/api/v1/legal/attributions");
}
