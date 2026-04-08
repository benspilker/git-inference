from __future__ import annotations

import json
import time
from pathlib import Path

from .browser_session import (
    assistant_turns,
    find_chat_composer,
    send_prompt,
    stabilize_response,
    wait_for_valid_response,
)
from .prompt_contracts import extract_json_payload
from .recovery import click_retry_if_visible, refresh_chat


def run_stage_once(
    page,
    timeout_ms: int,
    prompt_text: str,
    wait_seconds: int,
    post_response_wait_seconds: int,
    response_settle_seconds: int,
    max_settle_wait_seconds: int,
    allow_retry: bool = True,
    refresh_before_retry: bool = False,
) -> tuple[str, dict]:
    metadata = {
        "attempt": 1,
        "retry_count": 0,
        "used_retry": False,
        "page_refreshed": False,
        "thread_reused": True,
        "new_chat_started": False,
        "failure_reason": None,
    }
    start = time.time()

    def _single_attempt():
        composer = find_chat_composer(page, timeout_ms=timeout_ms)
        if composer is None:
            raise RuntimeError("Could not find composer for stage execution.")
        assistant_messages = assistant_turns(page)
        before_count = assistant_messages.count()
        before_last_text = ""
        if before_count > 0:
            try:
                before_last_text = assistant_messages.last.inner_text().strip()
            except Exception:
                before_last_text = ""
        send_prompt(page, composer, prompt_text)
        page.wait_for_timeout(1200)
        assistant_messages = assistant_turns(page)
        response_text = wait_for_valid_response(
            page,
            assistant_messages,
            before_count=before_count,
            before_last_text=before_last_text,
            wait_seconds=wait_seconds,
        )
        if not response_text:
            raise RuntimeError("Assistant response was empty for stage submission.")
        refreshed = stabilize_response(
            page,
            assistant_messages,
            post_response_wait_seconds=post_response_wait_seconds,
            response_settle_seconds=response_settle_seconds,
            max_settle_wait_seconds=max_settle_wait_seconds,
        )
        return refreshed or response_text

    try:
        response = _single_attempt()
    except Exception as exc:
        if not allow_retry:
            metadata["failure_reason"] = str(exc)
            raise
        metadata["used_retry"] = True
        metadata["retry_count"] = 1
        metadata["attempt"] = 2
        if refresh_before_retry:
            refresh_chat(page, timeout_ms)
            metadata["page_refreshed"] = True
        else:
            click_retry_if_visible(page)
        response = _single_attempt()

    end = time.time()
    metadata["start_time"] = start
    metadata["end_time"] = end
    metadata["duration_seconds"] = round(end - start, 3)
    return response, metadata


def write_stage_outputs(
    response_text: str,
    output_file: Path,
    metadata_output_file: Path | None,
    parsed_output_file: Path | None,
    stage_name: str,
    expect_json: bool,
    run_metadata: dict | None = None,
    response_mode: str = "text",
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(response_text + "\n", encoding="utf-8")

    parsed = extract_json_payload(response_text) if expect_json else None
    if expect_json and parsed_output_file is not None and parsed is not None:
        parsed_output_file.parent.mkdir(parents=True, exist_ok=True)
        parsed_output_file.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if metadata_output_file is not None:
        metadata_output_file.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "stage_name": stage_name,
            "expect_json": expect_json,
            "parsed_json": parsed is not None if expect_json else False,
            "response_mode": response_mode,
            "response_chars": len(response_text or ""),
        }
        if isinstance(run_metadata, dict):
            metadata.update(run_metadata)
        metadata_output_file.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
