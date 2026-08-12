import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Database, Mail, Target, Users } from "lucide-react";
import { useState } from "react";
import alymarketLogo from "@/assets/alymarket-logo.png";
import { CompliancePanel } from "@/components/aly/compliance-panel";
import { DistributionCharts } from "@/components/aly/distribution-charts";
import { FiltersSidebar } from "@/components/aly/filters-sidebar";
import { LeadsTable } from "@/components/aly/leads-table";
import { ModeToggle } from "@/components/aly/mode-toggle";
import { SchedulerPanel } from "@/components/aly/scheduler-panel";
import { StatCard } from "@/components/aly/stat-card";
import { ThemeProvider } from "@/components/theme-provider";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { ApiError } from "@/lib/api-client";
import { EMPTY_CRITERIA } from "@/lib/api-types";
import type { FiltersDraft } from "@/lib/filters";
import { usePreview } from "@/lib/queries";

const queryClient = new QueryClient();

const DEFAULT_DRAFT: FiltersDraft = { criteria: EMPTY_CRITERIA, demo: false, limit: 100 };

function Dashboard() {
  const [draft, setDraft] = useState<FiltersDraft>(DEFAULT_DRAFT);
  const [applied, setApplied] = useState<FiltersDraft>(DEFAULT_DRAFT);

  const preview = usePreview(applied.criteria, applied.demo, applied.limit);
  const noActiveVersion = preview.error instanceof ApiError && preview.error.status === 409;

  const comEmail = preview.data?.rows.filter((r) => r.email).length ?? 0;
  const ativas = preview.data?.rows.filter((r) => r.situacao === "ATIVA").length ?? 0;

  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-30 border-b border-border bg-background/85 backdrop-blur">
        <div className="flex h-16 items-center justify-between gap-4 px-5">
          <div className="flex items-center gap-3">
            <img src={alymarketLogo} alt="alymarket" className="size-9" />
            <p className="font-display text-sm font-semibold leading-tight">alymarket</p>
          </div>
          <div className="flex items-center gap-3">
            <Badge variant="secondary" className="hidden gap-1.5 sm:inline-flex">
              <Database className="size-3" aria-hidden="true" />
              Fonte: OpenCNPJ
            </Badge>
            <ModeToggle />
          </div>
        </div>
      </header>

      <div className="flex flex-col lg:flex-row">
        <FiltersSidebar
          draft={draft}
          onDraftChange={setDraft}
          onApply={() => setApplied(draft)}
        />

        <main className="min-w-0 flex-1 space-y-10 p-5 lg:p-8">
          <div className="space-y-2">
            <h1 className="font-display text-3xl font-semibold tracking-tight sm:text-4xl">
              Geração de leads B2B
            </h1>
            <p className="max-w-2xl text-sm text-muted-foreground">
              Defina o perfil de cliente ideal, revise o recorte de empresas e exporte a lista
              pronta para prospecção.
            </p>
          </div>

          {applied.demo ? (
            <div className="surface-panel border-warning/40 bg-warning/10 p-4 text-sm">
              ⚠️ DADOS FICTÍCIOS — DEMONSTRAÇÃO — os resultados abaixo podem incluir dados
              fictícios.
            </div>
          ) : null}

          {noActiveVersion ? (
            <div className="surface-panel border-accent/40 bg-accent/10 p-4 text-sm">
              Nenhuma versão de leads está ativa ainda. Use o painel "🔄 Coletar leads" abaixo
              pra popular o warehouse.
            </div>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard
              label="TAM endereçável"
              value={preview.data ? String(preview.data.tam) : "—"}
              hint="Empresas no recorte atual"
              icon={<Target className="size-4" aria-hidden="true" />}
            />
            <StatCard
              label="Linhas no preview"
              value={preview.data ? String(preview.data.rows.length) : "—"}
              hint={`Limite configurado: ${applied.limit}`}
              icon={<Users className="size-4" aria-hidden="true" />}
            />
            <StatCard
              label="Com e-mail"
              value={preview.data ? String(comEmail) : "—"}
              tone="accent"
              icon={<Mail className="size-4" aria-hidden="true" />}
            />
            <StatCard
              label="Situação ativa"
              value={preview.data ? `${ativas} / ${preview.data.rows.length}` : "—"}
              tone="muted"
              icon={<Database className="size-4" aria-hidden="true" />}
            />
          </div>

          <Separator />
          <SchedulerPanel noActiveVersion={noActiveVersion} />
          <Separator />

          <LeadsTable
            rows={preview.data?.rows ?? []}
            criteria={applied.criteria}
            demoMode={applied.demo}
          />

          <Separator />
          <DistributionCharts criteria={applied.criteria} demo={applied.demo} />
          <Separator />
          <CompliancePanel criteria={applied.criteria} demo={applied.demo} />

          <footer className="pb-4 text-xs text-muted-foreground">
            alymarket — geração de leads B2B a partir de dados públicos.
          </footer>
        </main>
      </div>
    </div>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <Dashboard />
      </ThemeProvider>
    </QueryClientProvider>
  );
}

export default App;
