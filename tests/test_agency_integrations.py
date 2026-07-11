import sqlite3
import unittest
import json
import base64
from unittest.mock import patch

from agency_integrations import (
    GitHubClient,
    IntegrationError,
    LinearClient,
    ProductContextClient,
    Workspace,
    _PublicRedirectHandler,
    _linear_title,
    _is_source_path,
    _search_terms,
    create_linear_escalation,
    get_workspace,
    investigate_ticket,
    save_workspace,
)
from db import apply_schema


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        apply_schema(self.conn)

    def test_workspace_round_trip_keeps_public_configuration(self):
        saved = save_workspace(
            self.conn,
            Workspace(
                name="Writer SaaS",
                product_url="https://writer.example.com/docs",
                github_repo="acme/writer",
                github_branch="main",
                linear_team_id="team-123",
                linear_project_id="project-456",
                bug_confidence_threshold="likely",
            ),
        )

        loaded = get_workspace(self.conn)

        self.assertEqual(saved.id, loaded.id)
        self.assertEqual(loaded.github_repo, "acme/writer")
        self.assertEqual(loaded.linear_team_id, "team-123")
        self.assertEqual(loaded.bug_confidence_threshold, "likely")

    def test_workspace_rejects_a_local_product_url(self):
        with self.assertRaisesRegex(IntegrationError, "public http"):
            save_workspace(
                self.conn,
                Workspace(name="Unsafe", product_url="http://127.0.0.1:8080"),
            )


class ProductContextTests(unittest.TestCase):
    @patch("agency_integrations.socket.getaddrinfo")
    def test_redirect_handler_blocks_private_network_targets(self, getaddrinfo):
        getaddrinfo.return_value = [(None, None, None, None, ("127.0.0.1", 80))]
        handler = _PublicRedirectHandler()

        with self.assertRaisesRegex(IntegrationError, "public http"):
            handler.redirect_request(None, None, 302, "Found", {}, "http://localhost/admin")

    @patch("agency_integrations.socket.getaddrinfo")
    def test_fetch_converts_public_html_to_bounded_text(self, getaddrinfo):
        getaddrinfo.return_value = [(None, None, None, None, ("93.184.216.34", 443))]

        class Response:
            headers = {"Content-Type": "text/html; charset=utf-8"}

            def read(self, _limit):
                return b"<html><title>Writer</title><body><h1>Exports</h1><script>ignore()</script><p>PDF export is available on Pro.</p></body></html>"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        client = ProductContextClient(opener=lambda *_args, **_kwargs: Response())
        context = client.fetch("https://writer.example.com/docs")

        self.assertIn("Exports", context)
        self.assertIn("PDF export is available on Pro.", context)
        self.assertNotIn("ignore()", context)


class GitHubClientTests(unittest.TestCase):
    def test_search_terms_prioritize_feature_labels_and_numeric_values(self):
        ticket = {
            "subject": "I don't understand why I have 999999999 balance",
            "body": (
                "Product/feature: Inteply account credit balance\n"
                "Earlier screenshot: The CREDIT BALANCE card showed 999999 remaining."
            ),
        }

        terms = _search_terms(ticket)

        self.assertIn("credit balance", terms)
        self.assertIn("999999", terms)
        self.assertNotIn("don", terms)

    def test_only_product_source_files_are_accepted_as_code_evidence(self):
        self.assertTrue(_is_source_path("intelply-backend/src/services/creditsService.ts"))
        self.assertTrue(_is_source_path("intelply-frontend/src/hooks/useCredits.ts"))
        self.assertFalse(_is_source_path("AGENTS.md"))
        self.assertFalse(_is_source_path("intelply-frontend/CLAUDE.md"))
        self.assertFalse(_is_source_path("intelply-frontend/content/blog/post.mdx"))

    def test_linear_title_replaces_generic_image_description_with_error_phrase(self):
        ticket = {
            "subject": "The user sent an image. It is a portrait Android screenshot",
            "body": "A red alert says exactly: Failed to generate articles: Network failed.",
        }

        self.assertEqual(_linear_title(ticket), "Bug: Failed to generate articles")

    def test_search_checks_that_the_configured_repository_is_accessible(self):
        def transport(method, url, payload=None, headers=None):
            if url == "https://api.github.com/repos/acme/missing":
                raise IntegrationError("Provider returned HTTP 404")
            return {"items": []}

        client = GitHubClient(token="token", transport=transport)

        with self.assertRaisesRegex(IntegrationError, "404"):
            client.search("acme/missing", "main", ["export"])

    def test_search_terms_prioritize_reported_ui_errors_over_image_description(self):
        ticket = {
            "subject": "The user sent an image. It is a portrait Android screenshot",
            "body": (
                "The page shows Processing content as active. A red alert says exactly: "
                "Failed to generate articles: Network connection failed. "
                "The UI simultaneously shows Generating and Processing content."
            ),
        }

        terms = _search_terms(ticket)

        self.assertIn("Failed to generate articles", terms)
        self.assertIn("Processing content", terms)
        self.assertNotIn("user", terms)

    def test_search_returns_file_line_and_code_evidence(self):
        calls = []

        def transport(method, url, payload=None, headers=None):
            calls.append((method, url, payload))
            if "/contents/" in url:
                source = "# export helpers\n\ndef export_pdf(report):\n    return render(report)\n"
                return {"encoding": "base64", "content": base64.b64encode(source.encode()).decode()}
            return {
                "items": [
                    {
                        "name": "export.ts",
                        "path": "intelply-backend/src/services/export.ts",
                        "html_url": "https://github.com/acme/writer/blob/main/intelply-backend/src/services/export.ts",
                        "text_matches": [
                            {
                                "fragment": "def export_pdf(report):\n    return render(report)",
                                "matches": [{"indices": [4, 14]}],
                            }
                        ],
                    }
                ]
            }

        client = GitHubClient(token="token", transport=transport)
        evidence = client.search("acme/writer", "main", ["export_pdf"])

        self.assertEqual(evidence[0].path, "intelply-backend/src/services/export.ts")
        self.assertEqual(evidence[0].line, 3)
        self.assertIn("render(report)", evidence[0].snippet)
        self.assertEqual(calls[0][1], "https://api.github.com/repos/acme/writer")
        self.assertIn("repo%3Aacme%2Fwriter", calls[1][1])
        self.assertIn("ref=main", calls[2][1])


