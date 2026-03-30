#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def confidence_rank(value: str | None) -> int:
    band = str(value or "").strip().lower()
    mapping = {"low": 1, "medium": 2, "high": 3}
    return mapping.get(band, 0)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def get_primary_score(payload: dict[str, Any]) -> tuple[float | None, str | None]:
    scores = payload.get("scores")
    if isinstance(scores, list) and scores and isinstance(scores[0], dict):
        first = scores[0]
        try:
            score = float(first.get("overall_score"))
        except Exception:
            score = None
        confidence = first.get("confidence_band")
        confidence = str(confidence).strip().lower() if isinstance(confidence, str) else None
        return score, confidence
    return None, None


def determine_tier(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 9.0:
        return "gold"
    if score >= 8.5:
        return "silver"
    if score >= 7.5:
        return "bronze"
    return None


def apply_policy(payload: dict[str, Any], min_score: float, required_confidence: str) -> dict[str, Any]:
    promotion = payload.get("promotion") if isinstance(payload.get("promotion"), dict) else {}
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    quality_flags = content.get("quality_flags") if isinstance(content.get("quality_flags"), dict) else {}

    score, confidence = get_primary_score(payload)

    blocked_reason: str | None = None
    if score is None:
        blocked_reason = "missing_score"
    elif confidence_rank(confidence) < confidence_rank(required_confidence):
        blocked_reason = f"confidence_below_{required_confidence}"
    elif bool(quality_flags.get("needs_web_verification")):
        blocked_reason = "needs_web_verification"
    elif bool(quality_flags.get("safety_or_policy_risk")):
        blocked_reason = "safety_or_policy_risk"
    elif score < min_score:
        blocked_reason = f"score_below_{min_score:.2f}"

    memory_eligible = blocked_reason is None
    tier = determine_tier(score) if memory_eligible else None

    promotion["memory_eligible"] = memory_eligible
    promotion["memory_tier"] = tier
    promotion["effective_score"] = score
    if memory_eligible:
        promotion["promotion_reason"] = f"score_and_confidence_pass(score>={min_score:.2f},confidence>={required_confidence})"
        promotion["blocked_reason"] = None
    else:
        promotion["promotion_reason"] = None
        promotion["blocked_reason"] = blocked_reason

    payload["promotion"] = promotion
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply deterministic promotion policy to evaluation JSON.")
    parser.add_argument("--eval-file", required=True)
    parser.add_argument("--min-score", type=float, required=True)
    parser.add_argument("--required-confidence", default="high")
    args = parser.parse_args()

    eval_path = Path(args.eval_file)
    payload = read_json(eval_path)
    if payload is None:
        return 0

    updated = apply_policy(payload, min_score=args.min_score, required_confidence=args.required_confidence)
    eval_path.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
