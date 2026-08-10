# PRD & Arquitetura — Plataforma de Lead Gen B2B a partir de Dados Públicos de CNPJ

**Versão:** 1.0
**Stack alvo:** Python + DuckDB + Streamlit/FastAPI, desenvolvido com Claude Code
**Princípio norteador:** dado público oficial primeiro, scraping só para enriquecimento pontual e educado.

---

## 1. Visão e problema

### 1.1 Problema
Times de vendas B2B no Brasil gastam horas prospectando manualmente empresas por segmento, região e porte. As bases pagas de mercado (Econodata, Speedio, Casa dos Dados etc.) resolvem, mas custam caro e engessam os filtros. A Receita Federal disponibiliza gratuitamente a base completa de CNPJ — o gargalo é **ingestão, normalização, segmentação por ICP e entrega em formato utilizável**.

### 1.2 Solução
Uma plataforma que ingere a base oficial de CNPJ da Receita, permite segmentar por perfil de cliente ideal (ICP), enriquece pontualmente os leads selecionados via APIs públicas, aplica scoring, e exporta listas prontas para CRM/outreach — tudo com trilha de conformidade LGPD.

### 1.3 Não-objetivos (fora de escopo, explicitamente)
- ❌ Emissão de CNPJ / contorno de captcha em site de órgão público.
- ❌ Coleta de dados pessoais sensíveis ou de fontes que exigem burlar autenticação.
- ❌ Scraping de fontes que proíbem via robots.txt/ToS.
- ❌ Uso de dado sintético como se fosse lead real (ver 1.4 — o dado de demo é isolado e nunca vira insumo de cadastro ou exportação real).

### 1.4 Dado sintético (seed de demonstração) — permitido, mas isolado
O sistema pode gerar dados fictícios **exclusivamente** para popular a UI em desenvolvimento, testes e demonstrações (dashboard não pode ficar vazio). Regras rígidas:
- Gerado por **biblioteca de faker** (`Faker` locale `pt_BR`/`fr_FR`), **não** por scraping do 4devs — mesma saída (CPF/CNPJ/SIREN com dígito válido), sem dependência frágil nem risco de ToS.
- Todo registro sintético carrega flag `is_synthetic = true` (`origem = 'demo'`).
- **Fisicamente isolado** dos dados reais e **filtrado *fora*** por padrão em qualquer exportação, lista de outreach ou enriquecimento.
- **Nunca** usado como insumo para cadastro, registro ou criação de conta em qualquer sistema.

---

## 2. Personas e casos de uso

| Persona | Objetivo | Caso de uso principal |
|---|---|---|
| SDR / pré-vendas | Listar empresas de um nicho | "Me dá 500 clínicas odontológicas ativas em SP capital abertas nos últimos 2 anos, com e-mail." |
| Head de Growth | Dimensionar TAM | "Quantas empresas do CNAE X existem por UF e porte?" |
| Operações RevOps | Manter base limpa | Refresh mensal, deduplicação, supressão de opt-outs. |
| DPO / Jurídico | Garantir conformidade | Auditar base legal, ver logs, exportar registro de tratamento. |

---

## 3. Fontes de dados

### 3.1 🇧🇷 Brasil — Dados Abertos CNPJ (Receita Federal)
- **URL:** `https://arquivos.receitafederal.gov.br/dados/cnpj/dados_abertos_cnpj/` (mirror legado: `https://dadosabertos.rfb.gov.br/CNPJ/`)
- **Atualização:** mensal.
- **Formato:** ZIPs contendo CSVs, encoding `ISO-8859-1` (latin-1), separador `;`, sem cabeçalho, aspas duplas.
- **Volume:** ~60M estabelecimentos; ~5–6 GB comprimido, ~30–40 GB descomprimido.
- **Arquivos relevantes:**
  - `EMPRECSV` — razão social, natureza jurídica, porte, capital social.
  - `ESTABELE` — CNPJ completo, nome fantasia, **situação cadastral**, CNAE principal/secundário, endereço, UF, município, CEP, **telefone(s)**, **e-mail**, data de início de atividade.
  - `SOCIOCSV` — quadro societário (dado pessoal — tratar com cautela).
  - `SIMPLES` — opção pelo Simples Nacional / MEI.
  - Tabelas auxiliares: `CNAECSV`, `MUNICCSV`, `NATJUCSV`, `QUALSCSV`, `MOTICSV`, `PAISCSV`.

