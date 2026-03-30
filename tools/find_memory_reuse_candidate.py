#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "what",
    "when",
    "where",
    "who",
    "why",
    "with",
    "you",
    "your",
}


def token_set(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) > 2 and token not in STOP_WORDS
    }


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def get_candidate_score(payload: dict[str, Any]) -> float | None:
    summary = payload.get("score_summary")
    if isinstance(summary, dict):
        score = safe_float(summary.get("overall_score"))
        if score is not None:
            return score

    evaluation = payload.get("evaluation")
    if isinstance(evaluation, dict):
        scores = evaluation.get("scores")
        if isinstance(scores, list) and scores and isinstance(scores[0], dict):
            return safe_float(scores[0].get("overall_score"))
    return None


def find_best_candidate(
    question_text: str,
    current_job_id: str,
    min_score: float,
    min_similarity: float,
    combined_dir: Path,
) -> dict[str, Any]:
    q_tokens = token_set(question_text)
    if not q_tokens or not combined_dir.exists():
        return {}

    best: dict[str, Any] | None = None

    for candidate_path in sorted(combined_dir.glob("job_*.json")):
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        source_job_id = str(payload.get("job_id") or candidate_path.stem)
        if source_job_id == current_job_id:
            continue
        if payload.get("status") != "completed":
            continue
        if payload.get("error"):
            continue

        evaluation = payload.get("evaluation") if isinstance(payload.get("evaluation"), dict) else {}
        content = evaluation.get("content") if isinstance(evaluation.get("content"), dict) else {}
        categorization = content.get("categorization") if isinstance(content.get("categorization"), dict) else {}
        quality_flags = content.get("quality_flags") if isinstance(content.get("quality_flags"), dict) else {}

        recency_class = str(categorization.get("recency_class") or "stable").strip().lower()
        if recency_class in {"fast_changing", "time_sensitive"}:
            continue
        if bool(quality_flags.get("needs_web_verification")):
            continue

        source_question = str(content.get("question_text_preview") or "").strip()
        if not source_question:
            continue

        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        message = response.get("message") if isinstance(response.get("message"), dict) else {}
        response_content = str(message.get("content") or "").strip()
        if not response_content:
            continue

        score = get_candidate_score(payload)
        if score is None or score < min_score:
            continue

        source_tokens = token_set(source_question)
        if not source_tokens:
            continue
        similarity = len(q_tokens & source_tokens) / max(1, len(q_tokens | source_tokens))
        if similarity < min_similarity:
            continue

        candidate = {
            "source_job_id": source_job_id,
            "source_path": candidate_path.as_posix(),
            "score": round(score, 3),
            "similarity": round(similarity, 4),
            "source_question_preview": source_question,
            "response_content": response_content,
        }
        if best is None or (candidate["similarity"], candidate["score"]) > (best["similarity"], best["score"]):
            best = candidate

    return best or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Find best reusable high-scoring response from combined artifacts.")
    parser.add_argument("--question-file", required=True)
    parser.add_argument("--current-job-id", required=True)
    parser.add_argument("--min-score", required=True, type=float)
    parser.add_argument("--min-similarity", required=True, type=float)
    parser.add_argument("--out-file", required=True)
    args = parser.parse_args()

    question_text = Path(args.question_file).read_text(encoding="utf-8").strip()
    out_path = Path(args.out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = find_best_candidate(
        question_text=question_text,
        current_job_id=args.current_job_id,
        min_score=args.min_score,
        min_similarity=args.min_similarity,
        combined_dir=Path("combined"),
    )
    out_path.write_text(json.dumps(candidate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
