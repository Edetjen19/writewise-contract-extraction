"""Typed, read-only database tools for the Q&A agent.

Design choice (justified in DECISIONS.md): several purpose-built tools rather
than one generic SQL tool. Each maps to a real question shape, exposes only safe
typed filters (no arbitrary SQL from the model), and returns rows that always
include `raw_text` + `page` so the agent cites DB facts instead of inventing them.

`document_id` is injected by the dispatcher, never exposed to the model, so the
agent reasons about pricing, not database keys.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import psycopg

# ---- vocabularies surfaced to the model as enums ---------------------------
PRICED_NETWORKS = ["retail_30", "retail_90", "mail", "retail_specialty", "exclusive_specialty"]
COMPONENTS = [
    "brand_discount", "generic_discount", "dispensing_fee", "ldd",
    "new_to_market", "brand_effective_discount", "generic_effective_rate",
]
PRICING_BASES = ["traditional", "applied_rebates"]
REBATE_NETWORKS = ["retail_30", "retail_90", "mail", "specialty"]
PAYMENT_FREQS = ["quarterly", "monthly"]
FEE_CATEGORIES = [
    "implementation_allowance", "pharmacy_management_fund", "eligibility_maintenance",
    "reporting_it_support", "id_cards_member_communication", "fwa_programs",
    "other_programs_services", "additional_claim_fees",
]
ASSUMPTION_SECTIONS = ["general", "traditional_network", "rebate", "applied_rebate"]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _query(conn: psycopg.Connection, sql: str, params: tuple) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_jsonable(dict(r)) for r in cur.fetchall()]


def resolve_document_id(conn: psycopg.Connection, vendor: str | None = None) -> str | None:
    """Pick the active document: by vendor if given, else the most recent."""
    if vendor:
        rows = _query(conn,
            "select id from documents where vendor_name ilike %s order by created_at desc limit 1",
            (f"%{vendor}%",))
    else:
        rows = _query(conn, "select id from documents order by created_at desc limit 1", ())
    return rows[0]["id"] if rows else None


# --------------------------------------------------------------------------- #
# Tool implementations. Each takes (conn, document_id, **filters).
# --------------------------------------------------------------------------- #
def _where(filters: dict[str, Any]) -> tuple[str, list]:
    """Build an AND-ed WHERE fragment from non-null filters."""
    clauses, params = [], []
    for col, val in filters.items():
        if val is not None:
            clauses.append(f"{col} = %s")
            params.append(val)
    frag = (" and " + " and ".join(clauses)) if clauses else ""
    return frag, params


def get_network_pricing(conn, document_id, pricing_basis=None, network=None, component=None, year=None):
    frag, params = _where(
        {"pricing_basis": pricing_basis, "network": network, "component": component, "year": year})
    rows = _query(conn,
        f"""select pricing_basis, network, component, year, rate_type, value, basis, unit, raw_text, page
            from network_pricing where document_id = %s{frag}
            order by pricing_basis, network, component, year""",
        (document_id, *params))
    return {"rows": rows, "count": len(rows)}


def get_administrative_fee(conn, document_id, pricing_basis=None, year=None):
    frag, params = _where({"pricing_basis": pricing_basis, "year": year})
    rows = _query(conn,
        f"""select pricing_basis, year, value, unit, raw_text, page
            from administrative_fees where document_id = %s{frag}
            order by pricing_basis, year""",
        (document_id, *params))
    return {"rows": rows, "count": len(rows)}


def get_rebate_guarantee(conn, document_id, network=None, year=None, payment_frequency=None):
    frag, params = _where(
        {"network": network, "year": year, "payment_frequency": payment_frequency})
    rows = _query(conn,
        f"""select program_name, formulary, rebate_basis, payment_frequency, payment_lag_days,
                   payment_timing_text, network, year, amount, unit, raw_text, page
            from rebate_guarantees where document_id = %s{frag}
            order by payment_frequency, network, year""",
        (document_id, *params))
    return {"rows": rows, "count": len(rows)}


def search_fee_schedule(conn, document_id, query=None, category=None):
    clauses, params = ["document_id = %s"], [document_id]
    if category is not None:
        clauses.append("category = %s")
        params.append(category)
    if query:
        # match each query word independently (AND) against the row's combined text,
        # so phrasing/word-order differences don't cause a false "not found".
        searchable = ("(service_name || ' ' || coalesce(variant,'') || ' ' || "
                      "coalesce(qualifier,'') || ' ' || raw_text)")
        for tok in query.split():
            clauses.append(f"{searchable} ilike %s")
            params.append(f"%{tok}%")
    rows = _query(conn,
        f"""select category, service_name, variant, value_kind, value, unit, qualifier, raw_text, page
            from fee_schedule where {' and '.join(clauses)}
            order by category, service_name, variant nulls first""",
        tuple(params))
    return {"rows": rows, "count": len(rows)}


def list_included_services(conn, document_id, category=None):
    frag, params = _where({"category": category})
    rows = _query(conn,
        f"""select category, description, page from included_services
            where document_id = %s{frag} order by ordinal""",
        (document_id, *params))
    return {"rows": rows, "count": len(rows)}


def search_assumptions(conn, document_id, query=None, section=None):
    clauses, params = ["document_id = %s"], [document_id]
    if section is not None:
        clauses.append("section = %s")
        params.append(section)
    if query:
        for tok in query.split():
            clauses.append("text ilike %s")
            params.append(f"%{tok}%")
    rows = _query(conn,
        f"""select section, text, page from assumptions
            where {' and '.join(clauses)} order by ordinal""",
        tuple(params))
    return {"rows": rows, "count": len(rows)}


def estimate_rebate_total(conn, document_id, network, year, payment_frequency, brand_claim_count):
    """Stretch: compute a total from a retrieved per-brand-drug rebate rate."""
    rows = _query(conn,
        """select amount, unit, raw_text, payment_timing_text, page
           from rebate_guarantees
           where document_id = %s and network = %s and year = %s and payment_frequency = %s""",
        (document_id, network, year, payment_frequency))
    if not rows:
        return {"found": False,
                "message": f"No rebate guarantee for network={network}, year={year}, "
                           f"payment_frequency={payment_frequency}."}
    r = rows[0]
    rate = float(r["amount"])
    total = round(rate * brand_claim_count, 2)
    return {
        "found": True,
        "inputs": {"network": network, "year": year, "payment_frequency": payment_frequency,
                   "brand_claim_count": brand_claim_count},
        "rate_per_brand_drug": rate,
        "rate_raw_text": r["raw_text"],
        "payment_timing_text": r["payment_timing_text"],
        "page": r["page"],
        "estimated_total": total,
        "assumption": "Estimate = per-brand-drug rebate x brand_claim_count, i.e. it assumes one "
                      "rebatable brand drug per brand claim. Actual rebates depend on the specific "
                      "drugs dispensed and the formulary/utilization assumptions in the proposal.",
    }


def get_document_overview(conn, document_id):
    """Vendor/client/date + the filter values available, to orient and disambiguate."""
    doc = _query(conn,
        "select vendor_name, client_name, proposal_title, proposal_date from documents where id = %s",
        (document_id,))
    def distinct(col, table):
        return [r[col] for r in _query(conn,
            f"select distinct {col} from {table} where document_id = %s order by 1", (document_id,))]
    yrs = [r["year"] for r in _query(conn,
        "select distinct year from network_pricing where document_id = %s order by 1", (document_id,))]
    admin_yrs = [r["year"] for r in _query(conn,
        "select distinct year from administrative_fees where document_id = %s order by 1", (document_id,))]
    return {
        "document": doc[0] if doc else None,
        "pricing_bases": distinct("pricing_basis", "network_pricing"),
        "networks": distinct("network", "network_pricing"),
        "components": distinct("component", "network_pricing"),
        "pricing_years": yrs,
        "administrative_fee_years": admin_yrs,
        "rebate_payment_frequencies": distinct("payment_frequency", "rebate_guarantees"),
        "rebate_networks": distinct("network", "rebate_guarantees"),
        "fee_categories": distinct("category", "fee_schedule"),
        "assumption_sections": distinct("section", "assumptions"),
    }


# --------------------------------------------------------------------------- #
# Tool registry: name -> python function
# --------------------------------------------------------------------------- #
TOOL_FUNCS = {
    "get_document_overview": get_document_overview,
    "get_network_pricing": get_network_pricing,
    "get_administrative_fee": get_administrative_fee,
    "get_rebate_guarantee": get_rebate_guarantee,
    "search_fee_schedule": search_fee_schedule,
    "list_included_services": list_included_services,
    "search_assumptions": search_assumptions,
    "estimate_rebate_total": estimate_rebate_total,
}


def _opt(desc: str, enum: list[str] | None = None, typ: str = "string") -> dict:
    schema = {"type": typ, "description": desc}
    if enum is not None:
        schema["enum"] = enum
    return schema


# JSON schemas advertised to the model. Optional filters are simply omitted by the
# model when not needed; additionalProperties:false blocks junk params.
TOOL_SCHEMAS = [
    {
        "name": "get_document_overview",
        "description": "Vendor/client/date plus the available pricing bases, networks, components, "
                       "years, payment frequencies, and categories. Call this first when a question "
                       "is ambiguous, to see what dimensions exist before asking the user to narrow down.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_network_pricing",
        "description": "Network discount and dispensing-fee guarantees. Filters are optional; omit a "
                       "filter to get all matching rows (useful for disambiguation). 'brand_discount' "
                       "differs between pricing bases; 'applied_rebates' has rebates baked into the brand discount.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pricing_basis": _opt("'traditional' or 'applied_rebates'", PRICING_BASES),
                "network": _opt("pharmacy network", PRICED_NETWORKS),
                "component": _opt("which metric", COMPONENTS),
                "year": _opt("plan year, e.g. 2026", typ="integer"),
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_administrative_fee",
        "description": "Per-year administrative fee (per approved paid claim). Note this table covers "
                       "2024-2026, unlike the discount tables (2025-2027).",
        "input_schema": {
            "type": "object",
            "properties": {
                "pricing_basis": _opt("'traditional' or 'applied_rebates'", PRICING_BASES),
                "year": _opt("year, e.g. 2025", typ="integer"),
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_rebate_guarantee",
        "description": "Per-brand-drug rebate guarantees. There are two schedules differing by payment "
                       "timing (quarterly = 150 days after the quarter; monthly = 60 days after the month). "
                       "Omit payment_frequency to return both so you can present/disambiguate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "network": _opt("rebate network", REBATE_NETWORKS),
                "year": _opt("year, e.g. 2027", typ="integer"),
                "payment_frequency": _opt("'quarterly' or 'monthly'", PAYMENT_FREQS),
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_fee_schedule",
        "description": "Search the administrative/clinical/claim fee schedule by keyword and/or category. "
                       "Returns each line with its unit and value_kind (numeric, included, "
                       "quoted_on_request, pass_through, conditional).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": _opt("keyword, e.g. 'prior authorization', 'audit', 'eligibility'"),
                "category": _opt("restrict to a category", FEE_CATEGORIES),
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_included_services",
        "description": "The services bundled at no extra cost ('Included Services'), optionally by section.",
        "input_schema": {
            "type": "object",
            "properties": {"category": _opt("section heading, e.g. 'Member Services'")},
            "additionalProperties": False,
        },
    },
    {
        "name": "search_assumptions",
        "description": "Search the assumptions/caveats that qualify the guarantees (exclusions, "
                       "days-supply rules, minimum lives, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": _opt("keyword, e.g. 'retail 90', 'exclusions', 'specialty'"),
                "section": _opt("restrict to a section", ASSUMPTION_SECTIONS),
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "estimate_rebate_total",
        "description": "Compute an estimated total rebate from a DB-stored per-brand-drug rate: "
                       "rate x brand_claim_count. Returns the rate used, its source, and the assumption. "
                       "Use for 'estimate total rebate dollars' style questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "network": _opt("rebate network", REBATE_NETWORKS),
                "year": _opt("year", typ="integer"),
                "payment_frequency": _opt("'quarterly' or 'monthly'", PAYMENT_FREQS),
                "brand_claim_count": _opt("number of brand claims", typ="integer"),
            },
            "required": ["network", "year", "payment_frequency", "brand_claim_count"],
            "additionalProperties": False,
        },
    },
]

# OpenAI function-tool specs derived from the schemas above (non-strict, so the
# model omits optional filters; additionalProperties:false still blocks junk).
OPENAI_TOOLS = [
    {"type": "function",
     "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOL_SCHEMAS
]


def dispatch(conn: psycopg.Connection, document_id: str, name: str, params: dict) -> Any:
    func = TOOL_FUNCS.get(name)
    if func is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return func(conn, document_id, **params)
    except TypeError as e:
        return {"error": f"bad parameters for {name}: {e}"}
    except Exception as e:  # surface DB errors to the model as a tool error, don't crash
        return {"error": f"{type(e).__name__}: {e}"}
