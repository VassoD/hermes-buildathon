import sqlite3
import unittest

from db import apply_schema
from dodo import DodoRefund
from run_review import finalize


class FakeDodoClient:
    environment = "test"

    def create_refund(self, payment_id, reason, metadata):
        return DodoRefund(
            refund_id="ref_verified",
            payment_id=payment_id,
            status="succeeded",
            amount=4900,
            currency="USD",
        )


class ReviewExecutionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        apply_schema(self.conn)

    def add_ticket(self, body, payment_id=None):
        cursor = self.conn.execute(
            """
            INSERT INTO tickets (customer_email, subject, body, payment_id, status)
            VALUES ('buyer@example.com', 'Duplicate charge', ?, ?, 'in_review')
            """,
            (body, payment_id),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()

    def test_finalize_executes_refund_and_stores_provider_proof(self):
        ticket = self.add_ticket("Duplicate charge pay_abc123")

        status = finalize(
            self.conn,
            ticket,
            "run-1",
            "issue_refund",
            "We approved and issued your refund.",
            dodo_client=FakeDodoClient(),
        )

        updated = self.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket["id"],)
        ).fetchone()
        proof = self.conn.execute(
            "SELECT * FROM action_executions WHERE ticket_id = ?", (ticket["id"],)
        ).fetchone()
        run = self.conn.execute(
            "SELECT * FROM runs WHERE ticket_id = ? AND step_type = 'action_execute'",
            (ticket["id"],),
        ).fetchone()
        self.assertEqual(status, "resolved")
        self.assertEqual(updated["status"], "resolved")
        self.assertEqual(proof["external_id"], "ref_verified")
        self.assertEqual(proof["status"], "succeeded")
        self.assertIn("ref_verified", run["output_summary"])

    def test_finalize_escalates_when_refund_cannot_be_executed(self):
        ticket = self.add_ticket("Duplicate charge, no payment reference")

        status = finalize(
            self.conn,
            ticket,
            "run-2",
            "issue_refund",
            "Your refund has been issued.",
            dodo_client=FakeDodoClient(),
        )

        updated = self.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket["id"],)
        ).fetchone()
        self.assertEqual(status, "escalated")
        self.assertNotIn("has been issued", updated["resolution_notes"])
        self.assertIn("could not complete", updated["resolution_notes"])


if __name__ == "__main__":
    unittest.main()
