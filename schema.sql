-- ============================================================================
-- WriteWise contract-extraction schema
-- ============================================================================
-- Target: PostgreSQL 13+ (Supabase is Postgres 15; this also runs on a local
-- Postgres container unchanged). Idempotent: safe to re-apply.
--
-- Design goals (see DECISIONS.md for the full rationale):
--   1. One row per ATOMIC, queryable fact. The PDF stacks 3 years of values in
--      a single visual cell; we explode those into one row per (…, year) so the
--      agent can answer "the 2026 value" without parsing a blob.
--   2. Preserve meaning that the README calls out as easy to lose:
--        - units / rate types          -> explicit columns, never implied
--        - payment terms               -> rebate_guarantees.payment_* columns
--        - networks                    -> network column on the priced tables
--        - year applicability          -> a plain `year` int column, so a table
--                                         that runs 2024-2026 coexists with one
--                                         that runs 2025-2027 with no special case
--        - non-numeric values          -> fee_schedule.value_kind + value(NULL)
--   3. Grounding / auditability. Every extracted fact carries the verbatim
--      source string (`raw_text`) and `page`, and document_pages holds the full
--      page text. Nothing is stored that can't be traced back to the PDF.
--   4. Reproducibility across vendors. Controlled vocabularies (network,
--      component, category, unit) are TEXT, validated in the Python layer, not
--      frozen as Postgres ENUMs. A second vendor with a new network name loads
--      without a migration. Only the truly invariant axes (pricing_basis,
--      rate_type, payment_frequency, value_kind) carry CHECK constraints.
-- ============================================================================

-- Keyword search (fee_schedule, assumptions) is plain ILIKE: at this scale the
-- tables are tiny, so no index is warranted. For a large multi-document corpus,
-- add a pg_trgm GIN index, installed in the `extensions` schema (not `public`)
-- to satisfy the Supabase linter:
--   create extension if not exists pg_trgm with schema extensions;
--   create index ... using gin (service_name extensions.gin_trgm_ops);

