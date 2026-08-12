import type { ICPCriteria } from "@/lib/api-types";

/** Mesma regra de `dashboard/app.py::_split_csv_field`: separa por vírgula, tira
 * espaço, descarta vazio; string vazia/só vírgulas vira `null` (sem filtro). */
export function splitCsv(raw: string): string[] | null {
  const values = raw
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
  return values.length ? values : null;
}

export interface FiltersDraft {
  criteria: ICPCriteria;
  demo: boolean;
  limit: number;
}
