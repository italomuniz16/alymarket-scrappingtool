import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function StatCard({
  label,
  value,
  hint,
  icon,
  tone = "default",
}: {
  label: string;
  value: string;
  hint?: string;
  icon?: ReactNode;
  tone?: "default" | "accent" | "muted";
}) {
  return (
    <div className="surface-panel flex flex-col gap-3 p-5 transition-shadow hover:shadow-float">
      <div className="flex items-center justify-between gap-3">
        <span className="label-eyebrow">{label}</span>
        {icon ? <span className="text-accent">{icon}</span> : null}
      </div>
      <p
        className={cn(
          "num-tabular text-3xl font-semibold leading-none",
          tone === "accent" && "text-accent",
          tone === "muted" && "text-muted-foreground text-2xl",
        )}
      >
        {value}
      </p>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}