-- ----------------------------------------------------------------------------
-- documents: one row per proposal. Makes the pipeline multi-document and the
-- load idempotent (re-running the same file upserts onto the same row).
-- ----------------------------------------------------------------------------
create table if not exists documents (
    id              uuid primary key default gen_random_uuid(),
    vendor_name     text not null,                 -- "Northwind"
    client_name     text,                          -- "Brightline Health"
    proposal_title  text,                          -- "Pharmacy Benefit Pricing Proposal"
    proposal_date   date,                          -- 2025-03-14
    source_filename text not null,
    source_sha256   text not null unique,          -- identifies the exact file; dedupes re-runs
    created_at      timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- document_pages: deterministic full text per page (from pdfplumber). Backs the
-- "show me the source" tool and is the corpus the grounding check verifies
-- against. Not LLM-derived, so it is itself trustworthy provenance.
-- ----------------------------------------------------------------------------
create table if not exists document_pages (
    document_id uuid not null references documents(id) on delete cascade,
    page        int  not null,
    text        text not null,
    primary key (document_id, page)
);

-- ----------------------------------------------------------------------------
-- network_pricing: the network discount + dispensing fee guarantees.
-- Covers BOTH near-identical sections via pricing_basis:
--     'traditional'      = "Traditional Pricing"
--     'applied_rebates'  = "Traditional Pricing — Applied Rebates"
-- The two differ only in brand discounts (rebates baked in); storing each basis
-- as a complete set preserves that difference instead of silently merging them.
-- One row per (basis, network, component, year).
-- ----------------------------------------------------------------------------
create table if not exists network_pricing (
    id           uuid primary key default gen_random_uuid(),
    document_id  uuid not null references documents(id) on delete cascade,
    pricing_basis text not null,   -- 'traditional' | 'applied_rebates'
    network      text not null,    -- 'retail_30' | 'retail_90' | 'mail' | 'retail_specialty' | 'exclusive_specialty'
    component    text not null,    -- 'brand_discount' | 'generic_discount' | 'dispensing_fee'
                                   -- | 'ldd' | 'new_to_market'
                                   -- | 'brand_effective_discount' | 'generic_effective_rate'
    year         int  not null,
    rate_type    text not null,    -- 'awp_discount_percent' | 'fee_per_claim'
    value        numeric not null, -- 21.50 (percent off AWP) or 0.45 (dollars)
    basis        text,             -- 'AWP' for discounts; NULL for flat fees
    unit         text not null,    -- 'percent' | 'usd_per_claim'
    raw_text     text not null,    -- verbatim source, e.g. 'AWP-21.50%' / '$0.45 per claim'
    page         int  not null,
    constraint network_pricing_basis_chk
        check (pricing_basis in ('traditional', 'applied_rebates')),
    constraint network_pricing_rate_type_chk
        check (rate_type in ('awp_discount_percent', 'fee_per_claim')),
    constraint network_pricing_uq
        unique (document_id, pricing_basis, network, component, year)
);
create index if not exists network_pricing_lookup_idx
    on network_pricing (document_id, component, network, year);

-- ----------------------------------------------------------------------------
-- administrative_fees: the per-year admin fee table. Kept separate from
-- network_pricing because it is program-wide (not network-specific) and runs a
-- DIFFERENT year range (2024-2026) than the discount tables (2025-2027) — the
-- `year` column absorbs that with no special-casing.
-- ----------------------------------------------------------------------------
create table if not exists administrative_fees (
    id            uuid primary key default gen_random_uuid(),
    document_id   uuid not null references documents(id) on delete cascade,
    pricing_basis text not null,   -- 'traditional' | 'applied_rebates'
    year          int  not null,
    value         numeric not null,
    unit          text not null,   -- 'usd_per_paid_claim'
    raw_text      text not null,   -- '$0.00 per approved paid claim'
    page          int  not null,
    constraint administrative_fees_basis_chk
        check (pricing_basis in ('traditional', 'applied_rebates')),
    constraint administrative_fees_uq
        unique (document_id, pricing_basis, year)
);

-- ----------------------------------------------------------------------------
-- rebate_guarantees: the two rebate tables. They are identical in structure and
-- differ ONLY by payment timing, so payment_frequency / payment_lag_days /
-- payment_timing_text are first-class columns — losing them would collapse two
-- genuinely different offers into one. One row per (timing, network, year).
-- ----------------------------------------------------------------------------
create table if not exists rebate_guarantees (
    id                 uuid primary key default gen_random_uuid(),
    document_id        uuid not null references documents(id) on delete cascade,
    program_name       text,            -- 'Northwind Performance'
    formulary          text,            -- 'Performance exclusionary formulary'
    rebate_basis       text not null,   -- 'per_brand_drug'
    payment_frequency  text not null,   -- 'quarterly' | 'monthly'
    payment_lag_days   int,             -- 150 | 60
    payment_timing_text text not null,  -- verbatim 'Rebates paid 150 days after the quarter'
    network            text not null,   -- 'retail_30' | 'retail_90' | 'mail' | 'specialty'
    year               int  not null,
    amount             numeric not null,
    unit               text not null,   -- 'usd_per_brand_drug'
    raw_text           text not null,   -- '$375.50'
    page               int  not null,
    constraint rebate_payment_frequency_chk
        check (payment_frequency in ('quarterly', 'monthly')),
    constraint rebate_guarantees_uq
        unique (document_id, payment_frequency, network, year)
);
create index if not exists rebate_lookup_idx
    on rebate_guarantees (document_id, network, year);

-- ----------------------------------------------------------------------------
-- fee_schedule: the long, mixed-unit fee list (allowances, admin services, FWA,
-- other programs, claim fees). This is where the README's hardest cases live:
--   - mixed units            -> `unit` (usd_per_claim, pmpm, pmpy, usd_per_record,
--                               usd_per_audit, usd_per_year, usd_each, ...)
--   - non-numeric values     -> value_kind in ('included','quoted_on_request',
--                               'pass_through','conditional') with value = NULL
--   - one service, many prices (Appeals; "4 users: Included / $450 each add'l")
--                            -> multiple rows distinguished by `variant`
-- ----------------------------------------------------------------------------
create table if not exists fee_schedule (
    id           uuid primary key default gen_random_uuid(),
    document_id  uuid not null references documents(id) on delete cascade,
    category     text not null,    -- 'implementation_allowance' | 'pharmacy_management_fund'
                                   -- | 'eligibility_maintenance' | 'reporting_it_support'
                                   -- | 'id_cards_member_communication' | 'fwa_programs'
                                   -- | 'other_programs_services' | 'additional_claim_fees'
    service_name text not null,    -- 'Clinical prior authorization — physician review'
    variant      text,             -- NULL, or e.g. 'DMR appeal' / 'additional user'
    value_kind   text not null,    -- 'numeric' | 'included' | 'quoted_on_request'
                                   -- | 'pass_through' | 'conditional'
    value        numeric,          -- NULL unless value_kind = 'numeric'/'conditional'
    unit         text,             -- NULL for non-priced kinds
    qualifier    text,             -- extra conditions kept verbatim-ish (e.g. '4 users included')
    raw_text     text not null,    -- verbatim source cell, e.g. '$65 each' / 'Included'
    page         int  not null,
    constraint fee_value_kind_chk
        check (value_kind in ('numeric', 'included', 'quoted_on_request',
                              'pass_through', 'conditional'))
);
-- Uniqueness includes category so two same-named services in DIFFERENT categories
-- (possible in another vendor's schedule) don't collide and abort the load.
-- coalesce(variant,'') so a NULL-variant row is unique and reloads cleanly. The
-- drop keeps the definition current if an earlier version created a narrower key.
drop index if exists fee_schedule_uq;
create unique index if not exists fee_schedule_uq
    on fee_schedule (document_id, category, service_name, coalesce(variant, ''));
create index if not exists fee_schedule_category_idx
    on fee_schedule (document_id, category);

-- ----------------------------------------------------------------------------
-- included_services: the "Included Services" bullets, grouped by section. Lets
-- the agent answer "what's included vs extra-cost" by joining intent against
-- fee_schedule. ordinal preserves document order.
-- ----------------------------------------------------------------------------
create table if not exists included_services (
    id          uuid primary key default gen_random_uuid(),
    document_id uuid not null references documents(id) on delete cascade,
    category    text not null,     -- 'Plan Administration' | 'Pharmacy Network Management' | ...
    description text not null,
    ordinal     int,
    page        int not null,
    constraint included_services_uq
        unique (document_id, category, description)
);

-- ----------------------------------------------------------------------------
-- assumptions: the assumptions / caveats bullets. These qualify the numeric
-- guarantees (e.g. "Retail-90 applies to 84+ days' supply"), so grounded
-- qualitative answers need them. ordinal preserves document order.
-- ----------------------------------------------------------------------------
create table if not exists assumptions (
    id          uuid primary key default gen_random_uuid(),
    document_id uuid not null references documents(id) on delete cascade,
    section     text not null,     -- 'general' | 'traditional_network' | 'rebate' | 'applied_rebate'
    text        text not null,
    ordinal     int,
    page        int not null,
    constraint assumptions_uq
        unique (document_id, section, text)
);

-- ----------------------------------------------------------------------------
-- Row Level Security. This is a backend-only datastore: the pipeline and agent
-- connect over the DIRECT Postgres connection (the postgres/service role, which
-- bypasses RLS), and nothing uses Supabase's public anon/authenticated
-- (PostgREST) roles. Enabling RLS with no policies denies those public roles by
-- default, so a leaked anon key cannot read or modify pricing data, while our
-- own connection is unaffected. Harmless on a plain local Postgres.
-- ----------------------------------------------------------------------------
alter table documents           enable row level security;
alter table document_pages      enable row level security;
alter table network_pricing     enable row level security;
alter table administrative_fees enable row level security;
alter table rebate_guarantees   enable row level security;
alter table fee_schedule        enable row level security;
alter table included_services   enable row level security;
alter table assumptions         enable row level security;