> **Por que isso substitui scraping:** o campo de e-mail/telefone de contato já é público no dump oficial. Baixar e consultar localmente é mais completo, mais rápido e mais legal do que raspar sites.

### 3.2 🇫🇷 França — Base SIRENE (INSEE / Annuaire des Entreprises)

O site `annuaire-entreprises.data.gouv.fr` é apenas a interface de consulta pública. A fonte real é a base **SIRENE** do INSEE, exposta por **APIs oficiais e gratuitas** da DINUM e por dumps de stock no data.gouv.fr. **Não se raspa a tela do annuaire** — usa-se a API/os arquivos oficiais. (O antigo `sirene.fr` foi desativado em dez/2025 e tudo migrou para o annuaire.)

Identificadores: **SIREN** (9 dígitos, unidade legal) e **SIRET** (14 dígitos, estabelecimento) — os equivalentes ao CNPJ básico e ao CNPJ completo, respectivamente. Código de atividade: **NAF/APE** (equivalente ao CNAE).

**Três formas de puxar dados (por ordem de preferência):**

1. **API Recherche d'Entreprises** — busca e descoberta, aberta e sem autenticação:
   - Endpoint: `https://recherche-entreprises.api.gouv.fr/search`
   - Exemplos:
     - `?q=DINUM` (texto livre)
     - `?q=siren:130025265` (por SIREN)
     - `?q=Boulangerie&code_postal=13001&code_naf=56.10A` (com filtros)
   - Filtros: nome, endereço, código NAF, localização, porte. Atualização diária.
   - **Limite:** serve para *encontrar* empresas; não entrega a base SIRENE completa (sem predecessores/sucessores, sem entidades de difusão restrita).

2. **API Sirene open data (INSEE)** — consulta completa do repositório desde 1973, unidade legal e estabelecimento, incluindo unidades fechadas. Use para dados mais ricos por SIREN/SIRET.

3. **Arquivos de stock em massa** — dataset "Base Sirene des entreprises et de leurs établissements (SIREN, SIRET)" no data.gouv.fr. É o equivalente ao dump da Receita — ideal para carga inicial completa e para o refresh via arquivos de atualização diária.
   - ⚠️ Alguns arquivos migraram (fev/2026) de `files.data.gouv.fr` para `object.files.data.gouv.fr` — confirme a URL atual antes de baixar.

**Limite de taxa:** APIs do bouquet operam na ordem de **~1000 requisições/min por IP** — respeite com rate limit e backoff.

> ⚠️ **Restrição legal crítica para prospecção (RGPD):** registros com status de **"diffusion partielle"** (o antigo "non diffusible N" foi convertido para "P" em 2023) **não podem ter dados pessoais redifundidos integralmente nem ser usados para fins de prospecção.** Além disso, dados de representantes legais de pessoas jurídicas **não são difundidos em open data** (Art. R123-232 do Code de commerce). O sistema **deve filtrar esses registros para fora de qualquer lista de outreach** — isso é obrigatório, não opcional.

### 3.3 Fonte secundária — enriquecimento sob demanda (opcional)
Usada só para os leads já filtrados, com rate limit e cache:
- 🇧🇷 **BrasilAPI** — `https://brasilapi.com.br/api/cnpj/v1/{cnpj}` (gratuita, rate-limited).
- 🇧🇷 **CNPJá / ReceitaWS** — freemium; validar situação cadastral atual, refrescar dados.
- 🇫🇷 **API Sirene / Recherche d'Entreprises** — refrescar situação (ativa/fechada) e dados por SIREN/SIRET.
- **Descoberta de site/contato:** crawler educado, respeitando `robots.txt`, com baixa taxa de requisição e identificação via User-Agent.

**Regras de enriquecimento:** nunca em massa sobre a base inteira; só sobre o subconjunto exportável. Sempre com backoff exponencial, cache local (TTL configurável) e respeito estrito ao ToS de cada fonte.

---

## 4. Arquitetura

### 4.1 Diagrama lógico (camadas)

