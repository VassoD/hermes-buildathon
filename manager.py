"""Manager agent: classifies open tickets and decides routing.

Phase 2 scope: classification (LLM) + routing decision + observability
logging. Specialists do not exist yet, so routed tickets stop at
status='in_review'.
"""

import sqlite3
import uuid
from dataclasses import dataclass

from llm import complete, extract_json, LLMError

AGENT_NAME = "Manager"
VALID_CATEGORIES = ("billing", "technical", "refund")

# Routing is 1:1 with category for now. Values are lists so a future phase
# can route one ticket to several specialists, and 'Manager' is a valid
# target for tickets the Manager should handle directly (phase 4 bounce-back).
CATEGORY_ROUTING: dict[str, list[str]] = {
    "billing": ["BillingAgent"],
    "technical": ["TechnicalAgent"],
    "refund": ["RefundAgent"],
}

CLASSIFY_SYSTEM_PROMPT = """\
You are the Manager of a customer support triage crew. You read a support
ticket and classify it into exactly one PRIMARY category:

- billing: charges, invoices, plan tiers, payment failures, subscription changes
- technical: bugs, login problems, crashes, broken integrations, sync issues
- refund: the customer's main request is to get money back and the core
  question is whether they are ELIGIBLE for a refund under policy

Some tickets touch multiple categories. Do not pick the first keyword you
see. Decide which category represents the PRIMARY action our team must take
first to resolve the ticket, and explain your reasoning BEFORE committing to
a label.

Respond with a single JSON object, reasoning first:
{"reasoning": "<2-4 sentences: what the customer needs, which categories apply, why one is primary>",
 "category": "billing" | "technical" | "refund"}
"""


REVIEW_SYSTEM_PROMPT = """\
You are the Manager of a customer support triage crew, reviewing a draft
resolution written by one of your specialists before it is sent to the
customer. Evaluate the draft against this checklist:

1. Is the proposed_action consistent with the specialist's own reasoning?
2. Does the customer_response actually address what the customer asked?
3. For refund-related actions: does it correctly apply our policy?
   Refunds are approved by default within 30 days of purchase, denied by
   default after 30 days unless there is a clear service failure on our
   side, and duplicate purchases are refundable regardless of the window.
4. Is the tone appropriate (not curt, not overpromising)?

Important: 'escalate' is a legitimate final action. If the specialist
escalated for a sound reason (borderline policy exception, likely product
bug, needs human judgment), APPROVE it. Do not send escalations back just
because they do not resolve the ticket outright.

Only choose REVISE for a specific, named problem. Your revision notes must
be concrete instructions the specialist can act on, never a vague "make it
better". Minor stylistic preferences are not grounds for revision.

Respond with a single JSON object, reasoning first:
{"reasoning": "<2-4 sentences walking the checklist>",
 "verdict": "APPROVE" | "REVISE",
 "revision_notes": "<required when verdict is REVISE, empty string otherwise>"}
"""


class ClassificationError(Exception):
    """Raised when the model cannot produce a valid classification."""


class ReviewError(Exception):
    """Raised when the model cannot produce a valid review verdict."""


@dataclass
class Classification:
    category: str
    reasoning: str
    tokens_used: int
    cost_estimate: float | None


@dataclass
class RoutingDecision:
    targets: list[str]
    reasoning: str


@dataclass
class ReviewVerdict:
    verdict: str
    revision_notes: str
    reasoning: str
    tokens_used: int
    cost_estimate: float | None

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVE"


def classify_ticket(
    ticket: sqlite3.Row, customer_history: str | None = None
) -> Classification:
    user_prompt = (
        f"Ticket #{ticket['id']}\n"
        f"Subject: {ticket['subject']}\n"
        f"Body: {ticket['body']}\n\n"
        f"Customer history: {customer_history or 'none on file'}"
    )
    result = complete(CLASSIFY_SYSTEM_PROMPT, user_prompt)
    parsed = extract_json(result.text)
    category = str(parsed.get("category", "")).strip().lower()
    reasoning = str(parsed.get("reasoning", "")).strip()
    if category not in VALID_CATEGORIES:
        raise ClassificationError(
            f"Model returned invalid category {category!r} for ticket {ticket['id']}"
        )
    return Classification(
        category=category,
        reasoning=reasoning,
        tokens_used=result.total_tokens,
        cost_estimate=result.cost_estimate,
    )


