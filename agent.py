"""The grounded Q&A agent (OpenAI function calling).

Grounding is enforced three ways (see DECISIONS.md):
  1. The agent has NO access to the PDF or its own memory for facts; the only way
     to get a number is to call a database tool.
  2. The system prompt forbids stating any figure not returned by a tool, and
     requires the unit + context (network / year / basis / timing) on every answer.
  3. The tools return `raw_text` + `page`, so answers are traceable to the source.

Ambiguity is handled by design: under-specified questions cause the agent to
fetch all matches (omitting the unknown filter) and present/clarify, rather than
silently guessing one.
"""
from __future__ import annotations

import json

import openai
import psycopg

import tools

MAX_TOKENS = 4096
MAX_TOOL_ITERATIONS = 10


def build_system(overview: dict) -> str:
    doc = overview.get("document") or {}
    return f"""You answer questions about ONE pharmacy-benefit pricing proposal, using ONLY the
provided database tools. This is the contract:
  vendor: {doc.get('vendor_name')}   client: {doc.get('client_name')}   date: {doc.get('proposal_date')}

Available dimensions (use these to interpret and disambiguate questions):
  pricing bases: {overview.get('pricing_bases')}
  networks (discounts): {overview.get('networks')}
  components: {overview.get('components')}
  discount years: {overview.get('pricing_years')}   admin-fee years: {overview.get('administrative_fee_years')}
  rebate networks: {overview.get('rebate_networks')}   rebate payment frequencies: {overview.get('rebate_payment_frequencies')}

GROUNDING RULES (non-negotiable):
- Every number you state (discount, fee, rebate, dollar amount, percentage) MUST come from a
  tool result in THIS conversation. Never recall a figure from memory and never estimate one
  that a tool did not return.
- If the tools return no matching rows, say the contract does not specify it. Do not guess.
- Always include the unit and the context: which network, year, pricing basis, and (for rebates)
  payment timing. Quote precisely, e.g. "AWP minus 21.50%", "$0.45 per claim", "$375.50 per brand drug".
- You may mention the source page (rows include `page`).

HANDLING AMBIGUITY:
- Many questions are under-specified. "What's the brand discount?" does not say which network, year,
  or pricing basis (traditional vs applied-rebates), and those differ a lot. Do NOT pick one silently.
- Instead, either call the tool WITHOUT the missing filter to retrieve all matches and present them
  grouped, or ask a short clarifying question. If only a few rows match, just show them.
- The two pricing bases matter: 'traditional' brand discounts are small (rebates paid separately);
  'applied_rebates' brand discounts are large (rebates baked into the discount). Surface both when relevant.

OTHER GUIDANCE:
- If a keyword search returns no rows, retry with fewer or broader terms (a single distinctive
  word, e.g. "integration" or "audit") before concluding the contract doesn't specify something.
- "Included vs extra-cost": combine list_included_services with search_fee_schedule.
- Computation questions (e.g. estimate total rebate dollars): use estimate_rebate_total and state its assumption.
- Be concise and factual. Prefer a small table or list when showing multiple rows."""


class GroundedAgent:
    def __init__(self, client: openai.OpenAI, model: str,
                 conn: psycopg.Connection, document_id: str):
        self.client = client
        self.model = model
        self.conn = conn
        self.document_id = document_id
        overview = tools.get_document_overview(conn, document_id)
        self.system = build_system(overview)
        self.messages: list = [{"role": "system", "content": self.system}]

    def ask(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})

        for _ in range(MAX_TOOL_ITERATIONS):
            resp = self.client.chat.completions.create(
                model=self.model,
                max_completion_tokens=MAX_TOKENS,
                messages=self.messages,
                tools=tools.OPENAI_TOOLS,
                tool_choice="auto",
            )
            msg = resp.choices[0].message
            self.messages.append(msg)

            if not msg.tool_calls:
                return (msg.content or "").strip()

            for tc in msg.tool_calls:
                if tc.type != "function":
                    continue
                args = json.loads(tc.function.arguments or "{}")
                result = tools.dispatch(self.conn, self.document_id, tc.function.name, args)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                })

        return "(stopped: too many tool iterations without a final answer)"
