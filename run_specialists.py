"""Phase 3 test script: hand routed tickets to their specialist(s).

Run with: python run_specialists.py
Requires run_manager.py to have run first (tickets must be classified and
in_review). Drafts are logged to the runs table only, tickets are not
updated, Manager review comes in phase 4.
"""

import json
import sqlite3

from db import get_connection
from llm import LLMError
from manager import CATEGORY_ROUTING, log_run
from specialists import run_specialist, SpecialistError


def manager_classify_context(conn: sqlite3.Connection, ticket_id: int) -> tuple[str, str]:
    """Return (run_id, reasoning) from the Manager's latest classify step."""
    row = conn.execute(
        """
        SELECT run_id, reasoning FROM runs
        WHERE ticket_id = ? AND step_type = 'classify'
        ORDER BY id DESC LIMIT 1
        """,
        (ticket_id,),
    ).fetchone()
    if row is None:
        raise SpecialistError(
            f"Ticket {ticket_id} has no classify run. Run run_manager.py first."
        )
    return row["run_id"], row["reasoning"]


def main() -> None:
    conn = get_connection()
    try:
        tickets = conn.execute(
            "SELECT * FROM tickets WHERE status = 'in_review' ORDER BY id"
        ).fetchall()
        if not tickets:
            print("No in_review tickets found. Run run_manager.py first.")
            return

        print(f"Handing {len(tickets)} routed tickets to specialists...\n")
        drafts = []
        for ticket in tickets:
            run_id, manager_reasoning = manager_classify_context(conn, ticket["id"])
            targets = CATEGORY_ROUTING[ticket["category"]]
            for target in targets:
                try:
                    draft = run_specialist(target, ticket, manager_reasoning)
                except (LLMError, SpecialistError) as error:
                    print(f"  ticket {ticket['id']}: {target} FAILED ({error})")
                    continue
                log_run(
                    conn,
                    run_id,
                    ticket["id"],
                    step_type="specialist_draft",
                    input_summary=f"Ticket #{ticket['id']}: {ticket['subject']}",
                    # Full draft as JSON so the phase 4 review step can read
                    # it back from the DB.
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
                drafts.append((ticket, draft))
                print(
                    f"  ticket {ticket['id']:>2}: {draft.agent_name:<14} "
                    f"-> {draft.proposed_action}"
                )

        print("\n" + "=" * 120)
        print(
            f"{'ticket_id':>9} | {'specialist':<14} | {'proposed_action':<17} | draft response (first 80 chars)"
        )
        print("-" * 120)
        for ticket, draft in drafts:
            preview = draft.customer_response[:80].replace("\n", " ")
            print(
                f"{ticket['id']:>9} | {draft.agent_name:<14} | "
                f"{draft.proposed_action:<17} | {preview}"
            )

        totals = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(tokens_used), 0),
                   ROUND(COALESCE(SUM(cost_estimate), 0), 4)
            FROM runs WHERE step_type = 'specialist_draft'
            """
        ).fetchone()
        print(
            f"\nObservability: {totals[0]} specialist_draft rows, "
            f"{totals[1]} tokens, ${totals[2]} estimated cost."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
