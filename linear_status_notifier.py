"""Notify Telegram customers when linked Linear engineering issues are completed."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable

from agency_integrations import LinearClient
from db import get_connection
from telegram_intake import Narrator

TELEGRAM_CUSTOMER = re.compile(r"^telegram_(\d+)@demo\.local$")


def _send_telegram(
    chat_id: str,
    text: str,
    conn: sqlite3.Connection,
    ticket: sqlite3.Row,
) -> None:
    Narrator(
        chat_id,
        dry_run=False,
        conn=conn,
        ticket_id=ticket["id"],
        customer_email=ticket["customer_email"],
    ).say(text)


def notify_completed_issues(
    conn: sqlite3.Connection,
    linear_client: LinearClient | None = None,
    *,
    sender: Callable[[str, str, sqlite3.Connection, sqlite3.Row], None] = _send_telegram,
) -> int:
    """Send each originating Telegram user one notification per completed issue."""
    client = linear_client or LinearClient()
    rows = conn.execute(
        """
        SELECT t.*, i.linear_issue_id, i.linear_issue_url
        FROM ticket_investigations i
        JOIN tickets t ON t.id = i.ticket_id
        LEFT JOIN engineering_notifications n
          ON n.linear_issue_id = i.linear_issue_id
         AND n.event_type = 'completed'
        WHERE i.linear_issue_id IS NOT NULL
          AND n.id IS NULL
        ORDER BY i.created_at, i.id
        """
    ).fetchall()
    sent = 0
    seen = set()
    for ticket in rows:
        issue_id = ticket["linear_issue_id"]
        if issue_id in seen:
            continue
        seen.add(issue_id)
        match = TELEGRAM_CUSTOMER.fullmatch(ticket["customer_email"])
        if match is None:
            continue
        issue = client.get_issue(issue_id)
        state = issue.get("state") or {}
        if str(state.get("type", "")).lower() != "completed":
            continue
        message = (
            f"✅ Engineering update: {issue['identifier']} — {issue['title']} "
            "has been marked Done.\n\n"
            "The engineering work on the issue you reported is complete. "
            "Thank you for helping us identify it.\n\n"
            f"Engineering issue: {issue['url']}"
        )
        sender(match.group(1), message, conn, ticket)
        conn.execute(
            """
            INSERT OR IGNORE INTO engineering_notifications
                (ticket_id, linear_issue_id, event_type)
            VALUES (?, ?, 'completed')
            """,
            (ticket["id"], issue_id),
        )
        existing_notes = ticket["resolution_notes"] or ""
        completion_note = f"Engineering issue {issue['identifier']} was marked Done."
        if completion_note not in existing_notes:
            existing_notes = (existing_notes.rstrip() + "\n\n" + completion_note).strip()
        conn.execute(
            """
            UPDATE tickets
            SET status = 'resolved', resolution_notes = ?, resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (existing_notes, ticket["id"]),
        )
        conn.commit()
        sent += 1
    return sent


def main() -> None:
    conn = get_connection()
    try:
        notify_completed_issues(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
