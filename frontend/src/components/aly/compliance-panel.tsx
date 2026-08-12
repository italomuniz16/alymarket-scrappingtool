import { FileSpreadsheet, FileText, Loader2, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { triggerDownload } from "@/lib/api-client";
import type { ExportFormat, ICPCriteria } from "@/lib/api-types";
import { useCompliance, useExport } from "@/lib/queries";

const checks = [
  {
    title: "Base pública oficial",
    detail: "Dados originados da Receita Federal (CNPJ) e do INSEE (Sirene).",
  },
  {
    title: "Sem dados pessoais sensíveis",
    detail: "Apenas contatos institucionais publicados pelas próprias empresas.",
  },
  {
    title: "Opt-out registrado",
    detail: "Empresas que pedem remoção são bloqueadas em todas as exportações.",
  },
];

const FORMAT_ICONS: Record<ExportFormat, typeof FileSpreadsheet> = {
  csv: FileText,
  xlsx: FileSpreadsheet,
  txt: FileText,
};

export function CompliancePanel({ criteria, demo }: { criteria: ICPCriteria; demo: boolean }) {
  const { data: panel } = useCompliance(criteria);
  const [pendingFormat, setPendingFormat] = useState<ExportFormat | null>(null);
  const exportMutation = useExport();

  const handleExport = (formato: ExportFormat) => {
    setPendingFormat(formato);
    exportMutation.mutate(
      { criteria, formato, demo },
      {
        onSuccess: (result) => triggerDownload(result.blob, result.filename),
        onSettled: () => setPendingFormat(null),
      },
    );
  };

  return (
    <section aria-labelledby="compliance-heading" className="grid gap-4 lg:grid-cols-3">
      <div className="surface-panel p-5 lg:col-span-2">
        <div className="flex items-center gap-2">
          <ShieldCheck className="size-4 text-success" aria-hidden="true" />
          <h2 id="compliance-heading" className="text-sm font-semibold">
            Compliance & LGPD
          </h2>
        </div>
        <ul className="mt-4 grid gap-3 sm:grid-cols-3">
          {checks.map((c) => (
            <li key={c.title} className="rounded-lg bg-secondary p-4">
              <p className="text-sm font-medium">{c.title}</p>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{c.detail}</p>
            </li>
          ))}
        </ul>
        {panel ? (
          <dl className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div>
              <dt className="label-eyebrow">Sintéticos excluídos</dt>
              <dd className="num-tabular text-lg font-semibold">{panel.n_sinteticos}</dd>
            </div>
            <div>
              <dt className="label-eyebrow">Difusão restrita</dt>
              <dd className="num-tabular text-lg font-semibold">{panel.n_difusao_restrita}</dd>
            </div>
            <div>
              <dt className="label-eyebrow">Duplicados</dt>
              <dd className="num-tabular text-lg font-semibold">{panel.n_duplicados}</dd>
            </div>
            <div>
              <dt className="label-eyebrow">Opt-out</dt>
              <dd className="num-tabular text-lg font-semibold">{panel.n_opt_out}</dd>
            </div>
          </dl>
        ) : null}
        {panel ? (
          <p className="mt-3 text-xs text-muted-foreground">
            De {panel.total_bruto} registro(s) no filtro,{" "}
            <span className="font-medium text-foreground">{panel.total_exportavel}</span>{" "}
            ficariam disponíveis para exportação.
          </p>
        ) : null}
      </div>

      <div className="surface-panel flex flex-col gap-3 p-5">
        <h3 className="text-sm font-semibold">Exportar</h3>
        {demo ? (
          <p className="text-xs text-muted-foreground">
            Exportação desabilitada em modo demonstração — desligue o modo para exportar.
          </p>
        ) : (
          <>
            <p className="text-xs text-muted-foreground">
              {panel ? panel.total_exportavel : "—"} empresa(s) no recorte atual, prontas para o
              CRM.
            </p>
            {(["csv", "xlsx", "txt"] as const).map((formato) => {
              const Icon = FORMAT_ICONS[formato];
              const isPending = pendingFormat === formato && exportMutation.isPending;
              return (
                <Button
                  key={formato}
                  variant={formato === "csv" ? "default" : "outline"}
                  className="gap-2"
                  disabled={exportMutation.isPending}
                  onClick={() => handleExport(formato)}
                >
                  {isPending ? (
                    <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                  ) : (
                    <Icon className="size-4" aria-hidden="true" />
                  )}
                  Exportar {formato.toUpperCase()}
                </Button>
              );
            })}
            {exportMutation.isSuccess ? (
              <p className="text-xs text-success">
                {exportMutation.data.nExported} lead(s) exportado(s): {exportMutation.data.filename}
              </p>
            ) : null}
            {exportMutation.isError ? (
              <p className="text-xs text-destructive">{exportMutation.error.message}</p>
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}
