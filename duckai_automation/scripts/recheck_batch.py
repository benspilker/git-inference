#!/usr/bin/env python3
"""Re-run rendered-page QA for a committed Duck.ai candidate batch."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
RUNS_DIR = REPO / "duckai-runs"
BROWSER_QA_SCRIPT = ROOT / "scripts" / "browser_qa.py"
CHECK_SCRIPT = ROOT / "scripts" / "check_website.py"
BATCH_PATTERN = re.compile(r"^run-(\d+)-(\d+)-candidate-(\d+)$")


def _latest_batch() -> tuple[str, str]:
    batches = set()
    for folder in RUNS_DIR.iterdir():
        match = BATCH_PATTERN.fullmatch(folder.name)
        if folder.is_dir() and match:
            batches.add((int(match.group(1)), int(match.group(2))))
    if not batches:
        raise RuntimeError("No committed Duck.ai candidate batches were found.")
    run_id, attempt = max(batches)
    return str(run_id), str(attempt)


def _read_checker(path: Path, request: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), str(path), request],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Checker output for {path.name} was not valid JSON: {result.stderr}") from error
    report["checker_exit_code"] = result.returncode
    (path / "debug" / "checker_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="GitHub Actions run ID; defaults to the newest saved batch")
    parser.add_argument("--attempt", default="1", help="GitHub Actions run attempt number")
    args = parser.parse_args()
    run_id, attempt = (args.run_id, args.attempt) if args.run_id else _latest_batch()
    if not run_id.isdigit() or not attempt.isdigit():
        raise RuntimeError("Run ID and attempt must be numeric.")

    candidates = sorted(
        (path for path in RUNS_DIR.glob(f"run-{run_id}-{attempt}-candidate-*") if path.is_dir()),
        key=lambda path: int(BATCH_PATTERN.fullmatch(path.name).group(3)),
    )
    if not candidates:
        raise RuntimeError(f"No candidate folders found for run {run_id}, attempt {attempt}.")

    successful = []
    for candidate in candidates:
        inputs_path = candidate / "debug" / "inputs.json"
        if inputs_path.is_file():
            request = json.loads(inputs_path.read_text(encoding="utf-8")).get("website_request", "")
        else:
            request = (ROOT / "website_request.txt").read_text(encoding="utf-8").strip()
        if not (candidate / "files" / "inlined" / "index.html").is_file():
            print(f"Skipping incomplete folder {candidate.name}.")
            continue
        qa = subprocess.run(
            [sys.executable, str(BROWSER_QA_SCRIPT), str(candidate), request],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        report_path = candidate / "debug" / "browser_qa.json"
        if qa.returncode != 0 or not report_path.is_file():
            raise RuntimeError(f"Browser QA could not complete for {candidate.name}: {qa.stderr}")
        browser_report = json.loads(report_path.read_text(encoding="utf-8"))
        checker_report = _read_checker(candidate, request)
        notes_path = candidate / "run_notes.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.is_file() else {}
        auto_pass = bool(checker_report.get("automated_success"))
        notes.update(
            {
                "automated_success": auto_pass,
                "status": (
                    "automated_checks_passed_pending_human_review" if auto_pass else "automated_checks_failed"
                ),
                "browser_qa_status": browser_report.get("status", "invalid"),
                "final_success": bool(checker_report.get("final_success")),
                "visual_review_status": checker_report.get("visual_review_status", "pending"),
            }
        )
        notes_path.write_text(json.dumps(notes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if auto_pass:
            successful.append(candidate.name)
        print(
            f"{candidate.name}: automated_success={str(auto_pass).lower()}, "
            f"browser_qa={browser_report.get('status', 'invalid')}"
        )

    output = {"run_id": run_id, "attempt": attempt, "candidate_count": len(candidates), "successful_count": len(successful), "successful_candidates": successful}
    print(json.dumps(output, ensure_ascii=False))
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as handle:
            for key, value in (("run_id", run_id), ("attempt", attempt), ("successful_count", len(successful))):
                handle.write(f"{key}={value}\n")
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as handle:
            handle.write(f"## Rechecked Duck.ai batch {run_id}-{attempt}\n\n")
            handle.write(f"Automated passes: **{len(successful)} of {len(candidates)} candidates**.\n\n")
            handle.write("Human visual and image-relevance review remains pending.\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Candidate batch QA failed: {error}", file=sys.stderr)
        raise SystemExit(1)
