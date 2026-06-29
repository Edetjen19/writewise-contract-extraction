# Decisions

A few notes on the choices that mattered and the parts that didn't go cleanly.

## Schema

The whole design follows from one decision: one row per fact, not one row per table cell. The PDF crams three years into a single cell ("2025: AWP-21.50% / 2026: ... / 2027: ..."), so I split that into a row per year. Asking for the 2026 number is then a `where year = 2026` instead of parsing a string.

After that it was about not losing the things that are easy to lose:

- The two pricing sections, "traditional" and "applied rebates", look almost identical, but the brand discounts differ a lot because applied-rebates folds the rebate into the discount. I keep them as two full sets under a `pricing_basis` column rather than merging, which would quietly throw away real pricing.
- The two rebate tables are identical except for when they pay out, so the payment timing (frequency, lag in days, and the literal sentence) is its own columns. Drop it and you've collapsed two different offers into one.
- Units are never implied. Every priced row has an explicit unit, which matters because the fee schedule is all over the place: per claim, PMPM, PMPY, per record, per audit, flat annual.
- Non-numeric "values" like "Included" go in a `value_kind` column with a null amount, so a price and a non-price share a table without me inventing a number.
- A few services have several prices (the four appeal types), so those become multiple rows with a `variant`.

Two smaller calls. `year` is just an integer, which is why the admin-fee table (2024-2026) sits next to the discount tables (2025-2027) with no special-casing. And every row keeps its exact source text and page, with the full page text stored separately, so nothing is untraceable to the PDF.

I went back and forth on Postgres enums for the controlled fields and decided against them. The vocabularies (networks, components, units) are validated in Python, and only the genuinely fixed axes get DB CHECKs. The point is the second-document requirement: a different vendor with a slightly different network name should load without a migration. Reloads are idempotent on the file's hash.

## Extraction, and trusting the output

I use the model to read the messy tables, but I treat its output as a draft that has to earn its way in. Everything is checked against the deterministic pdfplumber text, which I trust because no model touched it. Extraction is split into focused per-section calls with structured outputs, so the JSON shape can't drift and the model isn't juggling the whole document at once (which is how sections get dropped).

Then three checks, any of which aborts the load. Grounding: every value's source string must appear verbatim in the PDF, so the model copies instead of paraphrasing. Coverage: every dollar amount and AWP discount in the source has to land in some row, catching the opposite failure, silent drops. Structure: no duplicate keys and no grid holes (a network/component missing a year its siblings have). On failure it re-extracts up to three times, since a fresh pass usually clears a nondeterministic slip.

There's a fourth, advisory check: whether each value actually shows up on the page it cites. It doesn't block the load, but it's the one thing plain grounding can't catch, and it targets the mis-attribution bug below. I pull from text, not page images, on purpose. The text has every value exactly, which is what keeps grounding strict. Vision would be the fallback for a layout text couldn't handle.

## Tools for the agent

I gave the agent a handful of typed, read-only tools (get_network_pricing, get_rebate_guarantee, search_fee_schedule, estimate_rebate_total, ...) instead of one "run any SQL" tool. The model can't write arbitrary queries or name a column that doesn't exist, every call is auditable, and typed filters make ambiguity explicit: leave a filter off and you get all matches, not a guess. The cost is flexibility, since a generic SQL tool would handle stranger questions, but that's the trade I wanted, because grounding and safety are the point. Keyword-search tools and a get_document_overview cover the long tail.

## Keeping the agent honest, and vague questions

The grounding guarantee is mostly structural: the agent has no access to the PDF and no memory of the contract, so the only way it gets a number is to call a tool and read it off a row. The prompt reinforces it, never state a figure a tool didn't return, always give the unit and context, say "not specified" when nothing comes back. Every row carries source text and page.

For a vague question like "what's the brand discount?", the tools return everything matching when a filter is omitted, and the agent lays out the options or asks rather than picking one. I also feed it the available networks, years, and bases, so it disambiguates with the real options (both pricing bases, both rebate schedules) instead of guessing.

## Security

Backend-only datastore, so I treated it like one. The pipeline and agent use the direct Postgres connection, which bypasses row-level security, and nothing uses Supabase's anon/PostgREST path. So the schema turns RLS on with no policies: the public anon and authenticated roles are denied by default (a leaked anon key can't read or change pricing), my connection is unaffected, and it's a no-op on plain Postgres. I also kept extensions out of the public schema (dropped pg_trgm, since ILIKE over a few dozen rows needs no index), and psycopg runs with prepared statements off so it works through any Supabase pooler mode.

## What broke

The fee schedule on pages 7 and 8 was the real fight. The layout is jumbled enough that a price can sit on a different line from its label. When the price was buried in a row's wrapped text, the model sometimes stitched it into the wrong row (grounding caught it) or dropped a line (coverage caught it). The nastier case was a price sitting above its label, the "support late fee" that's actually $235 an hour. It once attached to the row above and got labeled "Included" on the wrong page, and it passed every check because "Included" genuinely appears elsewhere, so the string was technically grounded. That's the one I'm least happy slipped through. It's why I added the page-attribution check and a flat rule that a priced service is never "Included" and the price belongs to its own labeled row. One rough edge remains: an "included" base allotment that physically precedes its label can still attach to the neighboring service. No values are lost, the grouping is just slightly off, and I'd rather note that than re-roll a nondeterministic extraction and risk re-breaking the late-fee fix.

Smaller things. The model occasionally duplicated a network-pricing cell, which the structure check catches. The agent's keyword search matched the whole phrase and once said a fee wasn't there when it was, so it's per-word now. The rebate estimate assumes one rebatable brand drug per claim, and says so. And coverage only catches a dropped value if that value is unique, so a plain "Included" mis-grouped on the same page would still get past me, which a golden-file check would close.

## What I'd do with more time

Prompt-cache the document so re-runs are cheaper, run the section extractions in parallel, add a show_source tool that returns the page text behind an answer, turn on strict tool schemas with per-row confidence, build the golden-file reconciliation and a bigger eval set, and add a couple of SQL views for common lookups.
