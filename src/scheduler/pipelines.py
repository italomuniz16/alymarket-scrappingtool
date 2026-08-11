"""Wiring real dos pipelines agendáveis (BR_RECEITA, FR_SIRENE): compõe os módulos
já existentes de ingestão/ETL em `IngestionPipeline`s prontos pro scheduler (ver
`pipeline_runner.py`).

Este módulo não tem lógica de agendamento/idempotência própria — isso é
`pipeline_runner.py`. Aqui só existe "como baixar e materializar uma competência
desta fonte", que é a peça injetada em `IngestionPipeline.run`.
"""

from __future__ import annotations

from pathlib import Path

from src.etl.loader import load_staging_directory, open_warehouse
from src.etl.transform import (
    PipelineResult,
    run_transform_pipeline,
    run_transform_pipeline_fr,
    run_transform_pipeline_opencnpj,
)
from src.ingestion.br_opencnpj.client import OpenCnpjClient
from src.ingestion.br_opencnpj.discovery import SitemapCnpjDiscovery
from src.ingestion.br_receita.downloader import ReceitaCNPJDownloader
from src.ingestion.br_receita.extractor import extract_all
from src.ingestion.fr_sirene.parser import detect_entity
from src.ingestion.fr_sirene.stock_download import SireneStockDownloader
from src.scheduler.pipeline_runner import IngestionPipeline, PipelineStepResult

DEFAULT_RAW_DIR = Path("./data/raw")
DEFAULT_STAGING_DIR = Path("./data/staging")
DEFAULT_PARQUET_DIR = Path("./data/staging/_parquet")
DEFAULT_WAREHOUSE_DIR = Path("./data/warehouse")
DEFAULT_WAREHOUSE_DB_PATH = Path("./data/warehouse/alymarket.duckdb")
DEFAULT_RAW_DIR_FR = Path("./data/raw/fr_sirene")

FONTE_BR_RECEITA = "BR_RECEITA"
FONTE_FR_SIRENE = "FR_SIRENE"


def build_br_receita_pipeline(
    *,
    municipio_lookup_csv: Path,
    natureza_juridica_lookup_csv: Path,
    downloader: ReceitaCNPJDownloader | None = None,
    raw_dir: Path = DEFAULT_RAW_DIR,
    staging_dir: Path = DEFAULT_STAGING_DIR,
    parquet_dir: Path = DEFAULT_PARQUET_DIR,
    warehouse_dir: Path = DEFAULT_WAREHOUSE_DIR,
    warehouse_db_path: Path = DEFAULT_WAREHOUSE_DB_PATH,
) -> IngestionPipeline:
    """Pipeline agendável pra Receita Federal (BR): download retomável (só os
    arquivos de stock: Empresas/Estabelecimentos/Simples) -> extração -> carga em
    staging -> join + mapeamento canônico + blue/green (`etl/transform.py`).

    `municipio_lookup_csv`/`natureza_juridica_lookup_csv` são tabelas de
    referência que raramente mudam — não fazem parte do ciclo mensal dos arquivos
    de STOCK que este pipeline atualiza a cada rodada. Baixe-as/prepare-as
    separadamente (`ReceitaCNPJDownloader(...).download(dest, only=["Municipios",
    "Naturezas"])` + `extractor.extract_all`) e aponte pros CSVs já extraídos.

    Args:
        municipio_lookup_csv/natureza_juridica_lookup_csv: ver acima.
        downloader: `ReceitaCNPJDownloader` já configurado; default: um novo,
            padrão de produção (rede real). Injete pra testes.
        raw_dir/staging_dir/parquet_dir: diretórios intermediários de trabalho.
        warehouse_dir/warehouse_db_path: destino final (tabela `leads`) e o banco
            DuckDB persistente usado pra montar/consultar as tabelas de staging.
    """
    downloader = downloader or ReceitaCNPJDownloader()

    def run(competencia: str) -> PipelineStepResult:
        zip_paths = downloader.download(
            raw_dir, competencia=competencia, only=["Empresas", "Estabelecimentos", "Simples"]
        )
        competencia_staging_dir = staging_dir / competencia
        extract_all(zip_paths, competencia_staging_dir)

        con = open_warehouse(warehouse_db_path)
        try:
            load_staging_directory(con, competencia_staging_dir, parquet_dir)
            pipeline_result = run_transform_pipeline(
                con,
                warehouse_dir,
                municipio_lookup_csv=municipio_lookup_csv,
                natureza_juridica_lookup_csv=natureza_juridica_lookup_csv,
            )
        finally:
            con.close()

        return PipelineStepResult(
            n_rows_written=pipeline_result.materialize_result.n_rows_written,
            activated=pipeline_result.activated,
            details={
                "n_rows_skipped": pipeline_result.materialize_result.n_rows_skipped,
                "quality_failures": pipeline_result.quality_report.failures,
                "version_dir": str(pipeline_result.version_dir),
            },
        )

    return IngestionPipeline(fonte=FONTE_BR_RECEITA, check_latest=downloader.check_latest, run=run)


