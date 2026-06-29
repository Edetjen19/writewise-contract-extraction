# WriteWise: Contract Extraction & Q&A Agent

Turns a messy PBM pricing proposal (PDF) into structured, queryable data in
Postgres/Supabase, then answers questions about it with an LLM agent that
**only** reads from the database, so it can't invent a number.

LLM provider is **OpenAI** (the assessment supplies a key restricted to
`gpt-4o-mini` / `gpt-4.1-mini` / `gpt-5-mini`); default is `gpt-4.1-mini`, set by
`OPENAI_MODEL`. The provider touches only two files (`extract.py`, `agent.py`).

Two parts:

1. **Extraction pipeline** (`pipeline.py`): `PDF -> OpenAI (structured) -> grounding check -> Postgres`.
   Reproducible and not hardcoded to one vendor; the grounding check is a gate
   that refuses to load anything it can't trace back to verbatim source text.
2. **Q&A agent** (`chat.py`): an OpenAI agent with typed, read-only database tools.
   Every figure it states comes from a tool result, with the unit, context, and source page.

See **DECISIONS.md** for schema rationale, tool design, the grounding approach,
and trade-offs.

---

## Architecture

The extraction pipeline (PDF to database) and the Q&A agent (question to grounded answer).
Nothing loads unless it passes the verification gate, and the agent can only reach the
database through typed, read-only tools.

```mermaid
flowchart TB
  PDF["Pricing proposal PDF"]

  subgraph PIPE["Extraction pipeline (pipeline.py)"]
    direction TB
    TXT["pdf_text.py: pdfplumber reads page text (no AI)"]
    EXT["extract.py: OpenAI gpt-4.1-mini, structured outputs into typed rows"]
    VER{"verify.py gate: grounding + coverage + structure"}
    LOAD["load.py: idempotent load"]
    TXT --> EXT --> VER
    VER -->|"fail: retry up to 3x"| EXT
    VER -->|"pass"| LOAD
  end

  subgraph DB["Supabase (PostgreSQL, RLS on)"]
    direction TB
    TBLS["8 tables: documents, document_pages, network_pricing,<br/>administrative_fees, rebate_guarantees, fee_schedule,<br/>included_services, assumptions"]
  end

  subgraph AGENT["Q&A agent (chat.py + agent.py)"]
    direction TB
    USER["User question"]
    LLM["GroundedAgent: OpenAI gpt-4.1-mini"]
    TOOLS["tools.py: typed, read-only DB tools"]
    ANS["Grounded answer: value + unit + source page"]
    USER --> LLM
    LLM -->|"tool call with filters"| TOOLS
    TOOLS -->|"rows + raw_text + page"| LLM
    LLM --> ANS
  end

  PDF --> TXT
  LOAD --> TBLS
  TOOLS <-->|"safe SQL only"| TBLS
```

### Database schema

Everything links to one `documents` row. The core idea is **one row per atomic fact** (one
pricing basis, network, component, year), and every row keeps the verbatim source text and page.

```mermaid
erDiagram
  documents ||--o{ document_pages : "1 per page"
  documents ||--o{ network_pricing : "discount/fee rows"
  documents ||--o{ administrative_fees : "per-year fee"
  documents ||--o{ rebate_guarantees : "rebate rows"
  documents ||--o{ fee_schedule : "fee lines"
  documents ||--o{ included_services : "bundled services"
  documents ||--o{ assumptions : "caveats"

  documents {
    uuid id PK
    text vendor_name
    text client_name
    date proposal_date
    text source_sha256
  }
  network_pricing {
    text pricing_basis "traditional or applied_rebates"
    text network "retail_30, mail, etc"
    text component "brand_discount, etc"
    int year
    numeric value
    text unit
    text raw_text "verbatim source"
    int page
  }
  rebate_guarantees {
    text payment_frequency "quarterly or monthly"
    int payment_lag_days "150 or 60"
    text network
    int year
    numeric amount
    text raw_text
  }
  fee_schedule {
    text service_name
    text variant "for multi-price rows"
    text value_kind "numeric, included, quoted, pass_through"
    numeric value
    text unit "usd_per_claim, pmpm, etc"
    text raw_text
  }
  administrative_fees {
    text pricing_basis
    int year "2024-2026"
    numeric value
    text raw_text
  }
  document_pages {
    int page
    text text "full page text"
  }
  included_services {
    text category
    text description
  }
  assumptions {
    text section
    text text
  }
```

