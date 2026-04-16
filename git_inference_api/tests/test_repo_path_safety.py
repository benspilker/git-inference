from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from git_inference_api.app.git_ops import _looks_like_source_repo


class RepoPathSafetyTests(unittest.TestCase):
    def test_detects_source_repo_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
            (root / ".github" / "workflows" / "process-requests.yml").write_text("name: test\n", encoding="utf-8")
            (root / "git_inference_api" / "app").mkdir(parents=True, exist_ok=True)
            (root / "git_inference_api" / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")
            self.assertTrue(_looks_like_source_repo(root))

    def test_non_source_repo_path_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
            (root / ".github" / "workflows" / "process-requests.yml").write_text("name: test\n", encoding="utf-8")
            self.assertFalse(_looks_like_source_repo(root))


if __name__ == "__main__":
    unittest.main()