```
┌─────────────────────────────────────────────────────────────┐
│  DASHBOARD (Streamlit MVP  →  FastAPI + React em produção)   │
│  busca • filtros ICP • preview • export • painel compliance  │
└───────────────┬─────────────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────────────┐
│  API / SERVICE LAYER                                          │
│  segmentação • scoring • dedupe • supressão (opt-out)        │
└───────────────┬─────────────────────────────────────────────┘
                │
┌───────────────▼──────────────┐   ┌──────────────────────────┐
│  STORAGE ANALÍTICO           │   │  ENRICHMENT SERVICE       │
│  DuckDB + Parquet            │◄──┤  BrasilAPI/CNPJá (cache,  │
│  tabela leads denormalizada  │   │  rate limit, backoff)     │
└───────────────▲──────────────┘   └──────────────────────────┘
                │
┌───────────────┴──────────────┐
│  ETL / INGESTION             │
│  downloader RF • unzip •      │
│  parse latin-1 • normalize •  │
│  join • carga em Parquet      │
└───────────────▲──────────────┘
                │
┌───────────────┴──────────────┐
│  Dados Abertos CNPJ (Receita) │
└──────────────────────────────┘
```

### 4.2 Por que DuckDB
Para ~60M linhas em uma máquina local, DuckDB + Parquet é a escolha certa: consultas analíticas SQL em segundos, sem precisar subir um cluster ou pagar banco gerenciado. Postgres entra depois, se você precisar de multiusuário/transacional na camada de aplicação (ex.: gerenciar listas salvas, opt-outs, usuários).

### 4.3 Stack recomendada
| Camada | Tecnologia | Motivo |
|---|---|---|
| ETL | Python + `polars` ou DuckDB `read_csv` | rápido em CSV grande |
| Storage analítico | DuckDB + Parquet | escala local barata |
| Storage app | Postgres (fase 2+) | listas, opt-out, usuários |
| Enriquecimento | `httpx` + `tenacity` (backoff) | resiliência |
| Crawler (opcional) | `playwright` ou `httpx` + `selectolax` | leve, respeita robots |
| Dashboard MVP | Streamlit | velocidade de entrega |
| Dashboard prod | FastAPI + Next.js/React | escala, controle de UX |
| Orquestração | `APScheduler` (MVP) → Prefect (prod) | refresh mensal |
| Empacotamento | Docker | reprodutibilidade |

### 4.4 Modelo de dados (tabela `leads` denormalizada, multi-país)
Para suportar Brasil e França num schema único, use um **modelo canônico** com um campo `pais` e mapeamento dos identificadores locais:

| Campo canônico | 🇧🇷 Brasil | 🇫🇷 França |
|---|---|---|
| `pais` | `BR` | `FR` |
| `id_legal` (empresa) | CNPJ básico | SIREN |
| `id_estab` (estabelecimento) | CNPJ completo | SIRET |
| `razao_social` | razão social | dénomination / raison sociale |
| `nome_fantasia` | nome fantasia | nom commercial |
| `cod_atividade` | CNAE | NAF/APE |
| `situacao` | situação cadastral | état administratif (A/F) |
| `regiao` | UF | région / département |
| `municipio` | município | commune |
| `flag_difusao_restrita` | (n/a) | `diffusion partielle` → excluir de outreach |

Campos comuns: `data_inicio_atividade`, `porte`, `capital_social`, `natureza_juridica`, `cep`, `telefone`, `email`, `score_icp`, `enriquecido_em`, `fonte`, `pais`, `is_synthetic` (default `false`; `true` só para seed de demo — sempre filtrado fora de exportações reais).

Índices/particionamento: por `pais`, `regiao` e `cod_atividade` (as dimensões de filtro mais usadas).

---

## 5. Camada de conformidade (LGPD) — requisito de produto, não opcional

