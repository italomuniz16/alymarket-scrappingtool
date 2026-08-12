import { Download, Loader2, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { triggerDownload } from "@/lib/api-client";
import type { ExportFormat, ICPCriteria, Lead } from "@/lib/api-types";
import { useExportOne } from "@/lib/queries";
import { cn } from "@/lib/utils";

const FORMATOS: ExportFormat[] = ["csv", "xlsx", "txt"];

export function LeadsTable({
  rows,
  criteria,
  demoMode,
}: {
  rows: Lead[];
  criteria: ICPCriteria;
  demoMode: boolean;
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [formato, setFormato] = useState<ExportFormat>("csv");
  const exportOne = useExportOne();

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(
      (l) =>
        l.razao_social.toLowerCase().includes(q) ||
        (l.nome_fantasia ?? "").toLowerCase().includes(q) ||
        l.id_estab.includes(q),
    );
  }, [rows, query]);

  const selectedLead = filtered.find((l) => l.id_estab === selected) ?? null;

  const handleExport = () => {
    if (!selectedLead) return;
    exportOne.mutate(
      { criteria, id_estab: selectedLead.id_estab, formato, demo: demoMode },
      { onSuccess: (result) => triggerDownload(result.blob, result.filename) },
    );
  };

  return (
    <section aria-labelledby="preview-heading" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="preview-heading" className="text-xl font-semibold">
            Preview
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Selecione uma linha para exportar apenas aquela empresa.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Buscar razão social ou CNPJ"
              className="w-64 pl-9"
              aria-label="Buscar nos leads"
            />
          </div>
        </div>
      </div>

      <div className="surface-panel overflow-hidden">
        <div className="max-h-[26rem] overflow-auto">
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-surface">
              <TableRow>
                <TableHead className="w-10" />
                <TableHead>País</TableHead>
                <TableHead>CNPJ</TableHead>
                <TableHead>Razão social</TableHead>
                <TableHead>Nome fantasia</TableHead>
                <TableHead>Região</TableHead>
                <TableHead>Atividade</TableHead>
                <TableHead>Porte</TableHead>
                <TableHead>Situação</TableHead>
                <TableHead>E-mail</TableHead>
                <TableHead className="text-right">Abertura</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((lead) => {
                const isSelected = selected === lead.id_estab;
                return (
                  <TableRow
                    key={lead.id_estab}
                    onClick={() => setSelected(isSelected ? null : lead.id_estab)}
                    aria-selected={isSelected}
                    className={cn("cursor-pointer", isSelected && "bg-secondary")}
                  >
                    <TableCell>
                      <span
                        className={cn(
                          "block size-2.5 rounded-full border border-border",
                          isSelected && "border-accent bg-accent",
                        )}
                        aria-hidden="true"
                      />
                    </TableCell>
                    <TableCell className="num-tabular text-xs">{lead.pais}</TableCell>
                    <TableCell className="num-tabular text-xs">{lead.id_estab}</TableCell>
                    <TableCell className="max-w-[20rem] truncate font-medium">
                      {lead.razao_social}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {lead.nome_fantasia ?? "—"}
                    </TableCell>
                    <TableCell className="num-tabular text-xs">{lead.regiao ?? "—"}</TableCell>
                    <TableCell className="num-tabular text-xs">
                      {lead.cod_atividade ?? "—"}
                    </TableCell>
                    <TableCell className="text-xs">{lead.porte ?? "—"}</TableCell>
                    <TableCell>
                      <Badge variant={lead.situacao === "ATIVA" ? "secondary" : "outline"}>
                        {lead.situacao ?? "—"}
                      </Badge>
                    </TableCell>
                    <TableCell className="max-w-[14rem] truncate text-muted-foreground">
                      {lead.email ?? "—"}
                    </TableCell>
                    <TableCell className="num-tabular text-right text-xs">
                      {lead.data_inicio_atividade ?? "—"}
                    </TableCell>
                  </TableRow>
                );
              })}
              {filtered.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={11} className="py-10 text-center text-muted-foreground">
                    Nenhuma empresa corresponde à busca.
                  </TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </div>
      </div>

      {selectedLead ? (
        <div className="surface-panel space-y-4 p-5">
          <p className="text-sm">
            <span className="font-medium">Selecionada:</span> {selectedLead.razao_social} ·{" "}
            <span className="num-tabular text-muted-foreground">{selectedLead.id_estab}</span>
          </p>

          {demoMode ? (
            <p className="text-xs text-muted-foreground">
              Exportação desabilitada em modo demonstração.
            </p>
          ) : (
            <div className="flex flex-wrap items-end gap-3">
              <div className="space-y-2">
                <Label>Formato</Label>
                <div className="flex gap-1">
                  {FORMATOS.map((f) => (
                    <Button
                      key={f}
                      type="button"
                      size="sm"
                      variant={formato === f ? "default" : "outline"}
                      onClick={() => setFormato(f)}
                    >
                      {f}
                    </Button>
                  ))}
                </div>
              </div>
              <Button className="gap-2" disabled={exportOne.isPending} onClick={handleExport}>
                {exportOne.isPending ? (
                  <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                ) : (
                  <Download className="size-4" aria-hidden="true" />
                )}
                Exportar esta empresa
              </Button>
            </div>
          )}

          {exportOne.isError ? (
            <p className="text-sm text-destructive">{exportOne.error.message}</p>
          ) : null}
          {exportOne.isSuccess ? (
            exportOne.data.nExported === 0 ? (
              <p className="text-sm text-warning-foreground">
                Empresa suprimida (opt-out/duplicata) — nada exportado.
              </p>
            ) : (
              <p className="text-sm text-success">Exportado: {exportOne.data.filename}</p>
            )
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
