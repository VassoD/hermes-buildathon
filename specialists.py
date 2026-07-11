"""Specialist agents: BillingAgent, TechnicalAgent, RefundAgent.

Each specialist drafts a proposed resolution for a ticket routed to it by
the Manager. Phase 3 scope: drafts are logged to the runs table only,
nothing is written back to the tickets table and the Manager does not
review drafts yet (phase 4).
"""

import sqlite3
from dataclasses import dataclass

from llm import complete, extract_json, LLMError

VALID_ACTIONS = (
    "issue_refund",
    "deny_refund",
    "escalate",
    "reply_only",
    "request_more_info",
)

OUTPUT_FORMAT_INSTRUCTIONS = """\
Think through the ticket, then respond with a single JSON object, reasoning
first. An issue_refund action is executable through Dodo Payments only when
the ticket includes a payment reference beginning with pay_. If a refund is
appropriate but that reference is missing, choose request_more_info and ask
for the Dodo payment ID instead of claiming that a refund was issued.
{"reasoning": "<2-4 sentences of internal reasoning for your decision, not shown to the customer>",
 "proposed_action": "issue_refund" | "deny_refund" | "escalate" | "reply_only" | "request_more_info",
 "customer_response": "<the full draft reply to send to the customer, friendly and professional>"}
"""

SPECIALIST_PROMPTS: dict[str, str] = {
    "BillingAgent": f"""\
You are BillingAgent, the billing specialist on a customer support crew.
You handle charges, duplicate payments, plan tiers, invoices, failed
payments, and subscription changes.

Guidelines:
- Duplicate or erroneous charges: propose correcting the charge
  (proposed_action 'issue_refund' for the erroneous amount).
- Wrong plan tier or invoice requests: usually 'reply_only' with the fix or
  the document the customer needs.
- If account details you cannot see (payment processor logs, card status)
  are required to act, use 'request_more_info' or 'escalate'.
- Never invent specific amounts, dates, or account details not present in
  the ticket.

{OUTPUT_FORMAT_INSTRUCTIONS}""",
    "TechnicalAgent": f"""\
You are TechnicalAgent, the technical support specialist on a customer
support crew. You handle bugs, login failures, crashes, broken
integrations, and data sync issues.

Guidelines:
- Provide concrete troubleshooting steps when the ticket gives enough
  detail ('reply_only').
- Ask targeted diagnostic questions when it does not
  ('request_more_info').
- Escalate to engineering ('escalate') for likely product bugs,
  regressions after a release, or anything you cannot resolve with
  troubleshooting steps.
- Never promise a fix timeline you cannot know.

{OUTPUT_FORMAT_INSTRUCTIONS}""",
    "RefundAgent": f"""\
You are RefundAgent, the refund eligibility specialist on a customer
support crew.

Refund policy you must apply:
- Refunds are APPROVED by default within 30 days of purchase.
- After 30 days, refunds are DENIED by default, UNLESS there is a clear
  service failure on our side (outage, product not delivered, billing
  error such as a duplicate charge).
- Duplicate purchases are treated as a billing error and are refundable
  regardless of the 30-day window.

Guidelines:
- Apply the policy to the facts in the ticket. If purchase timing is
  unclear and it changes the outcome, use 'request_more_info'.
- Use 'escalate' for borderline cases where an exception to policy is
  being requested and a human should decide.
- Be empathetic when denying, and state the policy reason plainly.

{OUTPUT_FORMAT_INSTRUCTIONS}""",
}


class SpecialistError(Exception):
    """Raised when a specialist cannot produce a valid draft."""


@dataclass
class SpecialistDraft:
    agent_name: str
    proposed_action: str
    customer_response: str
    reasoning: str
    tokens_used: int
    cost_estimate: float | None


def run_specialist(
    agent_name: str,
    ticket: sqlite3.Row,
    manager_context: str,
    previous_draft: "SpecialistDraft | None" = None,
    revision_notes: str | None = None,
    customer_history: str | None = None,
) -> SpecialistDraft:
    """Have one specialist draft (or revise) a resolution for a ticket."""
    system_prompt = SPECIALIST_PROMPTS.get(agent_name)
    if system_prompt is None:
        raise SpecialistError(f"Unknown specialist: {agent_name!r}")

    user_prompt = (
        f"Ticket #{ticket['id']} from {ticket['customer_email']}\n"
        f"Subject: {ticket['subject']}\n"
        f"Body: {ticket['body']}\n\n"
        f"Customer history: {customer_history or 'none on file'}\n\n"
        f"Manager's routing context: {manager_context or 'none provided'}"
    )
    if previous_draft is not None and revision_notes:
        user_prompt += (
            "\n\nYour previous draft was reviewed by the Manager and sent "
            "back for revision.\n"
            f"Previous proposed_action: {previous_draft.proposed_action}\n"
            f"Previous customer_response: {previous_draft.customer_response}\n"
            f"Manager's revision notes: {revision_notes}\n"
            "Produce a revised draft that addresses these notes."
        )
    result = complete(system_prompt, user_prompt)
    parsed = extract_json(result.text)

    proposed_action = str(parsed.get("proposed_action", "")).strip().lower()
    if proposed_action not in VALID_ACTIONS:
        raise SpecialistError(
            f"{agent_name} returned invalid action {proposed_action!r} "
            f"for ticket {ticket['id']}"
        )
    customer_response = str(parsed.get("customer_response", "")).strip()
    reasoning = str(parsed.get("reasoning", "")).strip()
    if not customer_response:
        raise SpecialistError(
            f"{agent_name} returned an empty customer_response for ticket {ticket['id']}"
        )

    return SpecialistDraft(
        agent_name=agent_name,
        proposed_action=proposed_action,
        customer_response=customer_response,
        reasoning=reasoning,
        tokens_used=result.total_tokens,
        cost_estimate=result.cost_estimate,
    )
