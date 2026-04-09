#!/usr/bin/env python3
"""Stage-capable Playwright runner for Perplexity guest browser execution."""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright_runner.chunk_orchestrator import run_chunk_plan
from playwright_runner.diagnostics import enable_network_logging, save_failure_diagnostics
from playwright_runner.perplexity_recovery import refresh_chat, start_new_chat_if_available
from playwright_runner.perplexity_stage_runner import run_stage_once, write_stage_outputs
from playwright_runner.prompt_contracts import build_prompt_text, extract_json_payload


def ensure_package(module_name: str, pip_name: str | None = None):
    target = pip_name or module_name.split(".")[0]
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", target])
        return importlib.import_module(module_name)


def ensure_browser_installed() -> None:
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a named Playwright stage against Perplexity as guest.")
    parser.add_argument("--stage-name", default="generic")
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--instructions-file")
    parser.add_argument("--context-file")
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--parsed-output-file")
    parser.add_argument("--metadata-output-file")
    parser.add_argument("--expect-json", action="store_true")
    parser.add_argument("--fail-if-invalid-json", action="store_true")
    parser.add_argument("--response-mode", default="text", choices=["text", "markdown", "json"])
    parser.add_argument("--url", default="https://www.perplexity.ai/")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--wait-seconds", type=int, default=60)
    parser.add_argument("--post-response-wait-seconds", type=int, default=10)
    parser.add_argument("--response-settle-seconds", type=int, default=8)
    parser.add_argument("--max-settle-wait-seconds", type=int, default=180)
    parser.add_argument("--user-data-dir", default=".playwright-perplexity-profile")
    parser.add_argument("--omit-sections", default="")
    parser.add_argument("--start-new-chat", action="store_true")
    parser.add_argument("--refresh-before-send", action="store_true")
    parser.add_argument("--allow-retry", action="store_true")
    parser.add_argument("--refresh-before-retry", action="store_true")
    parser.add_argument("--network-log", action="store_true")
    parser.add_argument("--network-log-file", default="perplexity_network.log")
    parser.add_argument("--error-screenshot", default="perplexity_playwright_error.png")
    parser.add_argument("--chunks", type=int, default=1)
    parser.add_argument("--chunk-mode", choices=["none", "map_reduce", "finalize_on_last_chunk", "legacy"], default="none")
    return parser.parse_args()


def assert_perplexity_domain(page) -> None:
    current_url = page.url or ""
    host = (urlparse(current_url).netloc or "").lower()
    if "perplexity.ai" not in host:
        raise RuntimeError(f"Unexpected browser destination: {current_url} (expected host containing perplexity.ai)")


def run() -> int:
    args = parse_args()
    playwright_sync_api = ensure_package("playwright.sync_api")
    sync_playwright = playwright_sync_api.sync_playwright

    input_file = Path(args.input_file).expanduser().resolve()
    instructions_file = Path(args.instructions_file).expanduser().resolve() if args.instructions_file else None
    context_file = Path(args.context_file).expanduser().resolve() if args.context_file else None
    output_file = Path(args.output_file).expanduser().resolve()
    parsed_output_file = Path(args.parsed_output_file).expanduser().resolve() if args.parsed_output_file else None
    metadata_output_file = Path(args.metadata_output_file).expanduser().resolve() if args.metadata_output_file else None
    user_data_dir = Path(args.user_data_dir).expanduser().resolve()
    network_log_file = Path(args.network_log_file).expanduser().resolve()
    error_screenshot = Path(args.error_screenshot).expanduser().resolve() if args.error_screenshot else None

    omit_sections = [s.strip() for s in args.omit_sections.split(",") if s.strip()]
    input_text, instructions_text = build_prompt_text(input_file, instructions_file=instructions_file, omit_sections=omit_sections)

    prompt_parts = []
    if context_file is not None and context_file.exists():
        context_text = context_file.read_text(encoding="utf-8").strip()
        if context_text:
            prompt_parts.append("Structured context:\n" + context_text)
    prompt_parts.append(input_text)
    prompt_text = "\n\n".join([p for p in prompt_parts if p]).strip()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            enable_network_logging(page, network_log_file, enabled=args.network_log)
            page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            assert_perplexity_domain(page)

            if args.refresh_before_send:
                refresh_chat(page, args.timeout_ms)
                assert_perplexity_domain(page)

            started_new_chat = False
            if args.start_new_chat:
                started_new_chat = start_new_chat_if_available(page, args.timeout_ms)
                assert_perplexity_domain(page)

            if args.chunk_mode != "none" and args.chunks > 1:
                response_text = run_chunk_plan(
                    page=page,
                    prompt_text=prompt_text,
                    instructions_text=instructions_text,
                    chunks=args.chunks,
                    mode=args.chunk_mode,
                    timeout_ms=args.timeout_ms,
                    wait_seconds=args.wait_seconds,
                    post_response_wait_seconds=args.post_response_wait_seconds,
                    response_settle_seconds=args.response_settle_seconds,
                    max_settle_wait_seconds=args.max_settle_wait_seconds,
                    run_stage_once_fn=run_stage_once,
                )
                run_metadata = {
                    "attempt": 1,
                    "retry_count": 0,
                    "used_retry": False,
                    "page_refreshed": bool(args.refresh_before_send),
                    "thread_reused": not started_new_chat,
                    "new_chat_started": started_new_chat,
                    "chunk_mode": args.chunk_mode,
                    "chunks": args.chunks,
                }
            else:
                response_text, run_metadata = run_stage_once(
                    page=page,
                    timeout_ms=args.timeout_ms,
                    prompt_text=(f"{instructions_text}\n\n{prompt_text}" if instructions_text else prompt_text),
                    wait_seconds=args.wait_seconds,
                    post_response_wait_seconds=args.post_response_wait_seconds,
                    response_settle_seconds=args.response_settle_seconds,
                    max_settle_wait_seconds=args.max_settle_wait_seconds,
                    allow_retry=args.allow_retry,
                    refresh_before_retry=args.refresh_before_retry,
                    stage_name=args.stage_name,
                    error_screenshot=error_screenshot,
                )
                run_metadata["page_refreshed"] = bool(run_metadata.get("page_refreshed")) or bool(args.refresh_before_send)
                run_metadata["thread_reused"] = not started_new_chat
                run_metadata["new_chat_started"] = started_new_chat

            run_metadata["final_url"] = page.url

            if args.expect_json and args.fail_if_invalid_json and extract_json_payload(response_text) is None:
                raise RuntimeError(f"Stage {args.stage_name} expected valid JSON but did not receive it.")

            write_stage_outputs(
                response_text=response_text,
                output_file=output_file,
                metadata_output_file=metadata_output_file,
                parsed_output_file=parsed_output_file,
                stage_name=args.stage_name,
                expect_json=bool(args.expect_json),
                run_metadata=run_metadata,
                response_mode=args.response_mode,
            )
        except Exception:
            save_failure_diagnostics(page, error_screenshot)
            raise
        finally:
            context.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:
        msg = str(exc).lower()
        if "executable doesn't exist" in msg or "please run the following command to download new browsers" in msg:
            ensure_browser_installed()
            raise SystemExit(run())
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
