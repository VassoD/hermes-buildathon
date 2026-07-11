import sqlite3
import unittest
from unittest.mock import patch

from app import get_customer_messages, group_tickets_by_customer, propose_csm_actions
from db import apply_schema


class CustomerInboxTests(unittest.TestCase):
    def test_proposes_a_recurring_bug_action_from_conversation_evidence(self):
        tickets = [
            {
                "id": 1,
                "customer_email": "one@example.test",
                "subject": "Export generation failed",
                "body": "The export generation shows an error but completes.",
                "category": "technical",
                "status": "escalated",
                "resolution_notes": "Likely generation status synchronization bug.",
                "created_at": "2026-07-11 10:00:00",
            },
            {
                "id": 2,
                "customer_email": "two@example.test",
                "subject": "Generation error",
                "body": "Generation is stuck and the status is wrong.",
                "category": "technical",
                "status": "escalated",
                "resolution_notes": "Escalated to engineering.",
                "created_at": "2026-07-11 11:00:00",
            },
        ]

        actions = propose_csm_actions(tickets)

        recurring_bug = next(a for a in actions if a["signal"] == "Recurring bug")
        self.assertEqual(recurring_bug["priority"], "high")
        self.assertIn("generation", recurring_bug["title"].lower())
        self.assertEqual(recurring_bug["ticket_count"], 2)
        self.assertEqual(recurring_bug["customer_count"], 2)

    def test_dashboard_puts_recommended_actions_before_conversations(self):
        from app import app

        tickets = [
            {
                "id": 1,
                "customer_email": "customer@example.test",
                "subject": "I cannot find sign up",
                "body": "The sign up path is confusing.",
                "category": "technical",
                "status": "in_review",
                "resolution_notes": "Clarify onboarding.",
                "total_cost": 0.01,
                "had_memory_hit": False,
                "created_at": "2026-07-11 10:00:00",
            }
        ]

        with patch("app.get_all_tickets", return_value=tickets):
            response = app.test_client().get("/")

        page = response.get_data(as_text=True)
        self.assertLess(page.index("Recommended next steps"), page.index("Conversation evidence"))
        self.assertIn("UX friction", page)

    def test_groups_ticket_rows_into_one_conversation_per_customer(self):
        tickets = [
            {
                "id": 1,
                "customer_email": "repeat@example.test",
                "subject": "First question",
                "status": "resolved",
                "total_cost": 0.01,
                "created_at": "2026-07-11 10:00:00",
            },
            {
                "id": 2,
                "customer_email": "other@example.test",
                "subject": "Only question",
                "status": "escalated",
                "total_cost": 0.02,
                "created_at": "2026-07-11 10:01:00",
            },
            {
                "id": 3,
                "customer_email": "repeat@example.test",
                "subject": "Latest question",
                "status": "escalated",
                "total_cost": 0.03,
                "created_at": "2026-07-11 10:02:00",
            },
        ]

        conversations = group_tickets_by_customer(tickets)

        self.assertEqual([c["customer_email"] for c in conversations], [
            "repeat@example.test",
            "other@example.test",
        ])
        repeat = conversations[0]
        self.assertEqual(repeat["ticket_count"], 2)
        self.assertEqual(repeat["latest_subject"], "Latest question")
        self.assertEqual(repeat["latest_status"], "escalated")
        self.assertAlmostEqual(repeat["total_cost"], 0.04)

    def test_demo_does_not_expose_the_support_playbook(self):
        from app import app

        with patch("app.get_all_tickets", return_value=[]):
            response = app.test_client().get("/")
            rating_response = app.test_client().post(
                "/api/macros/billing:issue_refund/rate", json={"rating": 4}
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Support playbook", response.data)
        self.assertNotIn(b"CONVEX_URL", response.data)
        self.assertEqual(rating_response.status_code, 404)

    def test_legacy_tickets_are_rendered_as_inbound_and_reply_messages(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_schema(conn)
        conn.execute(
            """
            INSERT INTO tickets
                (customer_email, subject, body, status, resolution_notes)
            VALUES (?, ?, ?, 'resolved', ?)
            """,
            (
                "telegram_123@demo.local",
                "Export is broken",
                "The CSV export times out.",
                "I escalated this to engineering.",
            ),
        )
        conn.commit()

        with patch("app.get_connection", return_value=conn):
            messages = get_customer_messages("telegram_123@demo.local")

        self.assertEqual(
            [(m["direction"], m["body"]) for m in messages],
            [
                ("inbound", "The CSV export times out."),
                ("outbound", "I escalated this to engineering."),
            ],
        )


if __name__ == "__main__":
    unittest.main()
