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
QUESTION_ROUTE_HINTS = ("what", "which", "when", "where", "why", "how", "?")
JOB_ROUTE_HINTS = ("set up", "schedule", "remind me", "configure", "create", "cron", "run this daily")
RESEARCH_ROUTE_HINTS = ("research", "investigate", "deeply compare", "build a report", "from many angles")
DEFAULT_OPENCLAW_COMPAT_MODELS = ("git-chatgpt", "git-perplexity", "git-grok")


def _env_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in FALSEY


def _openclaw_compat_model_tails() -> set[str]:
    configured = os.getenv("OPENCLAW_COMPAT_MODELS", "")
    if configured.strip():
        names = [n.strip().lower() for n in configured.split(",") if n.strip()]
    else:
        names = list(DEFAULT_OPENCLAW_COMPAT_MODELS)
    tails = set()
    for name in names:
        if not name:
            continue
        tails.add(name.split("/")[-1])
    return tails


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


def _truncate(text: str, max_chars: int = 300) -> str:
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


def _classify_route_hint(question_text: str) -> str:
    lower = (question_text or "").strip().lower()
    if any(marker in lower for marker in RESEARCH_ROUTE_HINTS):
        return "research"
    if any(marker in lower for marker in JOB_ROUTE_HINTS):
        return "job"
    if any(marker in lower for marker in QUESTION_ROUTE_HINTS):
        return "question"
    return "question"


def _build_continuity_summary(previous_question: str, previous_response: str) -> str:
    return (
        "Recent continuity:\n"
        f"- Previous user topic: {_truncate(previous_question, 180)}\n"
        f"- Previous assistant focus: {_truncate(previous_response, 260)}\n\n"
        "Current user message:\n"
    )


def _find_previous_exchange_summary(
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
        if not response_content or response_content == "NO_REPLY":
            continue

        return prior_question_compact or prior_question.strip(), response_content

    return None


def main() -> None:
    request_path = Path(sys.argv[1])
    prompt_path = Path(sys.argv[2])
    question_path = Path(sys.argv[3])
    model_path = Path(sys.argv[4])
    startup_path = Path(sys.argv[5])
    context_path = Path(sys.argv[6]) if len(sys.argv) > 6 else None

    payload = json.loads(request_path.read_text(encoding="utf-8"))
    system_prompt = str(payload.get("system_prompt") or "").strip()
    user_prompt = str(payload.get("user_prompt") or "").strip()
    nested = payload.get("request")
    model_name = str(nested.get("model") or "") if isinstance(nested, dict) else ""
    messages = nested.get("messages", []) if isinstance(nested, dict) else []
    if not isinstance(messages, list):
        messages = []

    routing_metadata = payload.get("routing_metadata")
    if not isinstance(routing_metadata, dict) and isinstance(nested, dict):
        routing_metadata = nested.get("routing_metadata")
    if not isinstance(routing_metadata, dict):
        routing_metadata = {}

    transport = payload.get("transport")
    if not isinstance(transport, dict) and isinstance(nested, dict):
        transport = nested.get("transport")
    if not isinstance(transport, dict):
        transport = {}

    sys_text = _last_message_by_role(messages, "system")
    user_text = _last_message_by_role(messages, "user")

    normalized_model = model_name.strip().lower()
    model_tail = normalized_model.split("/")[-1] if normalized_model else ""
    is_openclaw_compat = model_tail in _openclaw_compat_model_tails()
    continuity_enabled = _env_enabled("SIMPLE_MODEL_CARRY_PREVIOUS_QA", default=False)

    parts = [x for x in (system_prompt, user_prompt) if x]
    if not parts:
        parts = [x for x in (sys_text, user_text) if x]

    final_prompt = (user_prompt or user_text).strip() if is_openclaw_compat else "\n\n".join(parts).strip()
    question_text = (user_prompt or user_text or final_prompt).strip()
    lower_question = question_text.lower()
    startup_only = int(is_openclaw_compat and all(marker in lower_question for marker in STARTUP_MARKERS))

    chat_id = _extract_chat_id((system_prompt, sys_text))
    route_hint = str(routing_metadata.get("intent_type") or "").strip().lower() or _classify_route_hint(question_text)
    task_type = str(routing_metadata.get("task_type") or "").strip()

    continuity = {
        "enabled": False,
        "source_job_question": None,
        "summary_applied": False,
    }

    carry_scope_match = _is_first_post_startup_user_message(messages)
    if (
        is_openclaw_compat
        and continuity_enabled
        and not startup_only
        and carry_scope_match
        and route_hint == "question"
    ):
        repo_root = request_path.parent.parent
        previous_exchange = _find_previous_exchange_summary(repo_root, request_path, chat_id, question_text)
        if previous_exchange is not None:
            previous_question, previous_response = previous_exchange
            continuity_block = _build_continuity_summary(previous_question, previous_response)
            final_prompt = f"{continuity_block}{final_prompt}".strip()
            continuity = {
                "enabled": True,
                "source_job_question": _truncate(previous_question, 180),
                "summary_applied": True,
            }

    if not final_prompt:
        raise SystemExit(f"No prompt content found in request artifact: {request_path}")

    prompt_path.write_text(final_prompt, encoding="utf-8")
    question_path.write_text(question_text, encoding="utf-8")
    model_path.write_text(model_name.strip(), encoding="utf-8")
    startup_path.write_text(str(startup_only), encoding="utf-8")

    if context_path is not None:
        chunking = None
        if isinstance(transport.get("chunking"), dict):
            chunking = transport.get("chunking")
        elif isinstance(payload.get("chunking"), dict):
            chunking = payload.get("chunking")
        elif isinstance(nested, dict) and isinstance(nested.get("chunking"), dict):
            chunking = nested.get("chunking")
        if not isinstance(chunking, dict):
            chunking = {}

        def _to_int(value, default: int) -> int:
            try:
                return int(value)
            except Exception:
                return default

        chunking_normalized = {
            "enabled": bool(chunking.get("enabled")),
            "chunk_count": max(1, _to_int(chunking.get("chunk_count"), 1)),
            "max_chunks": max(1, _to_int(chunking.get("max_chunks"), 5)),
            "chunk_size_words": max(1, _to_int(chunking.get("chunk_size_words"), 2000)),
            "word_count": max(0, _to_int(chunking.get("word_count"), 0)),
            "mode": str(chunking.get("mode") or "").strip().lower(),
        }
        context_payload = {
            "job_id": str(payload.get("job_id") or request_path.stem),
            "model": model_name.strip(),
            "question_text": question_text,
            "system_prompt": system_prompt or sys_text,
            "startup_only": bool(startup_only),
            "chat_id": chat_id,
            "route_hint": route_hint,
            "task_type": task_type,
            "routing_metadata": routing_metadata,
            "transport": transport,
            "chunking": chunking_normalized,
            "continuity": continuity,
        }
        context_path.write_text(json.dumps(context_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