---

## Prerequisites

- Python 3.11+ (developed on 3.13)
- An OpenAI API key (the assessment provides one; restricted to `gpt-4o-mini` / `gpt-4.1-mini` / `gpt-5-mini`)
- A Postgres database. Supabase free tier is fine; a local Postgres works too.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env       # then edit .env (see below)
```

Fill in `.env`:

```
OPENAI_API_KEY=sk-proj-...
OPENAI_MODEL=gpt-4.1-mini             # or gpt-4o-mini / gpt-5-mini
DATABASE_URL=postgresql://...         # Supabase or local; see options below
```

`.env` is gitignored. The key is read at runtime, never hardcoded or logged.

### Database, option A: Supabase (recommended, this is the grading target)

1. Create a free project at supabase.com.
2. Dashboard -> Project Settings -> Database -> **Connection string -> URI**.
3. Copy it into `DATABASE_URL`, replace `[YOUR-PASSWORD]`, and keep `sslmode=require`.
   If the direct host won't connect on your network, use the **Session pooler**
   string from the same page (it's IPv4-friendly).

The schema is created automatically on first run, so a fresh project works out of the box.

### Database, option B: local Postgres via Docker

```bash
docker run --name writewise-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 -d postgres:16
# DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
```

---

## Run it

```bash
# 1. (optional) create the schema explicitly; `run` also does this idempotently
python pipeline.py init-db

# 2. Extract the PDF and load it into the database
python pipeline.py run assets/Northwind_Pricing_Proposal_SAMPLE.pdf --json out/extraction.json

# 3. Ask questions
python chat.py
#   or one-shot:
python chat.py "What's the generic discount for Retail 90 in 2026?"

# 4. (stretch) run the eval Q&A pairs
python eval/run_eval.py
```

`pipeline run` verifies every extraction and, by default, **refuses to load** unless it
passes three hard checks: **grounding** (each value traces to verbatim PDF text), **coverage**
(every priced token captured, no silent drops), and **structure** (no duplicate or missing
grid cells); it also emits an advisory **page-attribution** signal (a value grounded in the
corpus but not on its cited page). LLM extraction is nondeterministic, so the pipeline
**self-heals** by re-extracting up to 3x before giving up. Override the gate with
`--allow-ungrounded`; use `--no-load` to verify without a database. A sample
`out/extraction.json` (the validated rows + the verification report) is committed so you can
inspect the structured output without running the pipeline; it is regenerated on each run.

### Reproducible across same-format proposals

The pipeline takes the PDF as input and is not hardcoded to Northwind's numbers or
name: values are extracted from the text and the year grid is inferred from the data,
so a second proposal **in the same layout** with a different vendor and different
numbers loads as a second `documents` row. The format itself (the column-to-network
mapping, the section names) is encoded in the extraction prompts plus the controlled
vocabulary in `models.py`, so a genuinely different layout is a prompt + vocabulary
change, not a schema migration.

---

## Example session

```
you> What's the specialty rebate per brand drug in 2027, and when is it paid?
There are two rebate schedules for 2027 specialty (per brand drug):
  - $6,115.00, paid 150 days after the quarter (quarterly schedule)   [page 3]
  - $5,850.00, paid 60 days after the month (monthly schedule)         [page 3]

you> What's the brand discount?
That depends on which network, year, and pricing basis you mean. For example, in 2025:
  Traditional       Retail 30 AWP-21.50%, Retail 90 AWP-24.00%, Mail AWP-26.50% ...
  Applied Rebates   Retail 30 AWP-58.00%, Retail 90 AWP-61.50%, Mail AWP-62.00% ...
Which network / year / basis do you want?
```

---

## Project layout

```
schema.sql        the database schema (graded artifact; heavily commented)
config.py         loads .env
pdf_text.py       deterministic PDF text extraction + normalization (provenance + grounding corpus)
models.py         Pydantic extraction schema + controlled vocabulary
extract.py        OpenAI structured extraction, section by section
verify.py         grounding (precision) + coverage (recall) checks
load.py           idempotent load into Postgres
pipeline.py       CLI: init-db / run
tools.py          typed, read-only DB tools for the agent
agent.py          the grounded agent loop + system prompt
chat.py           Q&A CLI (REPL or one-shot)
eval/             Q&A pairs + runner (stretch)
assets/           the sample PDF
```
