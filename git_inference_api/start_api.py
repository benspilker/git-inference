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
