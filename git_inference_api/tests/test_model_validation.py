from __future__ import annotations

import unittest

from fastapi import HTTPException

from git_inference_api.app.main import ensure_supported_model_or_raise, is_supported_model


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