class LinearClientTests(unittest.TestCase):
    def test_get_issue_returns_completion_state(self):
        def transport(method, url, payload=None, headers=None):
            return {
                "data": {
                    "issue": {
                        "id": "issue-23",
                        "identifier": "TEC-23",
                        "title": "Generation status bug",
                        "url": "https://linear.app/issue/TEC-23",
                        "state": {"name": "Done", "type": "completed"},
                    }
                }
            }

        issue = LinearClient(api_key="lin_api", transport=transport).get_issue("issue-23")

        self.assertEqual(issue["state"]["type"], "completed")
        self.assertEqual(issue["identifier"], "TEC-23")

    def test_ensure_bug_label_creates_it_when_the_team_has_none(self):
        responses = [
            {"data": {"issueLabels": {"nodes": []}}},
            {
                "data": {
                    "issueLabelCreate": {
                        "success": True,
                        "issueLabel": {"id": "label-new", "name": "bug"},
                    }
                }
            },
        ]

        def transport(method, url, payload=None, headers=None):
            return responses.pop(0)

        client = LinearClient(api_key="lin_api", transport=transport)

        self.assertEqual(client.ensure_label_id("bug", "team-1"), "label-new")

    def test_create_issue_can_assign_owner_and_bug_label(self):
        responses = [
            {
                "data": {
                    "users": {
                        "nodes": [{"id": "user-1", "name": "Vassiliki Doropoulou"}]
                    }
                }
            },
            {"data": {"issueLabels": {"nodes": [{"id": "label-1", "name": "bug"}]}}},
            {
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "issue-1",
                            "identifier": "TEC-23",
                            "url": "https://linear.app/tech-eu/issue/TEC-23",
                            "title": "Generation status is stale",
                        },
                    }
                }
            },
        ]
        requests = []

        def transport(method, url, payload=None, headers=None):
            requests.append(payload)
            return responses.pop(0)

        client = LinearClient(api_key="lin_api", transport=transport)
        assignee_id = client.find_user_id("Vassiliki Doropoulou")
        label_id = client.find_label_id("bug", "team-1")
        client.create_issue(
            team_id="team-1",
            project_id=None,
            title="Generation status is stale",
            description="Evidence",
            assignee_id=assignee_id,
            label_ids=[label_id],
        )

        issue_input = requests[2]["variables"]["input"]
        self.assertEqual(issue_input["assigneeId"], "user-1")
        self.assertEqual(issue_input["labelIds"], ["label-1"])

    def test_find_duplicate_then_create_issue_with_source_evidence(self):
        responses = [
            {"data": {"searchIssues": {"nodes": []}}},
            {
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "issue-1",
                            "identifier": "SUP-42",
                            "url": "https://linear.app/acme/issue/SUP-42",
                        },
                    }
                }
            },
        ]
        requests = []

        def transport(method, url, payload=None, headers=None):
            requests.append(payload)
            return responses.pop(0)

        client = LinearClient(api_key="lin_api", transport=transport)
        duplicate = client.find_duplicate("PDF export spins forever", "team-1")
        issue = client.create_issue(
            team_id="team-1",
            project_id=None,
            title="PDF export spins forever",
            description="Evidence: src/export.py:1",
        )

        self.assertIsNone(duplicate)
        self.assertEqual(issue.identifier, "SUP-42")
        self.assertEqual(requests[1]["variables"]["input"]["teamId"], "team-1")
        self.assertIn("Evidence", requests[1]["variables"]["input"]["description"])


