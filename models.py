"""Pydantic models that define the extraction contract.

These are the *strict* shape the model must return (via OpenAI structured outputs,
which force schema-valid JSON). The DB columns are deliberately looser
(plain TEXT) so storage survives a future vendor with a new network name; the
strictness here is to guide and validate THIS run against the known proposal
format. Extend the Literals here if a genuinely new format appears.

Every priced row carries `raw_text` (the verbatim source string) and `page`.
raw_text is what the grounding verifier checks against the PDF text, so the model
is instructed to copy it character-for-character rather than paraphrase.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---- controlled vocabularies (the "same format" axes) ----------------------
PricingBasis = Literal["traditional", "applied_rebates"]
PricedNetwork = Literal[
    "retail_30", "retail_90", "mail", "retail_specialty", "exclusive_specialty"
]
Component = Literal[
    "brand_discount",
    "generic_discount",
    "dispensing_fee",
    "ldd",
    "new_to_market",
    "brand_effective_discount",
    "generic_effective_rate",
]
RateType = Literal["awp_discount_percent", "fee_per_claim"]
RebateNetwork = Literal["retail_30", "retail_90", "mail", "specialty"]
PaymentFrequency = Literal["quarterly", "monthly"]
ValueKind = Literal["numeric", "included", "quoted_on_request", "pass_through", "conditional"]
FeeCategory = Literal[
    "implementation_allowance",
    "pharmacy_management_fund",
    "eligibility_maintenance",
    "reporting_it_support",
    "id_cards_member_communication",
    "fwa_programs",
    "other_programs_services",
    "additional_claim_fees",
]


# ---- document metadata -----------------------------------------------------
class DocumentMetadata(BaseModel):
    vendor_name: str = Field(description="The PBM vendor issuing the proposal (read it from the document).")
    client_name: Optional[str] = Field(default=None, description="The client the proposal is for.")
    proposal_title: Optional[str] = Field(default=None, description="Document title/subtitle.")
    proposal_date: Optional[str] = Field(
        default=None, description="Proposal date as ISO yyyy-mm-dd, or null if absent."
    )


# ---- network pricing + administrative fee ----------------------------------
class NetworkPriceRow(BaseModel):
    pricing_basis: PricingBasis
    network: PricedNetwork
    component: Component
    year: int
    rate_type: RateType
    value: float = Field(description="Magnitude only: 21.50 for AWP-21.50%, 0.45 for $0.45/claim.")
    basis: Optional[str] = Field(default=None, description="'AWP' for discounts, null for flat fees.")
    unit: Literal["percent", "usd_per_claim"]
    raw_text: str = Field(description="Verbatim source string, e.g. 'AWP-21.50%' or '$0.45 per claim'.")
    page: int


class AdminFeeRow(BaseModel):
    pricing_basis: PricingBasis
    year: int
    value: float
    unit: str = Field(description="e.g. 'usd_per_paid_claim'.")
    raw_text: str = Field(description="Verbatim, e.g. '$0.00 per approved paid claim'.")
    page: int


class NetworkPricingExtraction(BaseModel):
    network_pricing: list[NetworkPriceRow]
    administrative_fees: list[AdminFeeRow]


# ---- rebate guarantees -----------------------------------------------------
class RebateRow(BaseModel):
    program_name: Optional[str] = None
    formulary: Optional[str] = None
    rebate_basis: str = Field(description="e.g. 'per_brand_drug'.")
    payment_frequency: PaymentFrequency
    payment_lag_days: Optional[int] = Field(
        default=None, description="Days after the period, e.g. 150 or 60."
    )
    payment_timing_text: str = Field(description="Verbatim, e.g. 'Rebates paid 150 days after the quarter'.")
    network: RebateNetwork
    year: int
    amount: float
    unit: str = Field(default="usd_per_brand_drug")
    raw_text: str = Field(description="Verbatim dollar string, e.g. '$375.50'.")
    page: int


class RebateExtraction(BaseModel):
    rebates: list[RebateRow]


# ---- fee schedule ----------------------------------------------------------
class FeeRow(BaseModel):
    category: FeeCategory
    service_name: str
    variant: Optional[str] = Field(
        default=None,
        description="Set only when one service has multiple priced lines "
        "(e.g. Appeals -> 'DMR appeal'; portal access -> 'additional user').",
    )
    value_kind: ValueKind
    value: Optional[float] = Field(default=None, description="Numeric amount; null for non-numeric kinds.")
    unit: Optional[str] = Field(
        default=None,
        description="e.g. usd_per_claim, pmpm, pmpy, usd_per_member, usd_per_record, "
        "usd_per_audit, usd_per_year, usd_per_hour, usd_per_transaction, usd_each, "
        "usd_per_additional_user_per_month. Null when there is no number.",
    )
    qualifier: Optional[str] = Field(
        default=None, description="Extra conditions, e.g. '4 users included', 'per formulary per year'."
    )
    raw_text: str = Field(description="Verbatim source text for this line, e.g. '$65 each' or 'Included'.")
    page: int


class FeeScheduleExtraction(BaseModel):
    fees: list[FeeRow]


# ---- included services + assumptions ---------------------------------------
class IncludedServiceRow(BaseModel):
    category: str = Field(description="Section heading, e.g. 'Plan Administration'.")
    description: str
    page: int


class IncludedServicesExtraction(BaseModel):
    services: list[IncludedServiceRow]


class AssumptionRow(BaseModel):
    section: str = Field(
        description="One of: general, traditional_network, rebate, applied_rebate."
    )
    text: str
    page: int


class AssumptionsExtraction(BaseModel):
    assumptions: list[AssumptionRow]
