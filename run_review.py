"""Phase 4 test script: Manager reviews specialist drafts, then finalizes.

Run with: python run_review.py
Requires run_manager.py and run_specialists.py to have run first.

Flow per ticket: manager_review -> (if REVISE) specialist_revision ->
manager_review_2 -> APPROVE finalizes, a second REVISE auto-escalates.
Approved 'escalate' actions finalize as status='escalated', everything
else as status='resolved'.
"""

import json
import sqlite3
from dataclasses import dataclass

from actions import execute_action, persist_execution, status_for_execution
from db import get_connection
from llm import LLMError
from manager import log_run, review_draft, ReviewError
from specialists import run_specialist, SpecialistDraft, SpecialistError

MAX_REVISION_ATTEMPTS = 1


@dataclass
class FinalOutcome:
    ticket_id: int
    subject: str
    status: str
    action: str
    approved_first_pass: bool


def latest_drafts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Latest specialist_draft row per (ticket, specialist), in_review only."""
    return conn.execute(
        """
        SELECT r.* FROM runs r
        JOIN tickets t ON t.id = r.ticket_id
        WHERE r.step_type = 'specialist_draft'
          AND t.status = 'in_review'
          AND r.id = (
            SELECT MAX(id) FROM runs
            WHERE ticket_id = r.ticket_id
              AND agent_name = r.agent_name
              AND step_type = 'specialist_draft'
          )
        ORDER BY r.ticket_id
        """
    ).fetchall()


def draft_from_run_row(row: sqlite3.Row) -> SpecialistDraft:
    payload = json.loads(row["output_summary"])
    return SpecialistDraft(
        agent_name=row["agent_name"],
        proposed_action=payload["proposed_action"],
        customer_response=payload["customer_response"],
        reasoning=row["reasoning"],
        tokens_used=0,
        cost_estimate=None,
    )


def log_review(
    conn: sqlite3.Connection,
    run_id: str,
    ticket_id: int,
    step_type: str,
    draft: SpecialistDraft,
    verdict,
) -> None:
    log_run(
        conn,
        run_id,
        ticket_id,
        step_type=step_type,
        input_summary=f"{draft.agent_name} draft: action={draft.proposed_action}",
        output_summary=f"verdict={verdict.verdict}",
        reasoning=(
            verdict.reasoning
            + (f"\nRevision notes: {verdict.revision_notes}" if verdict.revision_notes else "")
        ),
        tokens_used=verdict.tokens_used,
        cost_estimate=verdict.cost_estimate,
    )


def finalize(
    conn: sqlite3.Connection,
    ticket: sqlite3.Row,
    run_id: str,
    action: str,
    notes: str,
    dodo_client=None,
) -> str:
    """Execute an approved action, store provider proof, then update the ticket."""
    execution = execute_action(ticket, action, notes, dodo_client=dodo_client)
    persist_execution(conn, run_id, ticket["id"], action, execution)
    log_run(
        conn,
        run_id,
        ticket["id"],
        step_type="action_execute",
        input_summary=f"approved_action={action}",
        output_summary=(
            f"provider={execution.provider}; status={execution.status}; "
            f"external_id={execution.external_id or '-'}"
        ),
        reasoning=execution.detail,
        cost_estimate=0.0,
        agent_name="ActionExecutor",
    )

    status = status_for_execution(action, execution)
    customer_notes = notes
    if action == "issue_refund" and execution.status == "pending":
        customer_notes = (
            "Your refund was submitted to our payment provider and is pending. "
            "We will follow up if the provider needs anything else."
        )
    elif action == "issue_refund" and execution.status != "succeeded":
        customer_notes = (
            "Your refund was approved, but we could not complete it automatically. "
            "The case has been escalated with full context for a human to finish."
        )

    conn.execute(
        """
        UPDATE tickets
        SET status = ?, resolution_action = ?, resolution_notes = ?,
            resolved_at = CASE
                WHEN ? IN ('resolved', 'escalated') THEN CURRENT_TIMESTAMP
                ELSE NULL
            END
        WHERE id = ?
        """,
        (status, action, customer_notes, status, ticket["id"]),
    )
    conn.commit()
    return status


def process_review(conn: sqlite3.Connection, draft_row: sqlite3.Row) -> FinalOutcome:
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (draft_row["ticket_id"],)
    ).fetchone()
    return review_and_finalize(
        conn, ticket, draft_row["run_id"], draft_from_run_row(draft_row)
    )


def review_and_finalize(
    conn: sqlite3.Connection,
    ticket: sqlite3.Row,
    run_id: str,
    draft: SpecialistDraft,
    customer_history: str | None = None,
    dodo_client=None,
) -> FinalOutcome:
    verdict = review_draft(
        ticket,
        draft.agent_name,
        draft.proposed_action,
        draft.customer_response,
        draft.reasoning,
        customer_history=customer_history,
    )
    log_review(conn, run_id, ticket["id"], "manager_review", draft, verdict)
    conn.commit()

    if verdict.approved:
        status = finalize(
            conn,
            ticket,
            run_id,
            draft.proposed_action,
            draft.customer_response,
            dodo_client=dodo_client,
        )
        return FinalOutcome(ticket["id"], ticket["subject"], status, draft.proposed_action, True)

    print(f"  ticket {ticket['id']:>2}: REVISE -> {verdict.revision_notes[:70]}")
    revised = run_specialist(
        draft.agent_name,
        ticket,
        manager_context=f"Revision requested by Manager. Notes: {verdict.revision_notes}",
        previous_draft=draft,
        revision_notes=verdict.revision_notes,
        customer_history=customer_history,
    )
    log_run(
        conn,
        run_id,
        ticket["id"],
        step_type="specialist_revision",
        input_summary=f"Revision notes: {verdict.revision_notes[:120]}",
        output_summary=json.dumps(
            {
                "proposed_action": revised.proposed_action,
                "customer_response": revised.customer_response,
            }
        ),
        reasoning=revised.reasoning,
        tokens_used=revised.tokens_used,
        cost_estimate=revised.cost_estimate,
        agent_name=revised.agent_name,
    )
    conn.commit()

    second_verdict = review_draft(
        ticket,
        revised.agent_name,
        revised.proposed_action,
        revised.customer_response,
        revised.reasoning,
        customer_history=customer_history,
        is_second_pass=True,
    )
    log_review(conn, run_id, ticket["id"], "manager_review_2", revised, second_verdict)
    conn.commit()

    if second_verdict.approved:
        status = finalize(
            conn,
            ticket,
            run_id,
            revised.proposed_action,
            revised.customer_response,
            dodo_client=dodo_client,
        )
        return FinalOutcome(ticket["id"], ticket["subject"], status, revised.proposed_action, False)

    notes = (
        "Auto-escalated: draft failed Manager review twice. "
        f"Last revision notes: {second_verdict.revision_notes}"
    )
    status = finalize(
        conn, ticket, run_id, "escalate", notes, dodo_client=dodo_client
    )
    return FinalOutcome(ticket["id"], ticket["subject"], status, "escalate", False)


def main() -> None:
    conn = get_connection()
    try:
        draft_rows = latest_drafts(conn)
        if not draft_rows:
            print("No drafts to review. Run run_manager.py then run_specialists.py first.")
            return

        print(f"Manager reviewing {len(draft_rows)} specialist drafts...\n")
        outcomes = []
        for draft_row in draft_rows:
            try:
                outcome = process_review(conn, draft_row)
            except (LLMError, ReviewError, SpecialistError) as error:
                print(f"  ticket {draft_row['ticket_id']}: FAILED ({error})")
                continue
            outcomes.append(outcome)
            first = "first pass" if outcome.approved_first_pass else "after revision"
            print(
                f"  ticket {outcome.ticket_id:>2}: {outcome.status:<9} "
                f"({outcome.action}, {first})"
            )

        print("\n" + "=" * 100)
        print(
            f"{'ticket_id':>9} | {'subject':<46} | {'final_status':<12} | "
            f"{'final_action':<17} | first_pass"
        )
        print("-" * 100)
        for o in outcomes:
            print(
                f"{o.ticket_id:>9} | {o.subject[:46]:<46} | {o.status:<12} | "
                f"{o.action:<17} | {'yes' if o.approved_first_pass else 'no'}"
            )

        resolved = sum(o.status == "resolved" for o in outcomes)
        escalated = sum(o.status == "escalated" for o in outcomes)
        first_pass = sum(o.approved_first_pass for o in outcomes)
        print(
            f"\n{resolved} resolved, {escalated} escalated, "
            f"{first_pass}/{len(outcomes)} approved on first pass."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
