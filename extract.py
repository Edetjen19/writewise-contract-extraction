"""LLM extraction: turn the PDF page text into schema-valid rows.

Trustworthiness strategy (see DECISIONS.md):
  1. OpenAI structured outputs (chat.completions.parse) force the model to emit
     JSON matching the Pydantic schema, so the *shape* can't drift.
  2. Extraction is split into focused, single-purpose calls so the model never
     has to juggle the whole document at once and silently drop a section.
  3. Every value carries a verbatim `raw_text`; verify.py then checks each one
     actually appears in the deterministic PDF text. The model is told this, so
     it copies rather than paraphrases.

This module only produces validated Python objects. It does not touch the DB.
"""
from __future__ import annotations

from dataclasses import dataclass

import openai

from models import (
    AdminFeeRow,
    AssumptionRow,
    AssumptionsExtraction,
    DocumentMetadata,
    FeeRow,
    FeeScheduleExtraction,
    IncludedServiceRow,
    IncludedServicesExtraction,
    NetworkPriceRow,
    NetworkPricingExtraction,
    RebateExtraction,
    RebateRow,
)
from pdf_text import Page, page_numbered_text

MAX_TOKENS = 16000

SYSTEM = """You extract pharmacy-benefit pricing data from a vendor proposal into a strict schema.

Hard rules:
- Transcribe; never invent, infer, or "tidy" a value. If something is not in the text, omit it.
- `raw_text` MUST be copied character-for-character from the page text provided, as a
  SINGLE CONTIGUOUS span from one place in the document. Never stitch together fragments
  that are separated in the source by other text. It is automatically verified against the
  source; paraphrased or stitched raw_text will be rejected.
- The document stacks several years inside one visual cell. Explode these into ONE row per year.
- Use the exact page number (from the ===== PAGE n ===== markers) where each value appears.
- Use only the enum values defined by the schema."""


def get_client(api_key: str) -> openai.OpenAI:
    return openai.OpenAI(api_key=api_key)


def _parse(client: openai.OpenAI, model: str, output_format, instructions: str, corpus: str):
    """One focused extraction call using OpenAI structured outputs.

    response_format=<PydanticModel> forces a schema-valid JSON object, which the
    SDK validates and returns as the model instance on message.parsed.
    """
    completion = client.chat.completions.parse(
        model=model,
        max_completion_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{instructions}\n\nDOCUMENT TEXT:\n{corpus}"},
        ],
        response_format=output_format,
    )
    msg = completion.choices[0].message
    if getattr(msg, "refusal", None):
        raise RuntimeError(f"Extraction refused by the model: {msg.refusal}")
    if msg.parsed is None:
        raise RuntimeError(
            f"Extraction returned no parseable output "
            f"(finish_reason={completion.choices[0].finish_reason})."
        )
    return msg.parsed


# --------------------------------------------------------------------------- #
@dataclass
class ExtractionResult:
    metadata: DocumentMetadata
    network_pricing: list[NetworkPriceRow]
    administrative_fees: list[AdminFeeRow]
    rebates: list[RebateRow]
    fees: list[FeeRow]
    included_services: list[IncludedServiceRow]
    assumptions: list[AssumptionRow]

    def counts(self) -> dict[str, int]:
        return {
            "network_pricing": len(self.network_pricing),
            "administrative_fees": len(self.administrative_fees),
            "rebates": len(self.rebates),
            "fees": len(self.fees),
            "included_services": len(self.included_services),
            "assumptions": len(self.assumptions),
        }


