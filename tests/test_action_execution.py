import sqlite3
import unittest

from actions import execute_action, persist_execution, status_for_execution
from db import apply_schema
from dodo import DodoRefund


class FakeDodoClient:
    def __init__(self, refund_status="succeeded"):
        self.refund_status = refund_status
        self.calls = []

    def create_refund(self, payment_id, reason, metadata):
        self.calls.append((payment_id, reason, metadata))
        return DodoRefund(
            refund_id="ref_test_123",
            payment_id=payment_id,
            status=self.refund_status,
            amount=4900,
            currency="USD",
        )


class ActionExecutionTests(unittest.TestCase):
    def ticket(self, body="Duplicate Dodo charge pay_abc123", payment_id=None):
        return {
            "id": 42,
            "customer_email": "customer@example.com",
            "body": body,
            "payment_id": payment_id,
        }

    def test_issue_refund_executes_against_dodo(self):
        client = FakeDodoClient()

        result = execute_action(
            self.ticket(),
            "issue_refund",
            "Refunding the duplicate charge.",
            dodo_client=client,
        )

        self.assertEqual(result.provider, "dodo")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.external_id, "ref_test_123")
        self.assertEqual(client.calls[0][0], "pay_abc123")
        self.assertEqual(client.calls[0][2]["ticket_id"], "42")

    def test_explicit_ticket_payment_id_takes_precedence(self):
        client = FakeDodoClient()
        ticket = self.ticket(body="No id in this message", payment_id="pay_saved456")

        execute_action(ticket, "issue_refund", "Approved", dodo_client=client)

        self.assertEqual(client.calls[0][0], "pay_saved456")

    def test_missing_payment_id_blocks_refund_instead_of_claiming_success(self):
        result = execute_action(
            self.ticket(body="I was charged twice", payment_id=None),
            "issue_refund",
            "Approved",
            dodo_client=FakeDodoClient(),
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("payment ID", result.detail)

    def test_non_external_action_is_recorded_without_provider_call(self):
        result = execute_action(
            self.ticket(),
            "reply_only",
            "Troubleshooting steps",
            dodo_client=FakeDodoClient(),
        )

        self.assertEqual(result.status, "not_required")
        self.assertEqual(result.provider, "internal")

    def test_execution_is_persisted_for_traceability(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE action_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ticket_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                external_id TEXT,
                detail TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        result = execute_action(
            self.ticket(), "issue_refund", "Approved", dodo_client=FakeDodoClient()
        )

        persist_execution(conn, "run-1", 42, "issue_refund", result)

        row = conn.execute("SELECT * FROM action_executions").fetchone()
        self.assertEqual(row["provider"], "dodo")
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["external_id"], "ref_test_123")

    def test_schema_contains_payment_and_execution_evidence(self):
        conn = sqlite3.connect(":memory:")
        apply_schema(conn)

        ticket_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("payment_id", ticket_columns)
        self.assertIn("action_executions", tables)

    def test_successful_refund_resolves_ticket(self):
        result = execute_action(
            self.ticket(), "issue_refund", "Approved", dodo_client=FakeDodoClient()
        )
        self.assertEqual(status_for_execution("issue_refund", result), "resolved")

    def test_unproven_refund_escalates_instead_of_claiming_resolution(self):
        blocked = execute_action(
            self.ticket(body="No payment reference"),
            "issue_refund",
            "Approved",
            dodo_client=FakeDodoClient(),
        )
        self.assertEqual(status_for_execution("issue_refund", blocked), "escalated")

    def test_request_for_more_information_stays_in_review(self):
        result = execute_action(
            self.ticket(), "request_more_info", "Need the invoice ID"
        )
        self.assertEqual(status_for_execution("request_more_info", result), "in_review")


if __name__ == "__main__":
    unittest.main()
