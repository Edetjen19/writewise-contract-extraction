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
passes three checks: **grounding** (each value traces to verbatim PDF text), **coverage**
(every priced token captured, no silent drops), and **structure** (no duplicate or missing
grid cells). LLM extraction is nondeterministic, so the pipeline **self-heals** by
re-extracting up to 3x before giving up. Override the gate with `--allow-ungrounded`; use
`--no-load` to verify without a database (handy for inspecting `out/extraction.json`).

### Reproducible across vendors

The pipeline takes the PDF as input and is not hardcoded to Northwind's numbers
or name; running it on a second proposal in the same format adds a second
`documents` row. The controlled vocabulary (network/component/category/unit) lives
in `models.py`, so a new variant is a one-file change, not a schema migration.

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
extract.py        Claude structured extraction, section by section
verify.py         grounding (precision) + coverage (recall) checks
load.py           idempotent load into Postgres
pipeline.py       CLI: init-db / run
tools.py          typed, read-only DB tools for the agent
agent.py          the grounded agent loop + system prompt
chat.py           Q&A CLI (REPL or one-shot)
eval/             Q&A pairs + runner (stretch)
assets/           the sample PDF
```