def extract_all(client: openai.OpenAI, model: str, pages: list[Page]) -> ExtractionResult:
    corpus = page_numbered_text(pages)

    print("  - metadata ...", flush=True)
    metadata: DocumentMetadata = _parse(
        client, model, DocumentMetadata,
        "Extract the document metadata (vendor, client, title, date as ISO yyyy-mm-dd).",
        corpus,
    )

    print("  - network pricing + admin fees ...", flush=True)
    pricing: NetworkPricingExtraction = _parse(
        client, model, NetworkPricingExtraction,
        (
            "Extract the network discount/dispensing-fee guarantees AND the administrative fee table.\n"
            "There are TWO pricing sections; extract BOTH completely:\n"
            "  - 'Traditional Pricing'                    -> pricing_basis = 'traditional'\n"
            "  - 'Traditional Pricing - Applied Rebates'  -> pricing_basis = 'applied_rebates'\n"
            "They look near-identical but the brand discounts differ; do not merge or dedupe them.\n"
            "Network column mapping under 'Broad National Network': the FIRST value column is "
            "network='retail_30', the SECOND is network='retail_90'. 'Mail Order' -> 'mail'. "
            "'Retail Specialty' -> 'retail_specialty'. 'Exclusive Specialty' (the vendor's "
            "direct/exclusive specialty network) -> 'exclusive_specialty'.\n"
            "Components: Brand Discount->brand_discount, Generic Discount->generic_discount, "
            "Dispensing Fee->dispensing_fee, and for Exclusive Specialty also Brand Effective "
            "Discount->brand_effective_discount, Generic Effective Rate->generic_effective_rate, "
            "LDD->ldd, New to market->new_to_market.\n"
            "For AWP discounts: rate_type='awp_discount_percent', basis='AWP', unit='percent', "
            "value is the magnitude (AWP-21.50% -> 21.50). For dispensing fees: "
            "rate_type='fee_per_claim', basis=null, unit='usd_per_claim', value in dollars.\n"
            "Administrative Fee table runs 2024-2026 (one row per year per pricing_basis)."
        ),
        corpus,
    )

    print("  - rebate guarantees ...", flush=True)
    rebates: RebateExtraction = _parse(
        client, model, RebateExtraction,
        (
            "Extract the rebate guarantee tables. There are TWO tables that are identical except "
            "for payment timing; extract BOTH:\n"
            "  - 'Rebates paid 150 days after the quarter' -> payment_frequency='quarterly', payment_lag_days=150\n"
            "  - 'Rebates paid 60 days after the month'    -> payment_frequency='monthly',   payment_lag_days=60\n"
            "Set payment_timing_text to that timing phrase verbatim. Columns map to network: "
            "Retail 30->retail_30, Retail 90->retail_90, Mail->mail, Specialty->specialty. "
            "One row per (table, network, year). amount is the dollar value; rebate_basis='per_brand_drug'; "
            "unit='usd_per_brand_drug'; raw_text is the verbatim dollar string e.g. '$375.50'."
        ),
        corpus,
    )

    print("  - fee schedule ...", flush=True)
    fees: FeeScheduleExtraction = _parse(
        client, model, FeeScheduleExtraction,
        (
            "Extract EVERY line from the fee schedule pages (Allowances, Additional Administrative "
            "Services, FWA Programs, Other Programs and Services, Additional Claim Fees).\n"
            "Extract EVERY line, including non-priced ones ('Included', 'Quoted upon request').\n"
            "The layout is jumbled: a price can appear in the middle of the wrapped description text, "
            "with the row label sitting BETWEEN a price and its descriptor. Associate each price with "
            "the correct service name. For raw_text, copy ONLY the shortest verbatim string that "
            "contains the amount/rate and sits on a SINGLE line of the text (e.g. '$55,000', "
            "'$0.12 PMPM', '$0.03 per claim — standard USPS delivery;', '$235 per programming hour', "
            "'Included', 'Quoted upon request'). Do NOT append words from a different line into "
            "raw_text; put any remaining descriptor context in `qualifier`.\n"
            "Use value_kind: 'numeric' for dollar/PMPM/etc amounts; 'included' for 'Included'; "
            "'quoted_on_request' for 'Quoted upon request'; 'pass_through' for pure pass-through cost; "
            "'conditional' for tiered/contingent amounts. For non-numeric kinds leave value and unit null.\n"
            "When one service has MULTIPLE priced lines, emit one row per line and set `variant` "
            "(e.g. Appeals -> 'DMR appeal'/'administrative appeal'/'clinical appeal'/'external appeal'; "
            "'Claims portal access' -> one row variant='included allotment' value_kind='included' "
            "qualifier='4 users included', and one row variant='additional user' value=450 "
            "unit='usd_per_additional_user_per_month').\n"
            "Pick the closest category enum. raw_text is the verbatim price/term string."
        ),
        corpus,
    )

    print("  - included services ...", flush=True)
    included: IncludedServicesExtraction = _parse(
        client, model, IncludedServicesExtraction,
        (
            "Extract the 'Included Services' bullet list. category = the section heading the bullet "
            "falls under (e.g. 'Plan Administration', 'Pharmacy Network Management', 'Member Services', "
            "'Account Management and Client Tools'). description = the bullet text. Preserve order."
        ),
        corpus,
    )

    print("  - assumptions ...", flush=True)
    assumptions: AssumptionsExtraction = _parse(
        client, model, AssumptionsExtraction,
        (
            "Extract every assumption/caveat bullet. section is one of: 'general' (General Assumptions), "
            "'traditional_network' (Traditional Network Notes and Assumptions), 'rebate' (Rebate Notes "
            "and Assumptions), 'applied_rebate' (Applied Rebate Offer Assumptions). text = bullet text."
        ),
        corpus,
    )

    return ExtractionResult(
        metadata=metadata,
        network_pricing=pricing.network_pricing,
        administrative_fees=pricing.administrative_fees,
        rebates=rebates.rebates,
        fees=fees.fees,
        included_services=included.services,
        assumptions=assumptions.assumptions,
    )
