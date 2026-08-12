/**
 * Espelha `src/api/schemas.py` 1:1 — nenhuma regra de filtro/exportação é
 * reimplementada aqui, só o formato de transporte (JSON) entre o frontend e a API.
 */

export type StrOrList = string | string[] | null;

/** Espelha `src/api/schemas.py::ICPCriteriaIn`. `capital_social_min/max` viajam
 * como string (não number) pra não perder precisão decimal em JSON — a API já
 * aceita `Decimal | None` a partir de string. */
export interface ICPCriteria {
  pais: StrOrList;
  cod_atividade: StrOrList;
  regiao: StrOrList;
  porte: StrOrList;
  situacao: StrOrList;
  capital_social_min: string | null;
  capital_social_max: string | null;
  aberta_apos: string | null; // "YYYY-MM-DD"
  com_email: boolean;
  com_telefone: boolean;
}

export const EMPTY_CRITERIA: ICPCriteria = {
  pais: "BR",
  cod_atividade: null,
  regiao: null,
  porte: null,
  situacao: null,
  capital_social_min: null,
  capital_social_max: null,
  aberta_apos: null,
  com_email: false,
  com_telefone: false,
};

/** Uma linha da tabela `leads` (schema canônico -- ver CLAUDE.md). */
export interface Lead {
  pais: string;
  id_legal: string;
  id_estab: string;
  razao_social: string;
  nome_fantasia: string | null;
  cod_atividade: string | null;
  situacao: string | null;
  regiao: string | null;
  municipio: string | null;
  cep: string | null;
  telefone: string | null;
  email: string | null;
  data_inicio_atividade: string | null;
  porte: string | null;
  capital_social: number | null;
  natureza_juridica: string | null;
  score_icp: number | null;
  fonte: string;
  enriquecido_em: string | null;
  is_synthetic: boolean;
  flag_difusao_restrita: boolean;
}

export interface PreviewResponse {
  rows: Lead[];
  tam: number;
  demo: boolean;
}

export interface ChartsResponse {
  regiao: Record<string, number>;
  atividade: Record<string, number>;
}

export interface ComplianceResponse {
  total_bruto: number;
  n_sinteticos: number;
  n_difusao_restrita: number;
  n_duplicados: number;
  n_opt_out: number;
  total_exportavel: number;
}

export interface SchedulerStatusRow {
  fonte: string;
  ultima_competencia: string | null;
  ultima_execucao: string | null;
}

export interface IngestResponse {
  activated: boolean;
  n_rows_written: number;
  n_rows_skipped: number;
  n_rows_total: number;
  failures: string[];
}

export type ExportFormat = "csv" | "xlsx" | "txt";
