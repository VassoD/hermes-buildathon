import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from app import app


class WorkspaceUiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "workspace.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_operator_can_save_workspace_configuration(self):
        with patch("db.DB_PATH", self.db_path), patch.dict(
            "os.environ", {"GITHUB_TOKEN": "test", "LINEAR_API_KEY": "test"}
        ):
            client = app.test_client()
            response = client.post(
                "/workspace",
                data={
                    "name": "Writer SaaS",
                    "product_url": "https://writer.example.com/docs",
                    "github_repo": "https://github.com/acme/writer",
                    "github_branch": "main",
                    "linear_team_id": "team-1",
                    "linear_project_id": "",
                    "bug_confidence_threshold": "likely",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Writer SaaS", response.data)
        self.assertIn(b"GitHub configured", response.data)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT github_repo FROM product_workspaces").fetchone()
        self.assertEqual(row[0], "acme/writer")

    def test_navigation_exposes_workspace_setup(self):
        with patch("db.DB_PATH", self.db_path):
            response = app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'href="/workspace"', response.data)


    @patch("app.has_provider_secret", return_value=True)
    @patch("app.save_provider_secret")
    def test_operator_can_save_provider_credentials_from_local_dashboard(
        self, save_secret, _has_secret
    ):
        with patch("db.DB_PATH", self.db_path):
            client = app.test_client()
            client.get("/workspace")
            with client.session_transaction() as session:
                csrf_token = session["workspace_csrf_token"]
            response = client.post(
                "/workspace",
                data={
                    "name": "Writer SaaS",
                    "product_url": "https://writer.example.com/docs",
                    "github_repo": "acme/writer",
                    "github_branch": "main",
                    "linear_team_id": "team-1",
                    "linear_project_id": "",
                    "bug_confidence_threshold": "likely",
                    "github_token": "github-secret",
                    "linear_api_key": "linear-secret",
                    "csrf_token": csrf_token,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            save_secret.call_args_list,
            [call("github", "github-secret"), call("linear", "linear-secret")],
        )
        self.assertNotIn(b"github-secret", response.data)
        self.assertNotIn(b"linear-secret", response.data)
        self.assertIn(b'type="password"', response.data)

    @patch("app.save_provider_secret")
    def test_credential_update_requires_csrf_token(self, save_secret):
        with patch("db.DB_PATH", self.db_path):
            response = app.test_client().post(
                "/workspace",
                data={
                    "name": "Writer SaaS",
                    "product_url": "https://writer.example.com/docs",
                    "github_token": "github-secret",
                },
            )

        self.assertEqual(response.status_code, 200)
        save_secret.assert_not_called()
        self.assertIn(b"security token is invalid", response.data)

    @patch("app.save_provider_secret")
    def test_remote_dashboard_cannot_update_provider_credentials(self, save_secret):
        with patch("db.DB_PATH", self.db_path):
            response = app.test_client().post(
                "/workspace",
                base_url="https://support.example.com",
                data={
                    "name": "Writer SaaS",
                    "product_url": "https://writer.example.com/docs",
                    "github_token": "github-secret",
                },
            )

        self.assertEqual(response.status_code, 200)
        save_secret.assert_not_called()
        self.assertIn(b"Credentials can only be changed from localhost", response.data)


if __name__ == "__main__":
    unittest.main()
