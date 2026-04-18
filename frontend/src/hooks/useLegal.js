import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchLegalDocuments,
  fetchLegalDocument,
  acceptLegalDocuments,
  fetchMyAcceptances,
  fetchAttributions,
} from "../api/legal";

export function useLegalDocuments() {
  return useQuery({
    queryKey: ["legal", "documents"],
    queryFn: fetchLegalDocuments,
  });
}

export function useLegalDocument(slug) {
  return useQuery({
    queryKey: ["legal", "documents", slug],
    queryFn: () => fetchLegalDocument(slug),
    enabled: !!slug,
  });
}

export function useAcceptLegal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: acceptLegalDocuments,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["legal"] });
    },
  });
}

export function useMyAcceptances() {
  return useQuery({
    queryKey: ["legal", "acceptances"],
    queryFn: fetchMyAcceptances,
  });
}

export function useAttributions() {
  return useQuery({
    queryKey: ["legal", "attributions"],
    queryFn: fetchAttributions,
  });
}
