/**
 * Hooks de `@tanstack/react-query` sobre `api-client.ts` -- um por endpoint de
 * `src/api/app.py`. Leituras (`useQuery`) vs. ações com efeito colateral
 * (`useMutation`, nunca automáticas): `useExportOne`/`useExport` gravam no
 * audit_log a cada chamada (ver `dashboard/data.py`), então só disparam no clique.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetchBlob, apiFetchJson } from "@/lib/api-client";
import type {
  ChartsResponse,
  ComplianceResponse,
  ExportFormat,
  ICPCriteria,
  IngestResponse,
  PreviewResponse,
  SchedulerStatusRow,
} from "@/lib/api-types";

export function useSchedulerStatus() {
  return useQuery({
    queryKey: ["scheduler-status"],
    queryFn: () => apiFetchJson<SchedulerStatusRow[]>("/api/scheduler-status"),
  });
}

export function usePreview(criteria: ICPCriteria, demo: boolean, limit: number) {
  return useQuery({
    queryKey: ["preview", criteria, demo, limit],
    queryFn: () =>
      apiFetchJson<PreviewResponse>("/api/preview", {
        method: "POST",
        body: JSON.stringify({ criteria, demo, limit }),
      }),
  });
}

export function useCharts(criteria: ICPCriteria, demo: boolean) {
  return useQuery({
    queryKey: ["charts", criteria, demo],
    queryFn: () =>
      apiFetchJson<ChartsResponse>("/api/charts", {
        method: "POST",
        body: JSON.stringify({ criteria, demo }),
      }),
  });
}

export function useCompliance(criteria: ICPCriteria) {
  return useQuery({
    queryKey: ["compliance", criteria],
    queryFn: () =>
      apiFetchJson<ComplianceResponse>("/api/compliance", {
        method: "POST",
        body: JSON.stringify({ criteria }),
      }),
  });
}

export function useIngest() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { n: number }) =>
      apiFetchJson<IngestResponse>("/api/ingest", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    // Equivalente ao st.rerun() do dashboard Streamlit após ingestão bem-sucedida:
    // preview/charts/compliance dependiam da versão do warehouse que acabou de mudar.
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["scheduler-status"] });
      void queryClient.invalidateQueries({ queryKey: ["preview"] });
      void queryClient.invalidateQueries({ queryKey: ["charts"] });
      void queryClient.invalidateQueries({ queryKey: ["compliance"] });
    },
  });
}

interface ExportBody {
  criteria: ICPCriteria;
  formato: ExportFormat;
  demo: boolean;
  usuario?: string | null;
}

export function useExport() {
  return useMutation({
    mutationFn: (body: ExportBody) =>
      apiFetchBlob("/api/export", { method: "POST", body: JSON.stringify(body) }),
  });
}

export function useExportOne() {
  return useMutation({
    mutationFn: (body: ExportBody & { id_estab: string }) =>
      apiFetchBlob("/api/export-one", { method: "POST", body: JSON.stringify(body) }),
  });
}
