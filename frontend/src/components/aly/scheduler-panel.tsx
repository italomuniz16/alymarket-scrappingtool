import { ChevronRight, Clock, Loader2, Play, Radio } from "lucide-react";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useIngest, useSchedulerStatus } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** `"2026-08-05T03:00:12.345678+00:00"` -> `"2026-08-05 03:00:12"` -- mesma regra
 * de `dashboard/app.py::_format_timestamp` (mais legível no painel; `null` vira "—"). */
function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  return value.slice(0, 19).replace("T", " ");
}

export function SchedulerPanel({ noActiveVersion }: { noActiveVersion: boolean }) {
  const [open, setOpen] = useState(false);
  const [n, setN] = useState(40);
  const { data: sources, isLoading } = useSchedulerStatus();
  const ingest = useIngest();

  // Abre sozinho só quando confirma que não há versão ativa (409 do /api/preview)
  // -- não na primeira renderização, pra não abrir/fechar sozinho antes da API
  // responder (ver App.tsx).
  useEffect(() => {
    if (noActiveVersion) setOpen(true);
  }, [noActiveVersion]);

  return (
    <section aria-labelledby="scheduler-heading" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="scheduler-heading" className="text-xl font-semibold">
            Scheduler
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">Última rodada por fonte de dados.</p>
        </div>
        <Badge variant="outline" className="gap-1.5">
          <Radio className="size-3 text-accent" aria-hidden="true" />
          Fila ociosa
        </Badge>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {isLoading || !sources
          ? null
          : sources.map((s) => (
              <div key={s.fonte} className="surface-panel p-5">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="num-tabular text-xs font-semibold text-accent">{s.fonte}</p>
                  </div>
                </div>
                <p className="mt-4 font-display text-2xl font-semibold">
                  {s.ultima_competencia ?? "nunca rodou"}
                </p>
                <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Clock className="size-3" aria-hidden="true" />
                  Última execução: {formatTimestamp(s.ultima_execucao)}
                </p>
              </div>
            ))}
      </div>

      <Collapsible open={open} onOpenChange={setOpen} className="surface-panel overflow-hidden">
        <CollapsibleTrigger className="flex w-full items-center gap-3 p-4 text-left text-sm font-medium transition-colors hover:bg-secondary">
          <ChevronRight
            className={cn("size-4 text-muted-foreground transition-transform", open && "rotate-90")}
            aria-hidden="true"
          />
          🔄 Coletar leads (fonte: OpenCNPJ)
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="space-y-4 border-t border-border p-5">
            <p className="text-sm text-muted-foreground">
              Busca empresas reais via sitemap público (cnpja.com) + API aberta e gratuita do
              OpenCNPJ (sem autenticação, dados oficiais da Receita Federal) — fonte alternativa
              enquanto o conector oficial da Receita Federal (Dados Abertos CNPJ) está desativado.
            </p>

            <div className="flex flex-wrap items-end gap-3">
              <div className="space-y-2">
                <Label htmlFor="ingest-n">Quantidade de CNPJs</Label>
                <Input
                  id="ingest-n"
                  type="number"
                  min={1}
                  max={500}
                  step={10}
                  value={n}
                  onChange={(e) => setN(Number(e.target.value))}
                  className="w-40"
                  disabled={ingest.isPending}
                />
              </div>
              <Button
                className="gap-2"
                disabled={ingest.isPending}
                onClick={() => ingest.mutate({ n })}
              >
                {ingest.isPending ? (
                  <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                ) : (
                  <Play className="size-4" aria-hidden="true" />
                )}
                {ingest.isPending ? "Coletando..." : "Coletar leads"}
              </Button>
            </div>

            {ingest.isPending ? (
              <p className="text-xs text-muted-foreground">
                Pode levar alguns minutos (respeita o rate limit da fonte pública) — não feche
                esta aba.
              </p>
            ) : null}

            {ingest.isError ? (
              <p className="text-sm text-destructive">Falha ao coletar leads: {ingest.error.message}</p>
            ) : null}

            {ingest.isSuccess ? (
              ingest.data.activated ? (
                <p className="text-sm text-success">
                  {ingest.data.n_rows_written} lead(s) novo(s) coletado(s) —{" "}
                  {ingest.data.n_rows_total} no total ativado(s).
                </p>
              ) : (
                <p className="text-sm text-destructive">
                  Validação de qualidade reprovada: {ingest.data.failures.join(", ")}
                </p>
              )
            ) : null}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </section>
  );
}
