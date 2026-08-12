"""API HTTP (FastAPI) sobre `dashboard/data.py` -- ponte pro frontend React
(`frontend/`), que oferece uma interface mais rica que o dashboard Streamlit sobre
os MESMOS dados (mesmo `data/warehouse`, mesmo `.env`). Toda a lógica de negócio
continua em `dashboard/data.py` (idêntica à usada pelo Streamlit) -- este módulo só
adapta pra HTTP: parse de request, chamada da função já testada, serialização da
resposta. Nenhuma regra de compliance (portão de supressão, bloqueio de exportação
em modo demo, exclusões hard) é reimplementada nem contornada aqui.

Rode com: `uvicorn src.api.app:app --reload --port 8000` (ver `frontend/README.md`
ou `Makefile` para rodar API + frontend juntos).
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from src.api.schemas import (
    ChartsRequest,
    ChartsResponse,
    ComplianceRequest,
    ComplianceResponse,
    ExportOneRequest,
    ExportRequest,
    IngestRequest,
    IngestResponse,
    PreviewRequest,
    PreviewResponse,
    SchedulerStatusRow,
)
from src.dashboard.data import (
    DemoExportBlockedError,
    ExportResult,
    chart_counts_by_atividade,
    chart_counts_by_regiao,
    compute_compliance_panel,
    run_export,
    run_export_one,
    run_ingest_opencnpj,
    run_preview,
    scheduler_status_rows,
    sync_leads_view,
)
from src.scheduler.state import DEFAULT_STATE_PATH
from src.segmentation.suppression import load_suppression_list

# Mesmos defaults de `dashboard/app.py` (mesmas variáveis de ambiente, mesmo
# warehouse) -- API e Streamlit são duas interfaces sobre o mesmo dado, não duas
# fontes de verdade separadas.
WAREHOUSE_DIR = Path(os.environ.get("DATA_WAREHOUSE_DIR", "./data/warehouse"))
SUPPRESSION_LIST_PATH = Path(
    os.environ.get("SUPPRESSION_LIST_PATH", "./data/warehouse/suppression_list.csv")
)
EXPORT_DIR = Path(os.environ.get("EXPORTS_DIR", "./data/exports"))
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "./data/warehouse/audit_log.parquet"))
SCHEDULER_STATE_PATH = Path(os.environ.get("SCHEDULER_STATE_PATH", str(DEFAULT_STATE_PATH)))

_EXPORT_MEDIA_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
}

app = FastAPI(title="alymarket API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    # Dev do frontend (Vite) e, opcionalmente, uma origem de produção via env var.
    allow_origins=os.environ.get("API_CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-N-Exported"],
)

_connection: duckdb.DuckDBPyConnection | None = None


def _get_connection() -> duckdb.DuckDBPyConnection:
    """Conexão DuckDB única do processo -- mesma ideia de `dashboard.app.
    get_connection` (`@st.cache_resource`), mas sem o cache do Streamlit: um
    singleton de módulo simples basta, já que o processo uvicorn é de vida única.
    """
    global _connection
    if _connection is None:
        _connection = duckdb.connect(":memory:")
    return _connection


def _synced_connection() -> duckdb.DuckDBPyConnection:
    """Conexão com a view `leads` sincronizada com a versão ativa do warehouse.

    Raises:
        HTTPException: 409, se não houver versão ativa (ninguém rodou o
            `POST /api/ingest` ainda).
    """
    con = _get_connection()
    if not sync_leads_view(con, WAREHOUSE_DIR):
        raise HTTPException(
            status_code=409,
            detail="Nenhuma versão de leads está ativa. Rode POST /api/ingest primeiro.",
        )
    return con


def _export_response(result: ExportResult, formato: str) -> Response:
    """Devolve os bytes do arquivo exportado direto no corpo da resposta (o
    frontend baixa via `Content-Disposition: attachment`, sem round-trip extra) --
    `X-N-Exported` carrega a contagem pós-portão-de-supressão (pode ser `0` sem ser
    erro: lead suprimido por opt-out/duplicata, ver `dashboard.data.run_export`)."""
    headers = {
        "Content-Disposition": f'attachment; filename="{result.path.name}"',
        "X-N-Exported": str(result.n_exported),
    }
    media_type = _EXPORT_MEDIA_TYPES.get(formato, "application/octet-stream")
    return Response(content=result.path.read_bytes(), media_type=media_type, headers=headers)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/scheduler-status", response_model=list[SchedulerStatusRow])
def scheduler_status() -> list[dict[str, object]]:
    return scheduler_status_rows(SCHEDULER_STATE_PATH)


@app.post("/api/preview", response_model=PreviewResponse)
def preview(body: PreviewRequest) -> PreviewResponse:
    con = _synced_connection()
    result = run_preview(con, body.criteria.to_criteria(), demo=body.demo, limit=body.limit)
    return PreviewResponse(rows=result.rows, tam=result.tam, demo=result.demo)


@app.post("/api/charts", response_model=ChartsResponse)
def charts(body: ChartsRequest) -> ChartsResponse:
    con = _synced_connection()
    criteria = body.criteria.to_criteria()
    return ChartsResponse(
        regiao=chart_counts_by_regiao(con, criteria, demo=body.demo),
        atividade=chart_counts_by_atividade(con, criteria, demo=body.demo),
    )


@app.post("/api/compliance", response_model=ComplianceResponse)
def compliance(body: ComplianceRequest) -> ComplianceResponse:
    con = _synced_connection()
    suppression = load_suppression_list(SUPPRESSION_LIST_PATH)
    panel = compute_compliance_panel(con, body.criteria.to_criteria(), suppression=suppression)
    return ComplianceResponse(**dataclasses.asdict(panel))


@app.post("/api/export")
def export(body: ExportRequest) -> Response:
    con = _synced_connection()
    suppression = load_suppression_list(SUPPRESSION_LIST_PATH)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = EXPORT_DIR / f"leads_{timestamp}.{body.formato}"
    try:
        result = run_export(
            con,
            body.criteria.to_criteria(),
            suppression=suppression,
            dest=dest,
            formato=body.formato,
            demo=body.demo,
            usuario=body.usuario,
            audit_log_path=AUDIT_LOG_PATH,
        )
    except DemoExportBlockedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _export_response(result, body.formato)


@app.post("/api/export-one")
def export_one(body: ExportOneRequest) -> Response:
    con = _synced_connection()
    suppression = load_suppression_list(SUPPRESSION_LIST_PATH)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = EXPORT_DIR / f"lead_{body.id_estab}_{timestamp}.{body.formato}"
    try:
        result = run_export_one(
            con,
            body.criteria.to_criteria(),
            id_estab=body.id_estab,
            suppression=suppression,
            dest=dest,
            formato=body.formato,
            demo=body.demo,
            audit_log_path=AUDIT_LOG_PATH,
        )
    except DemoExportBlockedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _export_response(result, body.formato)


@app.post("/api/ingest", response_model=IngestResponse)
def ingest(body: IngestRequest) -> IngestResponse:
    """Coleta `body.n` leads reais (OpenCNPJ) e ativa uma nova versão do warehouse
    -- pode levar dezenas de segundos (rate limit da fonte pública, ver
    `ingestion/br_opencnpj/client.py`); o frontend deve mostrar um estado de
    carregamento, não é instantâneo."""
    result = run_ingest_opencnpj(WAREHOUSE_DIR, n=body.n)
    return IngestResponse(
        activated=result.activated,
        n_rows_written=result.materialize_result.n_rows_written,
        n_rows_skipped=result.materialize_result.n_rows_skipped,
        n_rows_total=result.quality_report.n_rows,
        failures=result.quality_report.failures,
    )
