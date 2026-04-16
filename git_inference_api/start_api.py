#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
REPO_PATH = os.environ.get("REPO_PATH", "/tmp/git_inference_github/api-workrepo")
REPO_BRANCH = os.environ.get("REPO_BRANCH", "main")
DB_PATH = os.environ.get("DB_PATH", "/tmp/git_inference_github/jobs.db")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = os.environ.get("PORT", "8000")
ALLOW_UNSAFE_REPO_PATH = os.environ.get("ALLOW_UNSAFE_REPO_PATH", "false").strip().lower() == "true"


def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def main() -> int:
    repo_git_dir = Path(REPO_PATH) / ".git"
    if not repo_git_dir.is_dir():
        return fail(
            f"REPO_PATH is not a git repo: {REPO_PATH}. "
            "Set REPO_PATH to your api workrepo (for example /tmp/git_inference_github/api-workrepo)."
        )

    if not ALLOW_UNSAFE_REPO_PATH:
        looks_like_source_repo = (
            (Path(REPO_PATH) / ".github" / "workflows" / "process-requests.yml").exists()
            and (Path(REPO_PATH) / "git_inference_api" / "app" / "main.py").exists()
        )
        if looks_like_source_repo:
            return fail(
                "REPO_PATH appears to be the source repo checkout. "
                "The worker performs destructive sync (fetch/reset --hard/clean) on REPO_PATH. "
                "Use a dedicated workrepo clone, or set ALLOW_UNSAFE_REPO_PATH=true to bypass intentionally."
            )

    fetch = subprocess.run(
        ["git", "-C", REPO_PATH, "fetch", "origin", REPO_BRANCH],
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout).strip() or "unknown git error"
        return fail(f"git fetch origin {REPO_BRANCH} failed in {REPO_PATH}: {detail}")

    env = dict(os.environ)
    env["REPO_PATH"] = REPO_PATH
    env["REPO_BRANCH"] = REPO_BRANCH
    env["DB_PATH"] = DB_PATH
    env["ALLOW_UNSAFE_REPO_PATH"] = "true" if ALLOW_UNSAFE_REPO_PATH else "false"

    print("Starting API with:")
    print(f"  REPO_PATH={REPO_PATH}")
    print(f"  REPO_BRANCH={REPO_BRANCH}")
    print(f"  DB_PATH={DB_PATH}")
    print(f"  HOST={HOST}")
    print(f"  PORT={PORT}")

    cmd = [
        "uvicorn",
        "app.main:app",
        "--host",
        HOST,
        "--port",
        PORT,
        "--app-dir",
        str(APP_DIR),
    ]
    proc = subprocess.run(cmd, env=env)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
