# Decisions

## Schema design

**One row per atomic, queryable fact.** The PDF stacks three years in one cell
(`2025: AWP-21.50% / 2026: … / 2027: …`); I explode these to one row per `(…, year)`
so a question about 2026 is a `WHERE year = 2026`, not blob-parsing. The dimensions the
document makes easy to lose are first-class columns:

- **`pricing_basis`** (`traditional` vs `applied_rebates`) keeps the two near-identical
  sections distinct. They differ only in brand discounts (rebates baked in), so each basis
  is stored as a full set, not merged.
- **Rebate payment timing** (`payment_frequency`, `payment_lag_days`, `payment_timing_text`):
  the two rebate tables differ *only* by timing, so it is first-class; merging would erase a
  real offer.
- **Units are never implied** — `network_pricing.rate_type`/`unit` and `fee_schedule.unit`
  carry the mixed bag ($/claim, PMPM, PMPY, $/record, $/audit, $/year, …).
- **Non-numeric values** use `fee_schedule.value_kind`
  (`included`/`quoted_on_request`/`pass_through`/`conditional`) with `value` NULL, so
  "Included" and "$65 each" coexist honestly.
- **One service, many prices** (Appeals; "4 users included / $450 each additional") →
  multiple rows via `variant`.

`year` is a plain `int`, so the admin-fee table (2024-2026) coexists with the discount
tables (2025-2027) with no special case; admin fees are a separate table because they are
program-wide, not network-scoped. Every row stores verbatim `raw_text` + `page`, and
`document_pages` holds full page text, so nothing is unprovenanced. Controlled vocabularies
are **TEXT validated in the app**, not Postgres ENUMs: a new vendor's network name then loads
without a migration; only the invariant axes (`pricing_basis`, `rate_type`,
`payment_frequency`, `value_kind`) carry DB `CHECK`s. Loading is idempotent (unique on file
SHA-256; a re-run replaces that document's child rows in one transaction).

## Extraction and trustworthiness

Provider is **OpenAI** (the assessment key; default `gpt-4.1-mini`, `OPENAI_MODEL`-configurable),
isolated to `extract.py` + `agent.py`. Trust comes from a gate over deterministic pdfplumber
text. Three hard checks abort the load on failure, plus one advisory:

- **Structured outputs** (`chat.completions.parse`) force schema-valid JSON, in focused
  per-section calls so the model never juggles the whole doc and drops a section.
- **Grounding (precision):** every `raw_text` must appear verbatim in the PDF text; the model
  is told this, so it copies rather than paraphrases.
- **Coverage (recall):** every priced token (`$…`, `AWP-…%`) must land in some row, catching
  silent drops.
- **Structure:** `check_structure` flags duplicate keys and grid holes (a
  `(basis, network, component)` missing a year its siblings have). On any failure the pipeline
  **re-extracts up to 3×** (LLM nondeterminism usually clears it) and aborts otherwise; the
  loader dedupes as a backstop. This caught two real `gpt-4.1-mini` slips: a duplicated cell
  and a dropped fee.
- **Page attribution (advisory):** each `raw_text` should also appear on the *page* the row
  cites; a value grounded elsewhere in the corpus but not on its cited page is surfaced for
  review. This is the one check a bare-string grounding pass can't do, and it targets the
  mis-attribution class below (it's clean on the current run).

I extract from text, not page images — the text already contains every value verbatim, which
keeps grounding tight (page-image vision is the fallback for a worse layout).

## Tool design: typed tools, not one SQL tool

I chose several typed, read-only tools (`get_network_pricing`, `get_rebate_guarantee`,
`search_fee_schedule`, `estimate_rebate_total`, …) over a generic query tool because: the
model can't emit arbitrary SQL or hallucinate a column (constrained, auditable); each tool is
a question shape whose typed filters make ambiguity explicit (omit a filter → broaden, don't
guess); and every row returned carries `raw_text` + `page`. Trade-off: less flexible for novel
cross-table queries and more design upfront, mitigated by keyword-search tools and a
`get_document_overview`. A raw-SQL tool would be more flexible but sacrifices exactly the
grounding/safety this is graded on.

## Grounding and ambiguity

Grounding is defense-in-depth: (1) the agent has **no access to the PDF or model memory** for
facts; the only path to a number is a tool, so there is no data path for the model to invent
one (the figure has to come from a row); (2) the system prompt forbids any figure not returned
by a tool, requires unit + context on every answer, and says "not specified" when tools return
nothing; (3) results carry `raw_text` + `page`. For ambiguity ("What's the brand discount?"), tools return all matches when a filter is
omitted, the prompt instructs present-or-clarify, and the available dimensions are injected from
the DB so the agent disambiguates concretely — surfacing both pricing bases and both rebate
timings rather than guessing.

## Security (Supabase)

Backend-only datastore: the pipeline/agent use the direct Postgres connection (the `postgres`
role bypasses RLS); nothing uses the anon/PostgREST path. So `schema.sql` enables RLS with no
policies — public anon/authenticated roles are denied by default (a leaked anon key can't touch
pricing data), our connection is unaffected, and it is a no-op on local Postgres. Extensions are
kept out of `public` (I dropped the optional `pg_trgm` index; ILIKE over tens of rows needs
none), and psycopg disables prepared statements so it works through any Supabase pooler mode.

## What broke / limitations

- The jumbled fee schedule (pages 7-8) puts a row label *between* a price and its descriptor;
  the model sometimes stitched `raw_text` across the gap (caught by grounding) or dropped a line
  (caught by coverage). Worse, where a *price precedes its label* (the "...support — late fee",
  priced `$235 per hour`), it once landed on the row above and the fee was mislabeled `Included`
  on the wrong page, grounded only because "Included" recurs. Fix: `raw_text` = a single-line
  verbatim token, descriptors → `qualifier`, an explicit "a priced service is never `Included`"
  rule that attributes the price to the labeled row, plus the page-attribution advisory. Residual:
  an *included* base allotment that physically precedes its portal-access label can still attach
  to the adjacent service (all values are preserved; only the grouping is imperfect). A nastier
  layout would warrant page-image vision.
- `gpt-4.1-mini` occasionally duplicated a `network_pricing` cell → `check_structure` gates it.
- Agent search was whole-phrase `ILIKE` and once called a present fee "not specified"; now
  per-word AND, and the agent broadens a failed search first.
- `estimate_rebate_total` assumes one rebatable brand drug per claim (surfaced in its output).
- Coverage only catches a dropped row when its value is unique. The page-attribution advisory now
  flags a value cited to the *wrong page*, but a bare "Included" mis-grouped on the *same* page
  still slips by; a golden-file reconciliation would close this.
- Single-document agent (latest, or `--vendor`); no multi-document comparison.

## With more time

Prompt-cache the corpus and parallelize section calls; a `show_source(page)` tool; `strict: true`
tool schemas + per-row confidence; golden-file reconciliation and a larger eval set; SQL views for
common lookups.
