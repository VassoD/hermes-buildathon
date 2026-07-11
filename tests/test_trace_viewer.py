import unittest

from trace_viewer import STEP_LABELS, format_output_summary


class TraceViewerExecutionTests(unittest.TestCase):
    def test_action_execution_has_human_readable_trace_label(self):
        self.assertEqual(STEP_LABELS["action_execute"], "EXECUTE ACTION")

    def test_action_execution_keeps_provider_proof_visible(self):
        proof = "provider=dodo; status=succeeded; external_id=ref_verified"
        self.assertEqual(format_output_summary("action_execute", proof), [proof])


if __name__ == "__main__":
    unittest.main()
