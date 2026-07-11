import sqlite3
import unittest

from db import apply_schema
from linear_status_notifier import notify_completed_issues


class LinearStatusNotifierTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        apply_schema(self.conn)
        cursor = self.conn.execute(
            """
            INSERT INTO tickets (customer_email, subject, body, category, status)
            VALUES ('telegram_12345@demo.local', 'Generation failed', 'Help',
                    'technical', 'escalated')
            """
        )
        self.ticket_id = cursor.lastrowid
        self.conn.execute(
            """
            INSERT INTO ticket_investigations
                (ticket_id, run_id, verdict, reasoning, linear_issue_id, linear_issue_url)
            VALUES (?, 'run-1', 'confirmed', 'Confirmed bug', 'issue-23',
                    'https://linear.app/tech-eu/issue/TEC-23')
            """,
            (self.ticket_id,),
        )
        self.conn.commit()

    def test_done_issue_notifies_originating_telegram_user_and_resolves_ticket(self):
        sent = []

        class FakeLinear:
            def get_issue(self, issue_id):
                return {
                    "id": issue_id,
                    "identifier": "TEC-23",
                    "title": "Article generated but dashboard shows network failure",
                    "url": "https://linear.app/tech-eu/issue/TEC-23",
                    "state": {"name": "Done", "type": "completed"},
                }

        def send(chat_id, text, conn, ticket):
            sent.append((chat_id, text))

        count = notify_completed_issues(self.conn, FakeLinear(), sender=send)

        self.assertEqual(count, 1)
        self.assertEqual(sent[0][0], "12345")
        self.assertIn("TEC-23", sent[0][1])
        self.assertIn("marked Done", sent[0][1])
        ticket = self.conn.execute(
            "SELECT status FROM tickets WHERE id = ?", (self.ticket_id,)
        ).fetchone()
        self.assertEqual(ticket["status"], "resolved")

    def test_done_issue_is_only_notified_once(self):
        sent = []

        class FakeLinear:
            def get_issue(self, issue_id):
                return {
                    "id": issue_id,
                    "identifier": "TEC-23",
                    "title": "Generation status bug",
                    "url": "https://linear.app/tech-eu/issue/TEC-23",
                    "state": {"name": "Done", "type": "completed"},
                }

        sender = lambda chat_id, text, conn, ticket: sent.append((chat_id, text))

        notify_completed_issues(self.conn, FakeLinear(), sender=sender)
        notify_completed_issues(self.conn, FakeLinear(), sender=sender)

        self.assertEqual(len(sent), 1)

    def test_non_completed_issue_does_not_notify(self):
        sent = []

        class FakeLinear:
            def get_issue(self, issue_id):
                return {
                    "id": issue_id,
                    "identifier": "TEC-23",
                    "title": "Generation status bug",
                    "url": "https://linear.app/tech-eu/issue/TEC-23",
                    "state": {"name": "In Progress", "type": "started"},
                }

        count = notify_completed_issues(
            self.conn,
            FakeLinear(),
            sender=lambda chat_id, text, conn, ticket: sent.append(text),
        )

        self.assertEqual(count, 0)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()