def build_fr_sirene_pipeline(
    *,
    downloader: SireneStockDownloader | None = None,
    raw_dir: Path = DEFAULT_RAW_DIR_FR,
    warehouse_dir: Path = DEFAULT_WAREHOUSE_DIR,
) -> IngestionPipeline:
    """Pipeline agendável pro SIRENE (FR): download retomável (unidade legal +
    estabelecimento) -> join em memória + mapeamento canônico + blue/green
    (`etl/transform.run_transform_pipeline_fr`).

    Diferente do BR, não precisa de lookups externos: os únicos dois arquivos que
    `SireneStockDownloader` baixa (`StockUniteLegale`/`StockEtablissement`) são
    tudo que `run_transform_pipeline_fr` precisa.

    Args:
        downloader: `SireneStockDownloader` já configurado; default: um novo,
            padrão de produção. Injete pra testes.
        raw_dir: diretório de trabalho pros arquivos baixados.
        warehouse_dir: destino final (tabela `leads`).
    """
    downloader = downloader or SireneStockDownloader()

    def run(competencia: str) -> PipelineStepResult:
        downloaded = downloader.download(raw_dir)
        unite_legale_files = [p for p in downloaded if detect_entity(p) == "UNITE_LEGALE"]
        etablissement_files = [p for p in downloaded if detect_entity(p) == "ETABLISSEMENT"]

        pipeline_result = run_transform_pipeline_fr(
            unite_legale_files, etablissement_files, warehouse_dir
        )

        return PipelineStepResult(
            n_rows_written=pipeline_result.materialize_result.n_rows_written,
            activated=pipeline_result.activated,
            details={
                "n_rows_skipped": pipeline_result.materialize_result.n_rows_skipped,
                "quality_failures": pipeline_result.quality_report.failures,
                "version_dir": str(pipeline_result.version_dir),
            },
        )

    return IngestionPipeline(fonte=FONTE_FR_SIRENE, check_latest=downloader.check_latest, run=run)


def collect_opencnpj_leads(
    warehouse_dir: Path = DEFAULT_WAREHOUSE_DIR,
    *,
    n: int,
    discovery: SitemapCnpjDiscovery | None = None,
    client: OpenCnpjClient | None = None,
) -> PipelineResult:
    """Coleta `n` leads reais via OpenCNPJ (descoberta por sitemap público +
    busca na API aberta, ver `src/ingestion/br_opencnpj/`) e ativa uma nova versão
    do warehouse -- fonte alternativa pra `pais=BR` enquanto
    `build_br_receita_pipeline` (Receita Federal oficial) está desativado.

    Sem conceito de "competência" (diferente de `build_br_receita_pipeline`/
    `build_fr_sirene_pipeline`): cada chamada busca `n` CNPJs do sitemap, sem
    idempotência entre rodadas -- não se encaixa no contrato `IngestionPipeline`
    (`check_latest`/`run(competencia)`) do scheduler, então não retorna um
    `IngestionPipeline`; é uma função simples, ponto único de orquestração pra não
    duplicar a lógica entre `cli.py ingest` e o botão "Coletar leads" do dashboard.

    Args:
        warehouse_dir: destino final (tabela `leads`).
        n: quantidade de CNPJs a buscar.
        discovery/client: instâncias já configuradas (injete pra testes); default:
            uma nova de cada, fechada ao final se não injetada.
    """
    owns_discovery = discovery is None
    discovery = discovery or SitemapCnpjDiscovery()
    try:
        cnpjs = discovery.discover(n)
    finally:
        if owns_discovery:
            discovery.close()

    owns_client = client is None
    client = client or OpenCnpjClient()
    try:
        records = list(client.fetch_many(cnpjs))
    finally:
        if owns_client:
            client.close()

    return run_transform_pipeline_opencnpj(records, warehouse_dir)
