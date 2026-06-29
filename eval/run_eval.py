"""Run the eval Q&A pairs against the agent and report pass/fail.

    python eval/run_eval.py

Grades by substring presence (comma-insensitive) of the expected grounded values
in the agent's answer. A fresh agent runs per question so there's no cross-bleed.
"""
from __future__ import annotations

import os
import sys

import yaml

# allow running from repo root: import the top-level modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import db  # noqa: E402
import tools  # noqa: E402
from config import load_settings  # noqa: E402
from extract import get_client  # noqa: E402

PAIRS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qa_pairs.yaml")


def _norm(s: str) -> str:
    return s.lower().replace(",", "")


def _contains(answer: str, needle: str) -> bool:
    return _norm(needle) in _norm(answer)


def main() -> int:
    with open(PAIRS) as f:
        pairs = yaml.safe_load(f)

    settings = load_settings(require_api_key=True, require_db=True)
    conn = db.connect_dict(settings.database_url)
    document_id = tools.resolve_document_id(conn)
    if document_id is None:
        raise SystemExit("No document loaded. Run the pipeline first.")
    client = get_client(settings.openai_api_key)

    passed = 0
    for p in pairs:
        agent = agent_mod.GroundedAgent(client, settings.openai_model, conn, document_id)
        answer = agent.ask(p["question"])

        all_ok = all(_contains(answer, s) for s in p.get("expect_all", []))
        any_ok = (not p.get("expect_any")) or any(_contains(answer, s) for s in p["expect_any"])
        ok = all_ok and any_ok
        passed += ok

        print(f"[{'PASS' if ok else 'FAIL'}] {p['id']}")
        print(f"   Q: {p['question']}")
        print(f"   A: {answer.replace(chr(10), ' ')[:300]}")
        if not ok:
            missing = [s for s in p.get("expect_all", []) if not _contains(answer, s)]
            if missing:
                print(f"   missing (expect_all): {missing}")
            if p.get("expect_any") and not any_ok:
                print(f"   none of (expect_any): {p['expect_any']}")
        print()

    conn.close()
    print(f"{passed}/{len(pairs)} passed.")
    return 0 if passed == len(pairs) else 1


if __name__ == "__main__":
    sys.exit(main())
