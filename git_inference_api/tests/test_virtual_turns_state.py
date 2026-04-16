from __future__ import annotations

import unittest

from git_inference_api.app.worker import JobWorker


class VirtualTurnsStateTests(unittest.TestCase):
    def test_build_execution_state_in_progress_sets_next_index(self) -> None:
        worker = JobWorker()
        execution = worker._build_allsequential_virtual_turns_execution(
            request_payload={"model": "git-allsequential", "user_prompt": "hi"},
            targets=["git-chatgpt", "git-perplexity", "git-grok"],
            aggregated_results=[{"index": 1, "model": "git-chatgpt", "status": "completed", "content": "ok"}],
            delivery_errors=[],
            stage="virtual_turns_in_progress",
            kickoff_content="kickoff",
            base_prompt="hi",
        )
        self.assertEqual(execution.get("mode"), "allsequential_virtual_turns")
        self.assertEqual(execution.get("next_index"), 2)
        self.assertEqual(execution.get("success_count"), 1)
        self.assertEqual(execution.get("failure_count"), 0)

    def test_build_execution_state_complete_contains_summary(self) -> None:
        worker = JobWorker()
        execution = worker._build_allsequential_virtual_turns_execution(
            request_payload={"model": "git-allsequential", "user_prompt": "hi"},
            targets=["git-chatgpt"],
            aggregated_results=[{"index": 1, "model": "git-chatgpt", "status": "completed", "content": "ok"}],
            delivery_errors=[],
            stage="virtual_turns_complete",
            kickoff_content="kickoff",
            base_prompt="hi",
        )
        self.assertEqual(execution.get("next_index"), 2)
        self.assertIn("allsequential_summary", execution)
        self.assertIsInstance(execution.get("allsequential_summary"), str)
        self.assertTrue(execution.get("allsequential_summary"))


if __name__ == "__main__":
    unittest.main()