class InvestigationWorkflowTests(unittest.TestCase):
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
            VALUES ('customer@example.com', 'PDF export spins forever',
                    'Reports containing charts never finish exporting.', 'technical', 'in_review')
            """
        )
        self.ticket = self.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()

    def test_investigation_persists_product_and_code_evidence(self):
        product_client = type(
            "Product", (), {"fetch": lambda _self, _url: "PDF export works on Pro."}
        )()
        evidence = type(
            "Evidence",
            (),
            {
                "path": "src/export.py",
                "line": 42,
                "url": "https://github.com/acme/writer/blob/main/src/export.py#L42",
                "snippet": "while job.status != 'complete': poll(job)",
            },
        )()
        github_client = type(
            "GitHub", (), {"search": lambda _self, _repo, _branch, _terms: [evidence]}
        )()

        result = investigate_ticket(
            self.conn,
            self.ticket,
            "run-1",
            self.workspace,
            product_client=product_client,
            github_client=github_client,
            analyzer=lambda *_args: {
                "verdict": "likely",
                "reasoning": "The implementation has an unbounded polling path.",
            },
        )

        stored = self.conn.execute("SELECT * FROM ticket_investigations").fetchone()
        self.assertEqual(result.verdict, "likely")
        self.assertIn("PDF export works", stored["product_context"])
        stored_evidence = json.loads(stored["code_evidence"])
        self.assertEqual(stored_evidence[0]["path"], "src/export.py")
        self.assertEqual(stored_evidence[0]["line"], 42)

    def test_linear_escalation_is_idempotent_and_records_external_issue(self):
        evidence = type(
            "Evidence",
            (),
            {
                "path": "src/export.py",
                "line": 42,
                "url": "https://github.com/acme/writer/blob/main/src/export.py#L42",
                "snippet": "raise ExportTimeout()",
            },
        )()
        investigation = investigate_ticket(
            self.conn,
            self.ticket,
            "run-2",
            self.workspace,
            product_client=type("Product", (), {"fetch": lambda _self, _url: "Docs"})(),
            github_client=type("GitHub", (), {"search": lambda *_args: [evidence]})(),
            analyzer=lambda *_args: {"verdict": "confirmed", "reasoning": "Confirmed."},
        )

        class FakeLinear:
            def find_duplicate(self, title, team_id):
                return None

            def find_user_id(self, name):
                return "user-1"

            def ensure_label_id(self, name, team_id):
                return "label-1"

            def create_issue(
                self, team_id, project_id, title, description, assignee_id=None, label_ids=None
            ):
                return type(
                    "Issue",
                    (),
                    {
                        "id": "issue-9",
                        "identifier": "BUG-9",
                        "url": "https://linear.app/acme/issue/BUG-9",
                        "title": title,
                    },
                )()

        issue = create_linear_escalation(
            self.conn, self.ticket, investigation, self.workspace, linear_client=FakeLinear()
        )
        again = create_linear_escalation(
            self.conn, self.ticket, investigation, self.workspace, linear_client=FakeLinear()
        )

        self.assertEqual(issue.identifier, "BUG-9")
        self.assertEqual(again.identifier, "BUG-9")
        stored = self.conn.execute("SELECT * FROM ticket_investigations").fetchone()
        self.assertEqual(stored["linear_issue_id"], "issue-9")

    def test_bug_verdict_cannot_be_likely_without_source_evidence(self):
        investigation = investigate_ticket(
            self.conn,
            self.ticket,
            "run-3",
            self.workspace,
            product_client=type("Product", (), {"fetch": lambda _self, _url: "Docs"})(),
            github_client=type("GitHub", (), {"search": lambda *_args: []})(),
            analyzer=lambda *_args: {"verdict": "likely", "reasoning": "Looks suspicious."},
        )

        self.assertEqual(investigation.verdict, "inconclusive")
        self.assertIn("No source evidence", investigation.reasoning)


if __name__ == "__main__":
    unittest.main()
