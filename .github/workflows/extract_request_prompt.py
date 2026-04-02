#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable


STARTUP_MARKERS = (
    "a new session was started via /new or /reset",
    "run your session startup sequence",
)

FALSEY = {"0", "false", "no", "off", "n"}


def _env_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in FALSEY


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _last_message_by_role(messages: list[dict], role: str) -> str:
    role = role.strip().lower()
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role", "")).strip().lower() != role:
            continue
        content = str(message.get("content", "")).strip()
        if content:
            return content
    return ""


def _latest_startup_user_index(messages: list[dict]) -> int:
    latest = -1
    for idx, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role", "")).strip().lower() != "user":
            continue
        content = str(message.get("content", "")).lower()
        if all(marker in content for marker in STARTUP_MARKERS):
            latest = idx
    return latest


def _is_first_post_startup_user_message(messages: list[dict]) -> bool:
    """
    Carry-over context should only apply to the first real user message after
    the latest /new startup sequence, not every subsequent turn in the session.
    """
    startup_idx = _latest_startup_user_index(messages)
    if startup_idx < 0:
        return False

    user_messages_after_startup = 0
    for message in messages[startup_idx + 1 :]:
        if not isinstance(message, dict):
            continue
        if str(message.get("role", "")).strip().lower() != "user":
            continue
        if not str(message.get("content", "")).strip():
            continue
        user_messages_after_startup += 1
        if user_messages_after_startup > 1:
            return False

    return user_messages_after_startup == 1


def _extract_chat_id(texts: Iterable[str]) -> str:
    pattern = re.compile(r'"chat_id"\s*:\s*"([^"]+)"')
    for text in texts:
        if not text:
            continue
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return ""


def _squash_question(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    for line in reversed(lines):
        if line != "```":
            return line
    return lines[-1]


def _truncate(text: str, max_chars: int = 1200) -> str:
    clean = (text or "").strip()
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 3].rstrip() + "..."


def _find_response_file(repo_root: Path, job_id: str) -> Path | None:
    direct_candidates = (
        repo_root / "responses" / f"{job_id}.json",
        repo_root / "responses" / "old-responses" / f"{job_id}.json",
    )
    for candidate in direct_candidates:
        if candidate.exists():
            return candidate

    wildcard_dirs = (
        repo_root / "responses",
        repo_root / "responses" / "old-responses",
    )
    for parent in wildcard_dirs:
        if not parent.exists():
            continue
        matches = sorted(parent.glob(f"{job_id}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
    return None


def _find_previous_exchange(
    repo_root: Path,
    current_request: Path,
    chat_id: str,
    current_question: str,
) -> tuple[str, str] | None:
    if not chat_id:
        return None

    request_dirs = (repo_root / "requests", repo_root / "requests" / "old-requests")
    request_files: list[Path] = []
    for directory in request_dirs:
        if directory.exists():
            request_files.extend(directory.glob("job_*.json"))
    request_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    current_question_compact = _squash_question(current_question)
    current_request_resolved = current_request.resolve()

    for request_file in request_files:
        try:
            if request_file.resolve() == current_request_resolved:
                continue
        except OSError:
            pass

        payload = _read_json(request_file)
        if not isinstance(payload, dict):
            continue

        nested = payload.get("request")
        messages = nested.get("messages", []) if isinstance(nested, dict) else []
        if not isinstance(messages, list):
            messages = []

        system_prompt = str(payload.get("system_prompt") or "")
        sys_text = _last_message_by_role(messages, "system")
        request_chat_id = _extract_chat_id((system_prompt, sys_text))
        if request_chat_id != chat_id:
            continue

        user_prompt = str(payload.get("user_prompt") or "").strip()
        user_text = _last_message_by_role(messages, "user")
        prior_question = user_prompt or user_text
        if not prior_question:
            continue
        prior_question_compact = _squash_question(prior_question)
        if prior_question_compact and prior_question_compact == current_question_compact:
            continue

        job_id = str(payload.get("job_id") or request_file.stem).strip()
        if not job_id:
            continue

        response_path = _find_response_file(repo_root, job_id)
        if response_path is None:
            continue
        response_payload = _read_json(response_path)
        if not isinstance(response_payload, dict):
            continue
        response_message = response_payload.get("message")
        if not isinstance(response_message, dict):
            continue
        response_content = str(response_message.get("content") or "").strip()
        if not response_content:
            continue
        if response_content == "NO_REPLY":
            continue

        return prior_question_compact or prior_question.strip(), response_content

    return None


def main() -> None:
    request_path = Path(sys.argv[1])
    prompt_path = Path(sys.argv[2])
    question_path = Path(sys.argv[3])
    model_path = Path(sys.argv[4])
    startup_path = Path(sys.argv[5])

    payload = json.loads(request_path.read_text(encoding="utf-8"))
    system_prompt = str(payload.get("system_prompt") or "").strip()
    user_prompt = str(payload.get("user_prompt") or "").strip()
    nested = payload.get("request")
    model_name = str(nested.get("model") or "") if isinstance(nested, dict) else ""
    messages = nested.get("messages", []) if isinstance(nested, dict) else []
    if not isinstance(messages, list):
        messages = []

    sys_text = _last_message_by_role(messages, "system")
    user_text = _last_message_by_role(messages, "user")
    simple_model = model_name.strip() == "git-chatgpt"
    carry_previous_qa_enabled = _env_enabled("SIMPLE_MODEL_CARRY_PREVIOUS_QA", default=True)

    parts = [x for x in (system_prompt, user_prompt) if x]
    if not parts:
        parts = [x for x in (sys_text, user_text) if x]

    final_prompt = (user_prompt or user_text).strip() if simple_model else "\n\n".join(parts).strip()
    question_text = (user_prompt or user_text or final_prompt).strip()
    lower_question = question_text.lower()
    startup_only = int(simple_model and all(marker in lower_question for marker in STARTUP_MARKERS))

    carry_scope_match = _is_first_post_startup_user_message(messages)
    if simple_model and carry_previous_qa_enabled and not startup_only and carry_scope_match:
        chat_id = _extract_chat_id((system_prompt, sys_text))
        repo_root = request_path.parent.parent
        previous_exchange = _find_previous_exchange(repo_root, request_path, chat_id, question_text)
        if previous_exchange is not None:
            previous_question, previous_response = previous_exchange
            continuity_block = (
                "Continuity context from your immediately previous exchange:\n"
                f"- Previous user question: {_truncate(previous_question, 500)}\n"
                f"- Previous assistant response: {_truncate(previous_response, 1200)}\n\n"
                "Current user message:\n"
            )
            final_prompt = f"{continuity_block}{final_prompt}".strip()

    if not final_prompt:
        raise SystemExit(f"No prompt content found in request artifact: {request_path}")

    prompt_path.write_text(final_prompt, encoding="utf-8")
    question_path.write_text(question_text, encoding="utf-8")
    model_path.write_text(model_name.strip(), encoding="utf-8")
    startup_path.write_text(str(startup_only), encoding="utf-8")


if __name__ == "__main__":
    main()
