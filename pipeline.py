"""Reproducible extraction pipeline:  PDF  ->  extract  ->  verify  ->  Postgres.

Usage:
    python pipeline.py init-db                 # apply schema.sql (idempotent)
    python pipeline.py run <pdf> [options]     # full pipeline
        --no-load            extract + verify only (no DB, no API key for DB needed)
        --allow-ungrounded   load even if some rows fail the grounding check
        --json PATH          also write the validated extraction + report to PATH

The grounding check is a gate: by default nothing is loaded unless every row's
verbatim source string is found in the PDF text.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import db
import load as loader
from config import load_settings
from extract import ExtractionResult, extract_all, get_client
from pdf_text import extract_pages, file_sha256
from verify import GroundingReport, check_structure, verify

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema.sql"


def _result_to_dict(result: ExtractionResult, report: GroundingReport) -> dict:
    return {
        "metadata": result.metadata.model_dump(),
        "counts": result.counts(),
        "network_pricing": [r.model_dump() for r in result.network_pricing],
        "administrative_fees": [r.model_dump() for r in result.administrative_fees],
        "rebates": [r.model_dump() for r in result.rebates],
        "fees": [r.model_dump() for r in result.fees],
        "included_services": [r.model_dump() for r in result.included_services],
        "assumptions": [r.model_dump() for r in result.assumptions],
        "grounding": {
            "checked": report.checked,
            "grounded": report.grounded,
            "ungrounded": [u.__dict__ for u in report.ungrounded],
            "source_tokens": report.source_tokens,
            "captured_tokens": report.captured_tokens,
            "uncaptured_tokens": report.uncaptured_tokens,
        },
    }


def cmd_init_db(_args) -> int:
    settings = load_settings(require_api_key=False, require_db=True)
    conn = db.connect(settings.database_url)
    try:
        db.apply_schema(conn, str(SCHEMA_PATH))
    finally:
        conn.close()
    print(f"Schema applied from {SCHEMA_PATH.name}.")
    return 0


def cmd_run(args) -> int:
    pdf_path = args.pdf
    if not os.path.exists(pdf_path):
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    settings = load_settings(require_api_key=True, require_db=not args.no_load)

    print(f"Reading {pdf_path} ...")
    pages = extract_pages(pdf_path)
    print(f"  {len(pages)} pages.")

    client = get_client(settings.openai_api_key)

    # Extract, verify, and self-heal: re-extract if grounding or structural checks
    # fail (LLM extraction is nondeterministic, so a fresh attempt usually clears
    # an occasional duplicate/missing-cell). Abort if it can't converge.
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        print(f"Extracting with {settings.openai_model} (attempt {attempt}/{max_attempts}) ...")
        result = extract_all(client, settings.openai_model, pages)
        print("Extracted:", json.dumps(result.counts()))
        report = verify(result, pages)
        structural = check_structure(result)
        if report.all_grounded and not structural and not report.uncaptured_tokens:
            break
        print("Verification found issues:")
        print(report.summary())
        for s in structural:
            print(f"  STRUCTURE: {s}")
        if attempt < max_attempts:
            print("Re-extracting to resolve nondeterministic issues ...\n")

    print("\n== Verification ==")
    print(report.summary())
    for s in structural:
        print(f"  STRUCTURE: {s}")

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(_result_to_dict(result, report), indent=2))
        print(f"Wrote {args.json}")

    if (not report.all_grounded or structural or report.uncaptured_tokens) and not args.allow_ungrounded:
        reasons = []
        if not report.all_grounded:
            reasons.append(f"{len(report.ungrounded)} ungrounded row(s)")
        if structural:
            reasons.append(f"{len(structural)} structural issue(s)")
        if report.uncaptured_tokens:
            reasons.append(f"{len(report.uncaptured_tokens)} uncaptured priced token(s)")
        print(
            f"\nABORT: {', '.join(reasons)} after {max_attempts} attempt(s). "
            f"Nothing loaded. Re-run with --allow-ungrounded to override.",
            file=sys.stderr,
        )
        return 1

    if args.no_load:
        print("\n--no-load set: skipping database load.")
        return 0

    conn = db.connect(settings.database_url)
    try:
        db.apply_schema(conn, str(SCHEMA_PATH))  # ensure tables exist (idempotent)
        doc_id = loader.load(
            conn, result,
            source_filename=os.path.basename(pdf_path),
            source_sha256=file_sha256(pdf_path),
            pages=pages,
        )
    finally:
        conn.close()
    print(f"\nLoaded document {doc_id} into the database.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PDF -> structured pricing data -> Postgres.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="apply schema.sql").set_defaults(func=cmd_init_db)

    run = sub.add_parser("run", help="extract a PDF and load it")
    run.add_argument("pdf", help="path to the proposal PDF")
    run.add_argument("--no-load", action="store_true", help="extract + verify only")
    run.add_argument("--allow-ungrounded", action="store_true", help="load even if grounding fails")
    run.add_argument("--json", metavar="PATH", help="write validated extraction + report to PATH")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
