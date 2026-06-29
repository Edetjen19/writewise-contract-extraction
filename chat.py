"""Q&A chatbot CLI.

    python chat.py                 # interactive REPL
    python chat.py "question"      # one-shot, prints the answer and exits
    python chat.py --vendor X ...  # pick a specific vendor's document

Answers come only from the database via the agent's tools.
"""
from __future__ import annotations

import argparse
import sys

import agent as agent_mod
import db
import tools
from config import load_settings
from extract import get_client


def _make_agent(vendor: str | None):
    settings = load_settings(require_api_key=True, require_db=True)
    conn = db.connect_dict(settings.database_url)
    document_id = tools.resolve_document_id(conn, vendor)
    if document_id is None:
        conn.close()
        raise SystemExit(
            "No document found in the database. Run the pipeline first:\n"
            "  python pipeline.py run assets/Northwind_Pricing_Proposal_SAMPLE.pdf"
        )
    client = get_client(settings.openai_api_key)
    return agent_mod.GroundedAgent(client, settings.openai_model, conn, document_id), conn


def main() -> int:
    parser = argparse.ArgumentParser(description="Grounded Q&A over an extracted pricing proposal.")
    parser.add_argument("question", nargs="*", help="ask one question and exit; omit for a REPL")
    parser.add_argument("--vendor", help="select a specific vendor's document")
    args = parser.parse_args()

    agent, conn = _make_agent(args.vendor)
    try:
        if args.question:
            print(agent.ask(" ".join(args.question)))
            return 0

        doc = agent.system.splitlines()[1].strip()
        print(f"Q&A agent ready ({doc}). Ask about discounts, rebates, fees, inclusions, assumptions.")
        print("Type 'exit' or Ctrl-D to quit.\n")
        while True:
            try:
                q = input("you> ").strip()
            except EOFError:
                print()
                break
            if q.lower() in {"exit", "quit"}:
                break
            if not q:
                continue
            print("\n" + agent.ask(q) + "\n")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
