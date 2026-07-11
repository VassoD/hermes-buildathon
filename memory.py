"""Per-customer memory: summarize a customer's prior finalized tickets.

The summary is built deterministically from the tickets table (no LLM
call) so the memory_lookup step is cheap, factual, and reproducible.
"""

import sqlite3

NO_HISTORY = "no prior history"
NOTE_SNIPPET_LENGTH = 160


def build_history_summary(conn: sqlite3.Connection, ticket: sqlite3.Row) -> str:
    """Summarize this customer's earlier resolved/escalated tickets.

    Only finalized tickets count as history; open and in_review tickets
    are excluded so the summary never references work still in flight.
    """
    prior = conn.execute(
        """
        SELECT id, subject, category, status, resolution_action,
               resolution_notes, resolved_at
        FROM tickets
        WHERE customer_email = ? AND id != ?
          AND status IN ('resolved', 'escalated')
        ORDER BY id
        """,
        (ticket["customer_email"], ticket["id"]),
    ).fetchall()
    if not prior:
        return NO_HISTORY

    count = len(prior)
    lines = [f"This customer has {count} prior ticket{'s' if count > 1 else ''}:"]
    for row in prior:
        note = (row["resolution_notes"] or "").replace("\n", " ").strip()
        if len(note) > NOTE_SNIPPET_LENGTH:
            note = note[:NOTE_SNIPPET_LENGTH] + "..."
        lines.append(
            f"- Ticket #{row['id']} '{row['subject']}' ({row['category']}): "
            f"{row['status']} on {row['resolved_at']} "
            f"(action: {row['resolution_action']}). Resolution note: {note}"
        )
    return "\n".join(lines)
