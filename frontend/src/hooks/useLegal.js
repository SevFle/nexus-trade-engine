import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "../lib/api";

export function useLegalDocuments() {
  return useQuery({
    queryKey: ["legal", "documents"],
    queryFn: () => apiFetch("/api/v1/legal/documents"),
  });
}

export function useLegalDocument(slug) {
  return useQuery({
    queryKey: ["legal", "documents", slug],
    queryFn: () => apiFetch(`/api/v1/legal/documents/${slug}`),
    enabled: !!slug,
  });
}

export function useAcceptances() {
  return useQuery({
    queryKey: ["legal", "acceptances"],
    queryFn: () => apiFetch("/api/v1/legal/acceptances/me"),
  });
}

export function useAcceptLegal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (acceptances) =>
      apiFetch("/api/v1/legal/accept", {
        method: "POST",
        body: JSON.stringify({ acceptances }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["legal"] });
    },
  });
}

export function usePendingDocuments() {
  const { data: documents } = useLegalDocuments();
  const { data: acceptances } = useAcceptances();

  if (!documents || !acceptances) return [];

  const acceptedMap = new Map(
    (acceptances || []).map((a) => [a.document_slug, a.version])
  );

  return (documents || []).filter(
    (doc) => doc.requires_acceptance && acceptedMap.get(doc.slug) !== doc.current_version
  );
}
