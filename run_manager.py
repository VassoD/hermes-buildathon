"""Phase 2 test script: run the Manager over all open tickets.

Run with: python run_manager.py
"""

from db import get_connection
from manager import ClassificationError, process_ticket
from llm import LLMError

# The two deliberately mixed seed tickets, keyed by subject, with the set of
# categories a reasonable Manager could defensibly pick as primary.
AMBIGUOUS_TICKETS = {
    "Charged twice AND the feature doesn't work": {"billing", "technical"},
    "Still locked out, want this month refunded": {"technical", "refund"},
}


def main() -> None:
    conn = get_connection()
    try:
        tickets = conn.execute(
            "SELECT * FROM tickets WHERE status = 'open' ORDER BY id"
        ).fetchall()
        if not tickets:
            print("No open tickets found. Run seed_data.py first (or reset statuses).")
            return

        print(f"Processing {len(tickets)} open tickets through the Manager...\n")
        results = []
        for ticket in tickets:
            try:
                classification, routing = process_ticket(conn, ticket)
            except (LLMError, ClassificationError) as error:
                print(f"  ticket {ticket['id']}: FAILED ({error})")
                continue
            results.append((ticket, classification, routing))
            print(
                f"  ticket {ticket['id']:>2}: {classification.category:<9} "
                f"-> {','.join(routing.targets)}"
            )

        print("\n" + "=" * 100)
        print(f"{'ticket_id':>9} | {'subject':<46} | {'category':<9} | routing_target")
        print("-" * 100)
        for ticket, classification, routing in results:
            print(
                f"{ticket['id']:>9} | {ticket['subject'][:46]:<46} | "
                f"{classification.category:<9} | {','.join(routing.targets)}"
            )

        print("\n--- Ambiguous ticket check ---")
        defensible_count = 0
        for ticket, classification, _ in results:
            expected = AMBIGUOUS_TICKETS.get(ticket["subject"])
            if expected is None:
                continue
            defensible = classification.category in expected
            defensible_count += defensible
            verdict = "defensible" if defensible else "questionable"
            print(f"\nTicket {ticket['id']}: {ticket['subject']}")
            print(f"  Picked: {classification.category} ({verdict}, plausible set: {sorted(expected)})")
            print(f"  Reasoning: {classification.reasoning}")
        print(
            f"\n{defensible_count}/{len(AMBIGUOUS_TICKETS)} ambiguous tickets "
            "got a defensible primary classification."
        )

        total = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_used), 0) FROM runs"
        ).fetchone()
        print(f"\nObservability: {total[0]} rows in runs table, {total[1]} tokens used total.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
