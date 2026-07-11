import unittest

from specialists import OUTPUT_FORMAT_INSTRUCTIONS


class SpecialistExecutionPolicyTests(unittest.TestCase):
    def test_refund_requires_provider_payment_reference(self):
        self.assertIn("pay_", OUTPUT_FORMAT_INSTRUCTIONS)
        self.assertIn("request_more_info", OUTPUT_FORMAT_INSTRUCTIONS)
        self.assertIn("issue_refund", OUTPUT_FORMAT_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
