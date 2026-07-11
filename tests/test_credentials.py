import os
import unittest
from unittest.mock import Mock, patch

from credentials import load_provider_secret, save_provider_secret


class CredentialStoreTests(unittest.TestCase):
    @patch("credentials.platform.system", return_value="Darwin")
    @patch("credentials.subprocess.run")
    def test_saves_secret_to_macos_keychain_without_shell(self, run, _system):
        run.return_value = Mock(returncode=0, stderr="")

        save_provider_secret("github", "github-secret")

        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["security", "add-generic-password"])
        self.assertIn("hermes-support-agency", args)
        self.assertIn("github-secret", args)
        self.assertIs(run.call_args.kwargs["shell"], False)

    @patch("credentials.platform.system", return_value="Darwin")
    @patch("credentials.subprocess.run")
    def test_loads_secret_from_macos_keychain(self, run, _system):
        run.return_value = Mock(returncode=0, stdout="linear-secret\n", stderr="")

        secret = load_provider_secret("linear")

        self.assertEqual(secret, "linear-secret")
        self.assertNotIn("linear-secret", repr(run.call_args))

    @patch.dict(os.environ, {"GITHUB_TOKEN": "environment-secret"})
    @patch("credentials.subprocess.run")
    def test_environment_variable_takes_precedence(self, run):
        self.assertEqual(load_provider_secret("github"), "environment-secret")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
