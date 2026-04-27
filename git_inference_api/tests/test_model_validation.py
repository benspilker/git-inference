from __future__ import annotations

import unittest

from fastapi import HTTPException

from git_inference_api.app.main import classify_route_hint, ensure_supported_model_or_raise, is_supported_model


class ModelValidationTests(unittest.TestCase):
    def test_supported_model_is_accepted(self) -> None:
        self.assertTrue(is_supported_model("git-chatgpt"))
        ensure_supported_model_or_raise("git-chatgpt")

    def test_parallel_model_is_accepted(self) -> None:
        self.assertTrue(is_supported_model("git-parallel"))
        ensure_supported_model_or_raise("git-parallel")

    def test_synth_model_is_accepted(self) -> None:
        self.assertTrue(is_supported_model("git-synth"))
        ensure_supported_model_or_raise("git-synth")

    def test_classify_route_hint_treats_jobs_topic_as_question(self) -> None:
        route = classify_route_hint(
            {
                "user_prompt": "Tell me what are the most lucrative jobs I can do on Fiverr and Upwork? Give me examples.",
            }
        )
        self.assertEqual(route, "question")

    def test_classify_route_hint_keeps_schedule_as_job(self) -> None:
        route = classify_route_hint(
            {
                "user_prompt": "Schedule a weather report every day at 8:30am.",
            }
        )
        self.assertEqual(route, "job")

    def test_supported_tail_model_is_accepted(self) -> None:
        self.assertTrue(is_supported_model("providers/custom/git-grok"))
        ensure_supported_model_or_raise("providers/custom/git-grok")

    def test_unsupported_model_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            ensure_supported_model_or_raise("git-unknown-model")
        self.assertEqual(ctx.exception.status_code, 400)
        detail = ctx.exception.detail
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail.get("code"), "INVALID_MODEL")
        self.assertIn("supported_models", detail)


if __name__ == "__main__":
    unittest.main()
