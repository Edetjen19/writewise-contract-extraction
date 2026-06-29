"""Grounding verification: the gate that keeps LLM output trustworthy.

Three checks run against the deterministic pdfplumber text. Precision and recall
are hard gates: by default the pipeline self-heals (re-extracts) and then refuses
to load if either fails (override with --allow-ungrounded). The page check is
advisory.

  PRECISION (no hallucination): every extracted row's verbatim `raw_text`
    (or description/text for the prose tables) must appear in the source. A row
    whose source string isn't found is flagged ungrounded -> load aborts.

  RECALL (no silent drops): every priced token in the source ($amounts and
    AWP-discounts) must appear in some extracted row. An uncaptured priced token
    means a value was dropped -> load aborts. (A second vendor may legitimately
    keep a price only in prose; --allow-ungrounded is the escape hatch.)

  PAGE ATTRIBUTION (advisory): each row's `raw_text` should also appear on the
    PAGE the row cites. A row grounded somewhere in the corpus but NOT on its
    cited page is surfaced (a likely mis-attribution). Advisory only, because a
    short token like "Included" can legitimately recur across pages, so this is a
    review signal, not a gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from extract import ExtractionResult
from pdf_text import Page, clean_bullets, normalize

# priced tokens that must end up captured somewhere
_PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
_AWP_RE = re.compile(r"AWP-\d+(?:\.\d+)?%", re.IGNORECASE)


@dataclass
class Ungrounded:
    table: str
    identifier: str
    snippet: str


@dataclass
class GroundingReport:
    checked: int = 0
    grounded: int = 0
    ungrounded: list[Ungrounded] = field(default_factory=list)
    source_tokens: int = 0
    captured_tokens: int = 0
    uncaptured_tokens: list[str] = field(default_factory=list)
    page_mismatches: list[Ungrounded] = field(default_factory=list)

    @property
    def all_grounded(self) -> bool:
        return not self.ungrounded

    def summary(self) -> str:
        lines = [
            f"Grounding (precision): {self.grounded}/{self.checked} rows trace to verbatim source text.",
            f"Coverage   (recall):   {self.captured_tokens}/{self.source_tokens} priced source tokens captured.",
        ]
        if self.ungrounded:
            lines.append(f"  UNGROUNDED ({len(self.ungrounded)}):")
            for u in self.ungrounded[:25]:
                lines.append(f"    [{u.table}] {u.identifier}: {u.snippet!r}")
        if self.uncaptured_tokens:
            lines.append(f"  UNCAPTURED priced tokens ({len(self.uncaptured_tokens)}): "
                         + ", ".join(self.uncaptured_tokens[:25]))
        if self.page_mismatches:
            lines.append(f"  PAGE-ATTRIBUTION advisories ({len(self.page_mismatches)}; "
                         f"grounded in corpus but not on the cited page):")
            for u in self.page_mismatches[:25]:
                lines.append(f"    [{u.table}] {u.identifier}: {u.snippet!r}")
        return "\n".join(lines)


def _iter_snippets(result: ExtractionResult):
    """Yield (table, identifier, snippet, page) for every row that should be grounded."""
    for r in result.network_pricing:
        yield "network_pricing", f"{r.pricing_basis}/{r.network}/{r.component}/{r.year}", r.raw_text, r.page
    for r in result.administrative_fees:
        yield "administrative_fees", f"{r.pricing_basis}/{r.year}", r.raw_text, r.page
    for r in result.rebates:
        yield "rebate_guarantees", f"{r.payment_frequency}/{r.network}/{r.year}", r.raw_text, r.page
    for r in result.fees:
        yield "fee_schedule", f"{r.service_name}/{r.variant or '-'}", r.raw_text, r.page
    for r in result.included_services:
        yield "included_services", r.category, r.description, r.page
    for r in result.assumptions:
        yield "assumptions", r.section, r.text, r.page


def verify(result: ExtractionResult, pages: list[Page]) -> GroundingReport:
    corpus_raw = "\n".join(clean_bullets(p.text) for p in pages)
    corpus = normalize(corpus_raw)
    page_corpus = {p.page: normalize(clean_bullets(p.text)) for p in pages}

    report = GroundingReport()

    # ---- precision (+ advisory page-attribution) ----
    for table, ident, snippet, page in _iter_snippets(result):
        report.checked += 1
        snip = normalize(snippet)
        if snip and snip in corpus:
            report.grounded += 1
            # Advisory: grounded somewhere, but is it on the page the row cites?
            if snip not in page_corpus.get(page, ""):
                report.page_mismatches.append(Ungrounded(table, f"{ident} (cited p{page})", snippet))
        else:
            report.ungrounded.append(Ungrounded(table, ident, snippet))

    # ---- recall ----
    tokens = set(_PRICE_RE.findall(corpus_raw)) | set(_AWP_RE.findall(corpus_raw))
    captured_blob = normalize(" ".join(
        s for _, _, s, _ in _iter_snippets(result)
    ))
    report.source_tokens = len(tokens)
    for tok in sorted(tokens):
        if normalize(tok) in captured_blob:
            report.captured_tokens += 1
        else:
            report.uncaptured_tokens.append(tok)

    return report


def check_structure(result: ExtractionResult) -> list[str]:
    """Structural integrity checks that grounding/coverage can't catch.

    Grounding is per-row (is this value real?) and coverage is per-token (did we
    capture every priced token?). Neither notices a malformed GRID: a duplicated
    key, or a (basis, network, component) cell missing a year that its siblings
    have. A duplicate usually implies a missing cell, and a missing cell silently
    drops a real value. Both are fidelity failures, so the pipeline gates on them.

    Holes are inferred from the data itself (the set of years present per basis),
    so this stays correct for a different vendor in the same format.
    """
    from collections import defaultdict

    issues: list[str] = []

    def find_dups(name, items, keyfn):
        counts: dict = defaultdict(int)
        for it in items:
            counts[keyfn(it)] += 1
        for key, n in counts.items():
            if n > 1:
                issues.append(f"duplicate {name} key x{n}: {key}")

    find_dups("network_pricing", result.network_pricing,
              lambda r: (r.pricing_basis, r.network, r.component, r.year))
    find_dups("administrative_fees", result.administrative_fees,
              lambda r: (r.pricing_basis, r.year))
    find_dups("rebate_guarantees", result.rebates,
              lambda r: (r.payment_frequency, r.network, r.year))
    find_dups("fee_schedule", result.fees,
              lambda r: (r.category, r.service_name, r.variant))

    # network_pricing grid: each (basis, network, component) should cover all
    # years seen for that basis.
    basis_years: dict = defaultdict(set)
    np_cell: dict = defaultdict(set)
    for r in result.network_pricing:
        basis_years[r.pricing_basis].add(r.year)
        np_cell[(r.pricing_basis, r.network, r.component)].add(r.year)
    for (basis, net, comp), yrs in np_cell.items():
        for y in sorted(basis_years[basis] - yrs):
            issues.append(f"network_pricing missing: {basis}/{net}/{comp}/{y}")

    # rebate grid: each (frequency, network) should cover all years seen for that frequency.
    freq_years: dict = defaultdict(set)
    rb_cell: dict = defaultdict(set)
    for r in result.rebates:
        freq_years[r.payment_frequency].add(r.year)
        rb_cell[(r.payment_frequency, r.network)].add(r.year)
    for (freq, net), yrs in rb_cell.items():
        for y in sorted(freq_years[freq] - yrs):
            issues.append(f"rebate_guarantees missing: {freq}/{net}/{y}")

    return issues