def review_draft(
    ticket: sqlite3.Row,
    agent_name: str,
    proposed_action: str,
    customer_response: str,
    specialist_reasoning: str,
    customer_history: str | None = None,
    is_second_pass: bool = False,
) -> ReviewVerdict:
    """Manager reviews one specialist draft and returns a verdict."""
    user_prompt = (
        f"Ticket #{ticket['id']} from {ticket['customer_email']}\n"
        f"Subject: {ticket['subject']}\n"
        f"Body: {ticket['body']}\n\n"
        f"Customer history: {customer_history or 'none on file'}\n\n"
        f"Draft under review (by {agent_name}"
        f"{', already revised once at your request' if is_second_pass else ''}):\n"
        f"proposed_action: {proposed_action}\n"
        f"customer_response: {customer_response}\n"
        f"specialist reasoning: {specialist_reasoning}"
    )
    result = complete(REVIEW_SYSTEM_PROMPT, user_prompt)
    parsed = extract_json(result.text)
    verdict = str(parsed.get("verdict", "")).strip().upper()
    revision_notes = str(parsed.get("revision_notes", "")).strip()
    if verdict not in ("APPROVE", "REVISE"):
        raise ReviewError(
            f"Model returned invalid verdict {verdict!r} for ticket {ticket['id']}"
        )
    if verdict == "REVISE" and not revision_notes:
        raise ReviewError(
            f"Model asked for revision without notes on ticket {ticket['id']}"
        )
    return ReviewVerdict(
        verdict=verdict,
        revision_notes=revision_notes,
        reasoning=str(parsed.get("reasoning", "")).strip(),
        tokens_used=result.total_tokens,
        cost_estimate=result.cost_estimate,
    )


def decide_routing(classification: Classification) -> RoutingDecision:
    targets = CATEGORY_ROUTING[classification.category]
    reasoning = (
        f"Category '{classification.category}' maps to {', '.join(targets)}. "
        "Single-target routing in phase 2; multi-target and Manager-handled "
        "routing become possible in phase 4."
    )
    return RoutingDecision(targets=targets, reasoning=reasoning)


def log_run(
    conn: sqlite3.Connection,
    run_id: str,
    ticket_id: int,
    step_type: str,
    input_summary: str,
    output_summary: str,
    reasoning: str,
    tokens_used: int = 0,
    cost_estimate: float | None = None,
    agent_name: str = AGENT_NAME,
) -> None:
    conn.execute(
        """
        INSERT INTO runs (run_id, ticket_id, agent_name, step_type,
                          input_summary, output_summary, reasoning,
                          tokens_used, cost_estimate)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            ticket_id,
            agent_name,
            step_type,
            input_summary,
            output_summary,
            reasoning,
            tokens_used,
            cost_estimate,
        ),
    )


def process_ticket(
    conn: sqlite3.Connection,
    ticket: sqlite3.Row,
    run_id: str | None = None,
    customer_history: str | None = None,
) -> tuple[Classification, RoutingDecision]:
    """Classify one ticket, decide routing, persist both plus run logs."""
    run_id = run_id or str(uuid.uuid4())
    input_summary = f"Ticket #{ticket['id']}: {ticket['subject']}"

    classification = classify_ticket(ticket, customer_history)
    log_run(
        conn,
        run_id,
        ticket["id"],
        step_type="classify",
        input_summary=input_summary,
        output_summary=f"category={classification.category}",
        reasoning=classification.reasoning,
        tokens_used=classification.tokens_used,
        cost_estimate=classification.cost_estimate,
    )

    routing = decide_routing(classification)
    log_run(
        conn,
        run_id,
        ticket["id"],
        step_type="route",
        input_summary=f"category={classification.category}",
        output_summary=f"route_to={','.join(routing.targets)}",
        reasoning=routing.reasoning,
        cost_estimate=0.0,
    )

    conn.execute(
        "UPDATE tickets SET category = ?, status = 'in_review' WHERE id = ?",
        (classification.category, ticket["id"]),
    )
    conn.commit()
    return classification, routing
