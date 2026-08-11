# alymarket

Plataforma de geração de leads B2B a partir de dados PÚBLICOS de empresas (Brasil: base
CNPJ da Receita Federal; França: base SIRENE do INSEE). Prospecção legítima, com
conformidade LGPD/RGPD.

## Objetivo
Ingerir dados públicos oficiais de empresas, segmentar por ICP, enriquecer sob demanda
e exportar listas para outreach B2B, com trilha de conformidade.

## Princípios inegociáveis
- Fonte oficial/API SEMPRE antes de scraping. Scraping só onde permitido, respeitando
  robots.txt, ToS e rate limit, com User-Agent identificado e backoff.
- Dado sintético (Faker) existe SÓ como seed de demonstração: todo registro leva
  is_synthetic=true e é filtrado FORA de qualquer exportação, outreach ou enriquecimento.
- PROIBIDO no projeto: contorno de captcha, emissão de CNPJ, criação de contas em
  sistemas de terceiros, uso de dado sintético como lead real.
- LGPD/RGPD: base legal = legítimo interesse; minimização; opt-out/supressão; registro
  de tratamento; retenção com TTL. França: registros "diffusion partielle" NUNCA entram
  em lista de prospecção (filtro hard).

## Stack
Python 3.12, DuckDB + Parquet (storage analítico), polars/pandas no ETL, httpx+tenacity
(APIs), Streamlit (dashboard MVP), APScheduler (agendamento), Docker, pytest, ruff, mypy.

## Arquitetura
Ingestão (conectores por país implementando um contrato comum) -> ETL (mapeia para schema
canônico) -> Storage (DuckDB/Parquet, tabela `leads` particionada por pais) -> Segmentação
(filtros ICP, scoring, supressão) -> Enriquecimento (sob demanda, com cache) -> Export /
Dashboard. Camada de compliance transversal (audit_log, retention, suppression).

**Nota sobre a fonte BR**: `ingestion/br_receita/downloader.py` (URL oficial original de
Dados Abertos CNPJ da Receita Federal) está com a URL desativada — o portal migrou de
estrutura (SERPRO+) e o conector não foi atualizado ainda. Enquanto isso, `pais=BR` é
populado por `ingestion/br_opencnpj/` (fonte alternativa: descoberta de CNPJs via sitemap
público do cnpja.com + busca via API aberta/sem-autenticação do OpenCNPJ, dados oficiais da
Receita Federal), acionado por `python cli.py ingest --fonte opencnpj --n N`.

## Schema canônico da tabela `leads`
pais, id_legal, id_estab, razao_social, nome_fantasia, cod_atividade, situacao, regiao,
municipio, cep, telefone, email, data_inicio_atividade, porte, capital_social,
natureza_juridica, score_icp, fonte, enriquecido_em, is_synthetic (default false),
flag_difusao_restrita (default false).

## Convenções
Type hints obrigatórios; docstrings; testes com pytest e fixtures pequenas; sem segredos
hardcoded (usar .env / variáveis de ambiente); commits pequenos e descritivos.

## Documento de referência
O PRD detalhado fica em docs/PRD.md. Consulte-o sob demanda para layouts de coluna, regras
de negócio e detalhes de compliance. O CLAUDE.md tem o resumo; o PRD tem o detalhe.
