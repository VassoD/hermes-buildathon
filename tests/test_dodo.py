import json
import unittest
from unittest.mock import patch

from dodo import DodoClient, DodoError, extract_payment_id


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class DodoClientTests(unittest.TestCase):
    def test_extracts_payment_id_from_support_message(self):
        self.assertEqual(
            extract_payment_id("Please refund Dodo payment pay_abc123XYZ."),
            "pay_abc123XYZ",
        )

    def test_returns_none_when_message_has_no_payment_id(self):
        self.assertIsNone(extract_payment_id("I was charged twice."))

    def test_create_refund_uses_test_endpoint_and_bearer_token(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "refund_id": "ref_123",
                    "payment_id": "pay_abc123",
                    "status": "succeeded",
                    "amount": 4900,
                    "currency": "USD",
                }
            )

        client = DodoClient("test_key", environment="test", opener=opener)
        refund = client.create_refund(
            "pay_abc123",
            reason="Duplicate charge",
            metadata={"ticket_id": "42"},
        )

        request = captured["request"]
        self.assertEqual(request.full_url, "https://test.dodopayments.com/refunds")
        self.assertEqual(request.get_header("Authorization"), "Bearer test_key")
        self.assertEqual(request.get_header("User-agent"), "HermesSupportCrew/1.0")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            json.loads(request.data),
            {
                "payment_id": "pay_abc123",
                "reason": "Duplicate charge",
                "metadata": {"ticket_id": "42"},
            },
        )
        self.assertEqual(refund.refund_id, "ref_123")
        self.assertEqual(refund.status, "succeeded")

    def test_rejects_live_environment_without_explicit_opt_in(self):
        with patch.dict("os.environ", {}, clear=False):
            with self.assertRaises(DodoError):
                DodoClient("live_key", environment="live")


if __name__ == "__main__":
    unittest.main()
