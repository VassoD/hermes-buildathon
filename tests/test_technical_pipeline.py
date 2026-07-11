import sqlite3
import unittest
from unittest.mock import patch

from agency_integrations import CodeEvidence, Investigation, LinearIssue, Workspace, save_workspace
from db import apply_schema
from telegram_intake import publish_bug_to_linear, run_bug_investigation


class TechnicalPipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        apply_schema(self.conn)
        self.workspace = save_workspace(
            self.conn,
            Workspace(
                name="Writer",
                product_url="https://writer.example.com/docs",
                github_repo="acme/writer",
                linear_team_id="team-1",
            ),
        )
        cursor = self.conn.execute(
            """
            INSERT INTO tickets (customer_email, subject, body, category, status)
            VALUES ('c@example.com', 'Export hangs', 'PDF export spins.', 'technical', 'in_review')
            """
        )
        self.ticket = self.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        self.investigation = Investigation(
            id=1,
            ticket_id=self.ticket["id"],
            product_context="PDF exports are supported.",
            evidence=(CodeEvidence("src/export.py", 12, "https://github.test/file#L12", "poll()"),),
            verdict="likely",
            reasoning="Polling has no terminal failure path.",
        )

    @patch("telegram_intake.investigate_ticket")
    def test_technical_ticket_adds_investigation_to_specialist_context(self, investigate):
        investigate.return_value = self.investigation

        result = run_bug_investigation(
            self.conn, self.ticket, "run-1", self.workspace
        )

        self.assertIn("src/export.py:12", result.context_summary())
        step = self.conn.execute(
            "SELECT * FROM runs WHERE step_type = 'bug_investigation'"
        ).fetchone()
        self.assertEqual(step["agent_name"], "BugInvestigator")
        self.assertIn("verdict=likely", step["output_summary"])

    @patch("telegram_intake.create_linear_escalation")
    def test_approved_escalation_creates_linear_issue_and_updates_customer_note(self, create):
        create.return_value = LinearIssue(
            "issue-1", "BUG-1", "https://linear.app/acme/issue/BUG-1", "Export hangs"
        )
        self.conn.execute(
            "UPDATE tickets SET status='escalated', resolution_notes='Sent to engineering.' WHERE id=?",
            (self.ticket["id"],),
        )
        self.conn.commit()

        issue = publish_bug_to_linear(
            self.conn, self.ticket, "run-2", self.investigation, self.workspace
        )

        self.assertEqual(issue.identifier, "BUG-1")
        updated = self.conn.execute(
            "SELECT resolution_notes FROM tickets WHERE id=?", (self.ticket["id"],)
        ).fetchone()
        self.assertIn("BUG-1", updated["resolution_notes"])
        step = self.conn.execute(
            "SELECT * FROM runs WHERE step_type='linear_escalation'"
        ).fetchone()
        self.assertIn("issue-1", step["output_summary"])

    def test_confirmed_bug_is_labeled_and_assigned_to_vassiliki(self):
        calls = {}

        class FakeLinear:
            def find_duplicate(self, title, team_id):
                return None

            def find_user_id(self, name):
                calls["assignee_name"] = name
                return "user-vassiliki"

            def ensure_label_id(self, name, team_id):
                calls["label_name"] = name
                return "label-bug"

            def create_issue(self, **kwargs):
                calls["create"] = kwargs
                return LinearIssue(
                    "issue-23",
                    "TEC-23",
                    "https://linear.app/tech-eu/issue/TEC-23",
                    kwargs["title"],
                )

        confirmed = Investigation(
            id=1,
            ticket_id=self.ticket["id"],
            product_context="Docs",
            evidence=self.investigation.evidence,
            verdict="confirmed",
            reasoning="The stale processing branch is directly visible in source.",
        )

        from agency_integrations import create_linear_escalation

        create_linear_escalation(
            self.conn,
            self.ticket,
            confirmed,
            self.workspace,
            linear_client=FakeLinear(),
        )

        self.assertEqual(calls["assignee_name"], "Vassiliki Doropoulou")
        self.assertEqual(calls["label_name"], "bug")
        self.assertEqual(calls["create"]["assignee_id"], "user-vassiliki")
        self.assertEqual(calls["create"]["label_ids"], ["label-bug"])

    @patch("agency_integrations.LinearClient")
    def test_manager_approved_escalation_overrides_automated_confidence_gate(self, client):
        self.conn.execute(
            """
            INSERT INTO ticket_investigations
                (ticket_id, run_id, workspace_id, product_context, code_evidence,
                 verdict, reasoning)
            VALUES (?, 'run-3', ?, 'Docs', '[]', 'inconclusive', 'No source match.')
            """,
            (self.ticket["id"], self.workspace.id),
        )
        self.conn.execute(
            "UPDATE tickets SET status='escalated', resolution_notes='Sent to engineering.' WHERE id=?",
            (self.ticket["id"],),
        )
        self.conn.commit()
        investigation = Investigation(
            id=1,
            ticket_id=self.ticket["id"],
            product_context="Docs",
            evidence=(),
            verdict="inconclusive",
            reasoning="No source match.",
        )
        client.return_value.find_duplicate.return_value = None
        client.return_value.create_issue.return_value = LinearIssue(
            "issue-2", "BUG-2", "https://linear.app/acme/issue/BUG-2", "Export hangs"
        )

        issue = publish_bug_to_linear(
            self.conn, self.ticket, "run-3", investigation, self.workspace
        )

        self.assertEqual(issue.identifier, "BUG-2")
        updated = self.conn.execute(
            "SELECT linear_issue_id FROM ticket_investigations WHERE id=1"
        ).fetchone()
        self.assertEqual(updated["linear_issue_id"], "issue-2")


if __name__ == "__main__":
    unittest.main()