1. **Base legal:** legítimo interesse (LGPD Art. 7º, IX no Brasil; RGPD Art. 6(1)(f) na França) para prospecção B2B. Documentar o teste de proporcionalidade (LIA).
2. **Minimização:** só coletar/armazenar o necessário para prospecção. Dados de sócios/dirigentes (PF) só quando estritamente necessário; por padrão, ocultar CPF (o dump BR já vem mascarado).
3. **Supressão / opt-out:** lista de supressão consultada em toda exportação; qualquer pedido de descadastramento remove o contato de futuras listas.
4. **🇫🇷 Filtro de difusão restrita (obrigatório):** registros SIRENE com status **"diffusion partielle"** devem ser excluídos de qualquer lista de prospecção — a lei francesa proíbe expressamente o uso desses dados para prospecção. Implementar como filtro *hard* no motor de exportação, não como preferência.
5. **Transparência:** todo outreach deve identificar origem dos dados e oferecer opt-out claro.
6. **Registro de tratamento:** log de quem exportou o quê e quando (auditoria) — atende tanto ao "registro de operações" da LGPD quanto ao "registre des traitements" do RGPD.
7. **Retenção:** política de expurgo de dados enriquecidos após TTL definido.

> Isso não é "burocracia" — é o que diferencia um produto vendável de um risco jurídico.

---

## 6. Processos operacionais

### 6.1 Fluxo de ingestão (por fonte, agendado)
Cada conector (BR/FR) implementa o mesmo contrato, então o fluxo é o mesmo:
1. Verificar nova competência/atualização disponível na fonte (mensal na RF; diária/semanal no SIRENE).
2. Baixar arquivos de stock (retomável, com verificação de integridade).
3. Descomprimir/parse → normalização de tipos e strings (latin-1 no BR; UTF-8 no FR).
4. Join das entidades locais (BR: estab+empresa+simples; FR: unité légale + établissement).
5. **Mapear para o schema canônico** (`canonical.py`), incluindo o `flag_difusao_restrita` no FR.
6. Materializar/atualizar a tabela `leads` em Parquet (partição por `pais`).
7. Rodar validações de qualidade (contagens, nulos, situação, flags de difusão).
8. Trocar a versão "ativa" (blue/green) e arquivar a anterior.

### 6.2 Fluxo de geração de lista
1. Usuário define filtros ICP no dashboard (incluindo país).
2. Sistema consulta DuckDB → preview + contagem.
3. Aplica supressão (opt-out), deduplicação, **exclusão de registros sintéticos (`is_synthetic`)** e **filtro *hard* de difusão restrita (FR)**.
4. (Opcional) enriquece o subconjunto via API, com cache.
5. Calcula `score_icp`.
6. Exporta CSV/Excel + registra no log de auditoria.

### 6.3 Fluxo de scoring ICP (exemplo de sinais)
- Situação cadastral = ATIVA (obrigatório).
- Presença de e-mail e/ou telefone (peso alto).
- CNAE dentro do ICP (peso alto).
- Porte / capital social dentro da faixa alvo.
- Recência de abertura (leads recém-abertos costumam converter melhor em certos nichos).
- UF/município dentro da área de atuação.

---

## 7. Milestones

### Fase 0 — Fundação (semana 1)
- Repositório, ambiente, Docker, gestão de segredos.
- Baixar 1 partição de amostra e validar parsing latin-1.
- **Entregável:** ambiente reprodutível + amostra carregada em DuckDB.

### Fase 1 — Ingestão MVP Brasil (semanas 2–3)
- Conector 🇧🇷 completo (downloader retomável) implementando o contrato `base.py`.
- ETL completo com joins → schema canônico → tabela `leads` em Parquet.
- CLI de consulta (`query --pais BR --uf SP --cnae 8630501 --ativa`).
- **Entregável:** consultar a base brasileira inteira via linha de comando.

### Fase 2 — Segmentação + Export (semana 4)
- Motor de filtros ICP, dedupe e supressão.
- Scoring ICP v1.
- Export CSV/Excel.
- **Entregável:** gerar lista segmentada e exportável.

### Fase 3 — Dashboard (semanas 5–6)
- Streamlit: busca, filtros, preview, download.
- Painel de contagem/TAM por UF/CNAE/porte.
- **Entregável:** UI utilizável por não-técnicos.

### Fase 4 — Enriquecimento (semana 7)
- Serviço de enriquecimento com rate limit, backoff e cache.
- Validação de situação cadastral atual.
- **Entregável:** enriquecer subconjunto exportável.

