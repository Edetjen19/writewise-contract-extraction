"""Load a validated ExtractionResult into Postgres.

Idempotent by design: the document is upserted on its file hash, and all of that
document's child rows are deleted and re-inserted inside one transaction. Re-running
the pipeline on the same PDF leaves the DB in the same state; running it on a new
PDF adds a second document without touching the first.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import psycopg

from extract import ExtractionResult
from pdf_text import Page, clean_bullets

_CHILD_TABLES = [
    "document_pages",
    "network_pricing",
    "administrative_fees",
    "rebate_guarantees",
    "fee_schedule",
    "included_services",
    "assumptions",
]


def _parse_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    for parse in (dt.date.fromisoformat, lambda v: dt.datetime.strptime(v, "%B %d, %Y").date()):
        try:
            return parse(value)
        except (ValueError, TypeError):
            continue
    return None


def _dedupe(items, keyfn):
    """Keep the first row per unique key. A defensive backstop: check_structure
    already gates duplicates, but this keeps a forced (--allow-ungrounded) load
    from violating a unique constraint."""
    seen, out = set(), []
    for it in items:
        k = keyfn(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def load(
    conn: psycopg.Connection,
    result: ExtractionResult,
    *,
    source_filename: str,
    source_sha256: str,
    pages: list[Page],
) -> str:
    md = result.metadata
    np_rows = _dedupe(result.network_pricing, lambda r: (r.pricing_basis, r.network, r.component, r.year))
    af_rows = _dedupe(result.administrative_fees, lambda r: (r.pricing_basis, r.year))
    rb_rows = _dedupe(result.rebates, lambda r: (r.payment_frequency, r.network, r.year))
    fee_rows = _dedupe(result.fees, lambda r: (r.service_name, r.variant))
    inc_rows = _dedupe(result.included_services, lambda r: (r.category, r.description))
    asm_rows = _dedupe(result.assumptions, lambda r: (r.section, r.text))
    with conn.cursor() as cur:
        # upsert the document, get its id
        cur.execute(
            """
            insert into documents (vendor_name, client_name, proposal_title, proposal_date,
                                   source_filename, source_sha256)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (source_sha256) do update
                set vendor_name    = excluded.vendor_name,
                    client_name    = excluded.client_name,
                    proposal_title = excluded.proposal_title,
                    proposal_date  = excluded.proposal_date,
                    source_filename = excluded.source_filename
            returning id
            """,
            (md.vendor_name, md.client_name, md.proposal_title, _parse_date(md.proposal_date),
             source_filename, source_sha256),
        )
        doc_id = cur.fetchone()[0]

        # replace child rows for a clean, idempotent reload
        for table in _CHILD_TABLES:
            cur.execute(f"delete from {table} where document_id = %s", (doc_id,))

        cur.executemany(
            "insert into document_pages (document_id, page, text) values (%s, %s, %s)",
            [(doc_id, p.page, clean_bullets(p.text)) for p in pages],
        )

        cur.executemany(
            """insert into network_pricing
               (document_id, pricing_basis, network, component, year, rate_type, value, basis, unit, raw_text, page)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(doc_id, r.pricing_basis, r.network, r.component, r.year, r.rate_type,
              r.value, r.basis, r.unit, r.raw_text, r.page) for r in np_rows],
        )

        cur.executemany(
            """insert into administrative_fees
               (document_id, pricing_basis, year, value, unit, raw_text, page)
               values (%s,%s,%s,%s,%s,%s,%s)""",
            [(doc_id, r.pricing_basis, r.year, r.value, r.unit, r.raw_text, r.page)
             for r in af_rows],
        )

        cur.executemany(
            """insert into rebate_guarantees
               (document_id, program_name, formulary, rebate_basis, payment_frequency, payment_lag_days,
                payment_timing_text, network, year, amount, unit, raw_text, page)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(doc_id, r.program_name, r.formulary, r.rebate_basis, r.payment_frequency, r.payment_lag_days,
              r.payment_timing_text, r.network, r.year, r.amount, r.unit, r.raw_text, r.page)
             for r in rb_rows],
        )

        cur.executemany(
            """insert into fee_schedule
               (document_id, category, service_name, variant, value_kind, value, unit, qualifier, raw_text, page)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(doc_id, r.category, r.service_name, r.variant, r.value_kind, r.value, r.unit,
              r.qualifier, r.raw_text, r.page) for r in fee_rows],
        )

        cur.executemany(
            """insert into included_services (document_id, category, description, ordinal, page)
               values (%s,%s,%s,%s,%s)""",
            [(doc_id, r.category, r.description, i, r.page)
             for i, r in enumerate(inc_rows)],
        )

        cur.executemany(
            """insert into assumptions (document_id, section, text, ordinal, page)
               values (%s,%s,%s,%s,%s)""",
            [(doc_id, r.section, r.text, i, r.page)
             for i, r in enumerate(asm_rows)],
        )

    conn.commit()
    return str(doc_id)
