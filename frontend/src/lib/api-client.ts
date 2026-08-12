/**
 * Fetch wrapper único para os endpoints de `src/api/app.py`. Duas variantes:
 * `apiFetchJson` (a maioria dos endpoints) e `apiFetchBlob` (export/export-one, que
 * devolvem os bytes do arquivo direto no corpo da resposta, ver `_export_response`
 * no lado Python).
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

/** Erro de API tipado -- `status` deixa o chamador tratar casos específicos (ex.:
 * 409 "nenhuma versão ativa", 403 "bloqueado em modo demo") sem parsear string. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail ?? response.statusText;
  } catch {
    return response.statusText;
  }
}

export async function apiFetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    throw new ApiError(await readErrorDetail(response), response.status);
  }
  return (await response.json()) as T;
}

function parseFilename(contentDisposition: string | null): string {
  const match = contentDisposition?.match(/filename="?([^"]+)"?/);
  return match?.[1] ?? "download";
}

export interface BlobResult {
  blob: Blob;
  filename: string;
  nExported: number;
}

export async function apiFetchBlob(path: string, init?: RequestInit): Promise<BlobResult> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    throw new ApiError(await readErrorDetail(response), response.status);
  }
  const blob = await response.blob();
  const filename = parseFilename(response.headers.get("Content-Disposition"));
  const nExported = Number(response.headers.get("X-N-Exported") ?? "0");
  return { blob, filename, nExported };
}

/** Mesmo padrão do `st.download_button` do dashboard Streamlit: cria um link
 * temporário e simula o clique -- funciona de dentro de um iframe (o embed no
 * Streamlit, ver `src/dashboard/react_embed.py`) sem precisar de permissão extra. */
export function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
