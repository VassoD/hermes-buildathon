"""Phase 5 entrypoint: full per-ticket pipeline with customer memory.

Run with: python run_pipeline.py

Processes each open ticket END-TO-END in order (memory lookup ->
classify -> route -> specialist draft -> manager review -> finalize)
rather than stage-by-stage, so a customer's earlier tickets are already
finalized and visible to memory when their later tickets are processed.
"""

import json
import uuid

from convex_sync import sync_support_ops
from db import get_connection
from llm import LLMError
from manager import (
    CATEGORY_ROUTING,
    ClassificationError,
    log_run,
    process_ticket,
    ReviewError,
)
from memory import build_history_summary, NO_HISTORY
from run_review import review_and_finalize
from specialists import run_specialist, SpecialistError


def main() -> None:
    conn = get_connection()
    try:
        tickets = conn.execute(
            "SELECT * FROM tickets WHERE status = 'open' ORDER BY id"
        ).fetchall()
        if not tickets:
            print("No open tickets. Syncing the existing support playbook.")
            sync_result = sync_support_ops(conn)
            print(
                f"Convex: {sync_result['status']} · {sync_result['clusters']} clusters · "
                f"{sync_result['macros']} macros"
            )
            return

        print(f"Processing {len(tickets)} tickets end-to-end...\n")
        outcomes = []
        for ticket in tickets:
            run_id = str(uuid.uuid4())
            try:
                history = build_history_summary(conn, ticket)
                log_run(
                    conn,
                    run_id,
                    ticket["id"],
                    step_type="memory_lookup",
                    input_summary=f"customer_email={ticket['customer_email']}",
                    output_summary=history,
                    reasoning=(
                        "Deterministic lookup of this customer's finalized "
                        "tickets (status resolved/escalated), no LLM call."
                    ),
                    cost_estimate=0.0,
                )
                conn.commit()

                classification, routing = process_ticket(
                    conn, ticket, run_id=run_id, customer_history=history
                )
                # Re-read so the specialist and reviewer see category/status
                # updates from process_ticket.
                ticket = conn.execute(
                    "SELECT * FROM tickets WHERE id = ?", (ticket["id"],)
                ).fetchone()

                for target in routing.targets:
                    draft = run_specialist(
                        target,
                        ticket,
                        manager_context=classification.reasoning,
                        customer_history=history,
                    )
                    log_run(
                        conn,
                        run_id,
                        ticket["id"],
                        step_type="specialist_draft",
                        input_summary=f"Ticket #{ticket['id']}: {ticket['subject']}",
                        output_summary=json.dumps(
                            {
                                "proposed_action": draft.proposed_action,
                                "customer_response": draft.customer_response,
                            }
                        ),
                        reasoning=draft.reasoning,
                        tokens_used=draft.tokens_used,
                        cost_estimate=draft.cost_estimate,
                        agent_name=draft.agent_name,
                    )
                    conn.commit()
                    outcome = review_and_finalize(
                        conn, ticket, run_id, draft, customer_history=history
                    )
                    outcomes.append((outcome, history))
            except (LLMError, ClassificationError, ReviewError, SpecialistError) as error:
                print(f"  ticket {ticket['id']}: FAILED ({error})")
                continue

            memory_note = "prior history found" if history != NO_HISTORY else "no history"
            print(
                f"  ticket {ticket['id']:>2}: {outcomes[-1][0].status:<9} "
                f"({outcomes[-1][0].action}, {memory_note})"
            )

        print("\n" + "=" * 110)
        print(
            f"{'ticket_id':>9} | {'subject':<46} | {'final_status':<12} | "
            f"{'final_action':<17} | memory"
        )
        print("-" * 110)
        for outcome, history in outcomes:
            memory_note = NO_HISTORY if history == NO_HISTORY else "history found"
            print(
                f"{outcome.ticket_id:>9} | {outcome.subject[:46]:<46} | "
                f"{outcome.status:<12} | {outcome.action:<17} | {memory_note}"
            )

        totals = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(tokens_used), 0),
                   ROUND(COALESCE(SUM(cost_estimate), 0), 4)
            FROM runs
            """
        ).fetchone()
        print(
            f"\nObservability: {totals[0]} run rows, {totals[1]} tokens, "
            f"${totals[2]} estimated cost."
        )
        sync_result = sync_support_ops(conn)
        print(
            f"Convex: {sync_result['status']} · {sync_result['clusters']} clusters · "
            f"{sync_result['macros']} macros"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
