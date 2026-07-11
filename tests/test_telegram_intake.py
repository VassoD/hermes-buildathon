import sqlite3
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from db import apply_schema
from manager import Classification, RoutingDecision
from specialists import SpecialistDraft
from telegram_intake import handle_message, insert_ticket


class KeepOpenConnection:
    """Lets a test inspect the database after handle_message closes it."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def close(self) -> None:
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TelegramIntakeTests(unittest.TestCase):
    @patch("telegram_intake.load_trace_base_url", return_value="https://dead.example")
    def test_unreachable_trace_page_is_not_shared_with_customer(self, _load_url):
        from telegram_intake import get_reachable_trace_url

        def unavailable(*_args, **_kwargs):
            raise urllib.error.URLError("host not found")

        self.assertIsNone(get_reachable_trace_url(7, opener=unavailable))

    def test_intake_stores_dodo_payment_reference(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_schema(conn)

        ticket = insert_ticket(
            conn,
            "telegram_123@demo.local",
            "Please refund duplicate payment pay_demo789",
        )

        self.assertEqual(ticket["payment_id"], "pay_demo789")

    def test_intake_persists_the_exact_incoming_telegram_message(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_schema(conn)
        message = "The CSV export still times out after three browsers."

        ticket = insert_ticket(conn, "telegram_123@demo.local", message)

        stored = conn.execute(
            "SELECT direction, body, ticket_id FROM support_messages"
        ).fetchone()
        self.assertEqual(stored["direction"], "inbound")
        self.assertEqual(stored["body"], message)
        self.assertEqual(stored["ticket_id"], ticket["id"])

    def test_narrator_persists_each_outbound_telegram_message(self):
        from telegram_intake import Narrator

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_schema(conn)
        ticket = insert_ticket(conn, "telegram_123@demo.local", "Help me")
        narrator = Narrator(
            "123",
            dry_run=True,
            conn=conn,
            ticket_id=ticket["id"],
            customer_email=ticket["customer_email"],
        )

        narrator.say("🔍 Checking your history...")

        stored = conn.execute(
            "SELECT direction, kind, body FROM support_messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(stored["direction"], "outbound")
        self.assertEqual(stored["kind"], "progress")
        self.assertEqual(stored["body"], "🔍 Checking your history...")

    def test_handle_message_only_sends_customer_safe_intake_and_resolution(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_schema(conn)

        classification = Classification("technical", "Needs signup help.", 0, 0.0)
        routing = RoutingDecision(["TechnicalAgent"], "Technical route.")
        draft = SpecialistDraft(
            agent_name="TechnicalAgent",
            proposed_action="request_more_info",
            customer_response="Inteply uses Request a Demo instead of public signup.",
            reasoning="The public site has no signup form.",
            tokens_used=0,
            cost_estimate=0.0,
        )

        def fake_process_ticket(db, ticket, **_kwargs):
            db.execute(
                "UPDATE tickets SET category = 'technical', status = 'in_review' WHERE id = ?",
                (ticket["id"],),
            )
            db.commit()
            return classification, routing

        def fake_review(db, ticket, *_args, **_kwargs):
            db.execute(
                """
                UPDATE tickets
                SET status = 'in_review', resolution_action = 'request_more_info',
                    resolution_notes = ?
                WHERE id = ?
                """,
                (draft.customer_response, ticket["id"]),
            )
            db.commit()
            return SimpleNamespace(approved_first_pass=True)

        with (
            patch("telegram_intake.get_connection", return_value=conn),
            patch("telegram_intake.process_ticket", side_effect=fake_process_ticket),
            patch("telegram_intake.get_workspace", return_value=None),
            patch("telegram_intake.run_specialist", return_value=draft),
            patch("telegram_intake.review_and_finalize", side_effect=fake_review),
            patch("telegram_intake.load_trace_base_url", return_value=None),
        ):
            result = handle_message("123", "I can't find signup", "123", dry_run=True)

        messages = result["narrated_messages"]
        self.assertEqual(len(messages), 2)
        self.assertIn("ticket #1", messages[0].lower())
        self.assertEqual(messages[1], draft.customer_response)
        forbidden = (
            "checking your history",
            "classified as",
            "routed to",
            "drafting a resolution",
            "manager reviewing",
            "action submitted",
        )
        transcript = "\n".join(messages).lower()
        for phrase in forbidden:
            self.assertNotIn(phrase, transcript)

    def test_followup_reply_reprocesses_waiting_ticket_instead_of_opening_new_one(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_schema(conn)
        original = insert_ticket(
            conn, "telegram_123@demo.local", "Article generation is stuck"
        )
        conn.execute(
            """
            UPDATE tickets
            SET status = 'in_review', category = 'technical',
                resolution_action = 'request_more_info',
                resolution_notes = 'Please share a screenshot of the error.'
            WHERE id = ?
            """,
            (original["id"],),
        )
        conn.commit()

        draft = SpecialistDraft(
            agent_name="TechnicalAgent",
            proposed_action="reply_only",
            customer_response="Thanks, the screenshot confirms the fix. Please retry.",
            reasoning="The follow-up screenshot identified the failing step.",
            tokens_used=0,
            cost_estimate=0.0,
        )

        def fake_review(db, ticket, *_args, **_kwargs):
            db.execute(
                """
                UPDATE tickets
                SET status = 'resolved', resolution_action = 'reply_only',
                    resolution_notes = ?
                WHERE id = ?
                """,
                (draft.customer_response, ticket["id"]),
            )
            db.commit()
            return SimpleNamespace(approved_first_pass=True)

        with (
            patch("telegram_intake.get_connection", return_value=KeepOpenConnection(conn)),
            patch("telegram_intake.process_ticket") as classify,
            patch("telegram_intake.get_workspace", return_value=None),
            patch("telegram_intake.run_specialist", return_value=draft) as specialist,
            patch("telegram_intake.review_and_finalize", side_effect=fake_review),
            patch("telegram_intake.load_trace_base_url", return_value=None),
        ):
            result = handle_message(
                "123", "Here is the screenshot", "123", dry_run=True
            )

        self.assertEqual(result["ticket_id"], original["id"])
        self.assertEqual(result["status"], "resolved")
        self.assertIn("followup_intake", result["steps_ran"])
        classify.assert_not_called()

        ticket_count = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        self.assertEqual(ticket_count, 1)
        updated = conn.execute(
            "SELECT body FROM tickets WHERE id = ?", (original["id"],)
        ).fetchone()
        self.assertIn("Customer follow-up: Here is the screenshot", updated["body"])
        inbound = conn.execute(
            """
            SELECT COUNT(*) FROM support_messages
            WHERE ticket_id = ? AND direction = 'inbound'
            """,
            (original["id"],),
        ).fetchone()[0]
        self.assertEqual(inbound, 2)
        specialist_context = specialist.call_args.kwargs["manager_context"]
        self.assertIn("Please share a screenshot of the error.", specialist_context)
        self.assertIn("Here is the screenshot", specialist_context)

    def test_new_customer_message_still_opens_a_fresh_ticket(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_schema(conn)
        resolved = insert_ticket(conn, "telegram_123@demo.local", "Old issue")
        conn.execute(
            """
            UPDATE tickets
            SET status = 'resolved', category = 'technical',
                resolution_action = 'reply_only'
            WHERE id = ?
            """,
            (resolved["id"],),
        )
        conn.commit()

        classification = Classification("technical", "Needs help.", 0, 0.0)
        routing = RoutingDecision(["TechnicalAgent"], "Technical route.")
        draft = SpecialistDraft(
            agent_name="TechnicalAgent",
            proposed_action="reply_only",
            customer_response="Here is how to fix it.",
            reasoning="Straightforward answer.",
            tokens_used=0,
            cost_estimate=0.0,
        )

        def fake_process_ticket(db, ticket, **_kwargs):
            db.execute(
                "UPDATE tickets SET category = 'technical', status = 'in_review' WHERE id = ?",
                (ticket["id"],),
            )
            db.commit()
            return classification, routing

        def fake_review(db, ticket, *_args, **_kwargs):
            db.execute(
                """
                UPDATE tickets
                SET status = 'resolved', resolution_action = 'reply_only',
                    resolution_notes = ?
                WHERE id = ?
                """,
                (draft.customer_response, ticket["id"]),
            )
            db.commit()
            return SimpleNamespace(approved_first_pass=True)

        with (
            patch("telegram_intake.get_connection", return_value=KeepOpenConnection(conn)),
            patch("telegram_intake.process_ticket", side_effect=fake_process_ticket),
            patch("telegram_intake.get_workspace", return_value=None),
            patch("telegram_intake.run_specialist", return_value=draft),
            patch("telegram_intake.review_and_finalize", side_effect=fake_review),
            patch("telegram_intake.load_trace_base_url", return_value=None),
        ):
            result = handle_message("123", "A brand new problem", "123", dry_run=True)

        self.assertNotEqual(result["ticket_id"], resolved["id"])
        self.assertIn("intake", result["steps_ran"])
        self.assertNotIn("followup_intake", result["steps_ran"])


if __name__ == "__main__":
    unittest.main()
