import { Bar, BarChart, CartesianGrid, Cell, XAxis, YAxis } from "recharts";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import type { ICPCriteria } from "@/lib/api-types";
import { useCharts } from "@/lib/queries";

const config = {
  total: { label: "Empresas", color: "var(--color-chart-1)" },
} as const;

function toChartData(counts: Record<string, number> | undefined): { key: string; total: number }[] {
  if (!counts) return [];
  return Object.entries(counts).map(([key, total]) => ({ key, total }));
}

function DistributionCard({
  title,
  description,
  data,
  accent,
}: {
  title: string;
  description: string;
  data: { key: string; total: number }[];
  accent: string;
}) {
  return (
    <div className="surface-panel p-5">
      <h3 className="text-sm font-semibold">{title}</h3>
      <p className="mt-1 text-xs text-muted-foreground">{description}</p>
      {data.length === 0 ? (
        <p className="mt-4 text-sm text-muted-foreground">Sem dados pro recorte atual.</p>
      ) : (
        <ChartContainer config={config} className="mt-4 h-56 w-full">
          <BarChart data={data} margin={{ top: 8, right: 4, bottom: 0, left: -20 }}>
            <CartesianGrid vertical={false} stroke="var(--color-border)" />
            <XAxis
              dataKey="key"
              tickLine={false}
              axisLine={false}
              tickMargin={8}
              fontSize={11}
              stroke="var(--color-muted-foreground)"
            />
            <YAxis
              allowDecimals={false}
              tickLine={false}
              axisLine={false}
              fontSize={11}
              stroke="var(--color-muted-foreground)"
            />
            <ChartTooltip content={<ChartTooltipContent />} />
            <Bar dataKey="total" radius={[6, 6, 0, 0]}>
              {data.map((entry) => (
                <Cell key={entry.key} fill={accent} />
              ))}
            </Bar>
          </BarChart>
        </ChartContainer>
      )}
    </div>
  );
}

export function DistributionCharts({ criteria, demo }: { criteria: ICPCriteria; demo: boolean }) {
  const { data } = useCharts(criteria, demo);

  return (
    <section aria-labelledby="dist-heading" className="space-y-4">
      <h2 id="dist-heading" className="text-xl font-semibold">
        Distribuição
      </h2>
      <div className="grid gap-4 lg:grid-cols-2">
        <DistributionCard
          title="Por região"
          description="Concentração de leads por UF."
          data={toChartData(data?.regiao)}
          accent="var(--color-chart-1)"
        />
        <DistributionCard
          title="Por atividade"
          description="Códigos CNAE mais recorrentes."
          data={toChartData(data?.atividade)}
          accent="var(--color-chart-2)"
        />
      </div>
    </section>
  );
}
