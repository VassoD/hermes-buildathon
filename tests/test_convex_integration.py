import json
from pathlib import Path
import sqlite3
import unittest


from db import apply_schema


class FakeConvexStore:
    def __init__(self):
        self.calls = []

    def query(self, name, args=None):
        self.calls.append((name, args or {}))
        return {
            "clusters": [
                {
                    "stableKey": "billing:issue_refund",
                    "name": "Billing · Approved Refunds",
                    "description": "Duplicate charges",
                    "volume": 2,
                    "severity": "high",
                    "rank": 1,
                }
            ],
            "macros": [
                {
                    "stableKey": "billing:issue_refund",
                    "clusterKey": "billing:issue_refund",
                    "title": "Macro: duplicate charge",
                    "response": "We refunded the duplicate charge.",
                    "qaScore": 100,
                    "version": 1,
                    "status": "published",
                }
            ],
            "events": [],
        }

    def mutation(self, name, args):
        self.calls.append((name, args))
        if name == "supportOps:syncSnapshot":
            return {"clusters": len(args["clusters"]), "macros": len(args["macros"])}
        return {"ok": True}


class ConvexSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        apply_schema(self.conn)
        self.conn.executemany(
            """
            INSERT INTO tickets
                (customer_email, subject, body, category, status,
                 resolution_action, resolution_notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "a@example.com",
                    "Duplicate charge",
                    "I was charged twice",
                    "billing",
                    "resolved",
                    "issue_refund",
                    "We refunded the duplicate charge.",
                ),
                (
                    "b@example.com",
                    "Charged twice",
                    "There are two charges",
                    "billing",
                    "resolved",
                    "issue_refund",
                    "The duplicate was refunded.",
                ),
                (
                    "c@example.com",
                    "App crashes",
                    "The desktop app crashes",
                    "technical",
                    "escalated",
                    "escalate",
                    "Engineering is investigating.",
                ),
            ],
        )
        for ticket_id in (1, 2, 3):
            response = {
                "proposed_action": "issue_refund" if ticket_id < 3 else "escalate",
                "customer_response": (
                    "We refunded the duplicate charge."
                    if ticket_id < 3
                    else "Engineering is investigating the crash."
                ),
            }
            self.conn.execute(
                """
                INSERT INTO runs
                    (run_id, ticket_id, agent_name, step_type,
                     input_summary, output_summary, reasoning, tokens_used,
                     cost_estimate)
                VALUES (?, ?, ?, 'specialist_draft', '', ?, '', 0, 0)
                """,
                (f"run-{ticket_id}", ticket_id, "SupportAgent", json.dumps(response)),
            )
            self.conn.execute(
                """
                INSERT INTO runs
                    (run_id, ticket_id, agent_name, step_type,
                     input_summary, output_summary, reasoning, tokens_used,
                     cost_estimate)
                VALUES (?, ?, 'Manager', 'manager_review', '', 'verdict=APPROVE', '', 0, 0)
                """,
                (f"run-{ticket_id}", ticket_id),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_snapshot_clusters_real_tickets_and_publishes_a_macro(self):
        from convex_sync import build_support_snapshot

        snapshot = build_support_snapshot(self.conn)

        self.assertEqual(snapshot["clusters"][0]["stableKey"], "billing:issue_refund")
        self.assertEqual(snapshot["clusters"][0]["volume"], 2)
        self.assertEqual(snapshot["clusters"][0]["severity"], "high")
        self.assertEqual(snapshot["macros"][0]["clusterKey"], "billing:issue_refund")
        self.assertEqual(snapshot["macros"][0]["qaScore"], 100)
        self.assertIn("refunded", snapshot["macros"][0]["response"].lower())
        self.assertTrue(snapshot["events"])

    def test_snapshot_includes_reviewed_tickets_waiting_on_customer(self):
        from convex_sync import build_support_snapshot

        self.conn.execute(
            """
            INSERT INTO tickets
                (customer_email, subject, body, category, status,
                 resolution_action, resolution_notes)
            VALUES ('waiting@example.com', 'Need account details', 'Help me log in',
                    'technical', 'in_review', 'request_more_info',
                    'Please share the exact login error.')
            """
        )
        self.conn.commit()

        snapshot = build_support_snapshot(self.conn)

        self.assertIn(
            "technical:request_more_info",
            [cluster["stableKey"] for cluster in snapshot["clusters"]],
        )

    def test_sync_sends_clusters_macros_and_events_to_convex(self):
        from convex_sync import sync_support_ops

        store = FakeConvexStore()
        result = sync_support_ops(self.conn, store=store)

        self.assertEqual(store.calls[0][0], "supportOps:syncSnapshot")
        payload = store.calls[0][1]
        self.assertEqual(len(payload["clusters"]), 2)
        self.assertEqual(len(payload["macros"]), 2)
        self.assertGreaterEqual(len(payload["events"]), 2)
        self.assertEqual(result["clusters"], 2)


    def test_dashboard_loader_reads_the_convex_query(self):
        from convex_sync import load_support_dashboard

        store = FakeConvexStore()
        dashboard = load_support_dashboard(self.conn, store=store)

        self.assertEqual(store.calls[0][0], "supportOps:dashboard")
        self.assertEqual(dashboard["mode"], "convex_live")
        self.assertEqual(dashboard["clusters"][0]["volume"], 2)


class ConvexBackendDefinitionTests(unittest.TestCase):
    def test_schema_defines_partner_powerup_tables(self):
        schema = (Path(__file__).parents[1] / "convex" / "schema.ts").read_text()

        for table in ("clusters", "macros", "ratings", "runEvents"):
            self.assertIn(f"{table}: defineTable", schema)


    def test_python_requirements_include_convex_client(self):
        requirements = (Path(__file__).parents[1] / "requirements.txt").read_text()

        self.assertIn("convex==", requirements)
        self.assertIn("python-dotenv==", requirements)

    def test_pipeline_syncs_the_finished_playbook(self):
        source = (Path(__file__).parents[1] / "run_pipeline.py").read_text()

        self.assertIn("sync_support_ops(conn)", source)

    def test_backend_exposes_sync_dashboard_and_rating_functions(self):
        source = (Path(__file__).parents[1] / "convex" / "supportOps.ts").read_text()

        self.assertIn("export const syncSnapshot", source)
        self.assertIn("export const dashboard", source)
        self.assertIn("export const rateMacro", source)



if __name__ == "__main__":
    unittest.main()