### Fase 5 — Conector França 🇫🇷 (semana 8)
- Conector `fr_sirene` implementando o mesmo contrato `base.py`.
- Ingestão dos arquivos de stock SIRENE + mapeamento para o schema canônico.
- Captura e propagação do `flag_difusao_restrita` até o filtro *hard* de exportação.
- Enriquecimento via API Recherche d'Entreprises / API Sirene.
- **Entregável:** buscar e exportar leads franceses respeitando a restrição de difusão parcial.
- **Por que só agora:** valide toda a pipeline com uma fonte (BR) antes de plugar a segunda. Como o schema canônico e o contrato de conector já existem, adicionar a França vira trabalho de conector, não de re-arquitetura.

### Fase 6 — Conformidade + Produtização (semanas 9–10)
- Lista de supressão, log de auditoria, política de retenção.
- Scheduler de refresh por fonte (mensal BR, diário/semanal FR).
- (Opcional) migração do dashboard para FastAPI + React.
- **Entregável:** sistema multi-país operável em produção com trilha LGPD/RGPD.

---

## 8. Estrutura de repositório (para o Claude Code)

```
leadgen-cnpj/
├── README.md
├── pyproject.toml
├── docker-compose.yml
├── .env.example
├── data/
│   ├── raw/            # ZIPs baixados (gitignored)
│   ├── staging/        # CSVs descomprimidos (gitignored)
│   └── warehouse/      # Parquet + DuckDB (gitignored)
├── src/
│   ├── ingestion/
│   │   ├── base.py            # interface Connector (contrato comum)
│   │   ├── br_receita/        # 🇧🇷 conector Brasil
│   │   │   ├── downloader.py  #   baixa ZIPs da RF, retomável
│   │   │   ├── extractor.py   #   unzip + validação
│   │   │   └── parser.py      #   latin-1, tipos, normalização
│   │   └── fr_sirene/         # 🇫🇷 conector França
│   │       ├── stock_download.py  # arquivos de stock data.gouv.fr
│   │       ├── api_client.py       # recherche-entreprises / API Sirene
│   │       └── parser.py           # SIREN/SIRET, NAF, état
│   ├── seed/
│   │   └── synthetic.py       # Faker pt_BR/fr_FR → dados de demo (is_synthetic=true)
│   ├── etl/
│   │   ├── loader.py          # carga em DuckDB
│   │   ├── canonical.py       # mapeia BR/FR → schema canônico
│   │   └── transform.py       # joins → tabela leads
│   ├── enrichment/
│   │   ├── client.py          # httpx + tenacity + cache
│   │   └── providers.py       # BrasilAPI, CNPJá...
│   ├── segmentation/
│   │   ├── filters.py         # ICP
│   │   ├── scoring.py         # score_icp
│   │   └── suppression.py     # opt-out / dedupe
│   ├── compliance/
│   │   ├── audit_log.py
│   │   └── retention.py
│   ├── export/
│   │   └── exporters.py       # CSV/Excel/CRM
│   └── dashboard/
│       └── app.py             # Streamlit
├── cli.py
├── tests/
└── docs/
    └── lia_legitimo_interesse.md
```

---

## 9. Como conduzir no Claude Code

1. **Comece pela Fase 0/1 e valide o parsing cedo.** O maior risco técnico é o encoding latin-1 e o layout sem cabeçalho — resolva isso com uma amostra antes de escalar.
2. **Peça um módulo por vez.** Ex.: "implemente `downloader.py` com download retomável e verificação de integridade dos ZIPs da RF."
3. **Exija testes.** Peça testes unitários para parser e transform usando fixtures pequenas.
4. **Trave o schema cedo.** Defina a tabela `leads` como contrato entre camadas antes de construir dashboard/export.
5. **Enriquecimento por último.** É a parte com dependência externa e rate limit — não bloqueie o MVP nele.
6. **Compliance embutida, não no fim.** Peça o `audit_log` e a `suppression` já na Fase 2, não como remendo.

---

## 10. Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Encoding/layout do CSV quebra o parse | Validar com amostra na Fase 0; testes com fixtures |
| Volume estoura memória | DuckDB streaming + Parquet particionado |
| Rate limit / ban nas APIs de enriquecimento | Backoff, cache, só sobre subconjunto |
| Exposição LGPD | Base legal documentada, supressão, minimização, auditoria |
| Dados desatualizados | Refresh mensal automatizado + validação de competência |
| Scraping de sites viola ToS | Só fontes que permitem; respeitar robots.txt; baixa taxa |
