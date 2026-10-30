#!/usr/bin/env python3
"""Smoke-test two-turn logged-out Duck.ai capture and save the transcript."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
RUN_ATTEMPT = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
RUN_DIR = REPO / "duckai-runs" / f"smoke-run-{RUN_ID}-{RUN_ATTEMPT}"
CONFIG = ROOT / "smoke_test.json"


def fail(message: str) -> None:
    raise RuntimeError(message)


def write_failure_summary(error: Exception, page) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        title = page.title()
        current_url = page.url
        body = page.locator("body").inner_text(timeout=3000)
    except Exception as diagnostic_error:
        title = "unavailable"
        current_url = "unavailable"
        body = f"Could not read page diagnostics: {diagnostic_error}"
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("## Duck.ai smoke test failed\n\n")
        summary.write(f"- Error: `{str(error).replace('`', '\\`')}`\n")
        summary.write(f"- Page title: `{title}`\n")
        summary.write(f"- Page URL: `{current_url}`\n\n")
        summary.write("Visible page text tail (truncated to 1,500 characters):\n\n")
        summary.write("```text\n" + body[-1500:].replace("```", "'''\u200b") + "\n```\n")


def main_area(page):
    main = page.locator("main").first
    return main if main.count() else page.locator("body")


def find_composer(page):
    for locator in (
        page.get_by_placeholder("Ask anything privately"),
        page.locator("[data-testid='duckai-chat-input'] textarea"),
        page.locator("textarea[placeholder*='Ask']"),
        page.locator("textarea"),
        page.locator("[contenteditable='true'][role='textbox']"),
    ):
        try:
            if locator.count() and locator.last.is_visible() and locator.last.is_enabled():
                return locator.last
        except Exception:
            continue
    return None


def wait_for_guest_chat(page):
    deadline = time.monotonic() + 45
    gate_terms = (
        "verify you are human",
        "verify that you are human",
        "captcha",
        "sign in to continue",
        "usage limit reached",
        "temporarily unavailable",
    )
    while time.monotonic() < deadline:
        composer = find_composer(page)
        if composer:
            return composer
        try:
            body_text = page.locator("body").inner_text(timeout=1500).lower()
            if any(term in body_text for term in gate_terms):
                fail("Duck.ai guest chat is gated or unavailable; stopped without bypassing the gate.")
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(500)
    fail("Could not find Duck.ai's guest-chat composer before timeout.")


def count_marker(page, marker: str) -> int:
    text = main_area(page).inner_text(timeout=3000)
    return text.count(marker)


def extract_latest_marked_message(page, marker: str) -> str:
    # The smoke answers are requested as one sentence, so the containing
    # paragraph is the smallest message-scoped rendered text node.
    extracted = main_area(page).evaluate(
        """(root, marker) => {
          const all = Array.from(root.querySelectorAll('p, li, blockquote'));
          const matches = all.filter(node => (node.innerText || '').includes(marker));
          if (matches.length) return matches[matches.length - 1].innerText.trim();
          const messageNodes = Array.from(root.querySelectorAll('[role="article"], [data-testid*="message"], [data-testid*="assistant"]'));
          const messageMatches = messageNodes.filter(node => (node.innerText || '').includes(marker));
          if (messageMatches.length) return messageMatches[messageMatches.length - 1].innerText.trim();
          return '';
        }""",
        marker,
    )
    text = (extracted or "").strip()
    if marker not in text or len(text) < len(marker) + 4 or len(text) > 1500:
        return ""
    return text


def send_and_capture(page, turn: dict) -> str:
    marker = turn["marker"]
    prompt = turn["prompt"]
    before_count = count_marker(page, marker)
    composer = wait_for_guest_chat(page)
    composer.fill(prompt, timeout=15000)
    send_candidates = (
        page.get_by_role("button", name=re.compile(r"send", re.IGNORECASE)),
        page.locator("button[aria-label*='send' i]"),
        page.locator("button[data-testid*='send' i]"),
        main_area(page).locator("button[type='submit']"),
    )
    sent = False
    for candidate in send_candidates:
        try:
            if candidate.count() and candidate.last.is_visible() and candidate.last.is_enabled():
                candidate.last.click(timeout=5000)
                sent = True
                break
        except PlaywrightTimeoutError:
            continue
    if not sent:
        try:
            composer.press("Enter", timeout=5000)
        except PlaywrightTimeoutError:
            fail("Duck.ai composer was found, but neither a Send control nor Enter could submit the prompt.")

    deadline = time.monotonic() + 180
    previous = ""
    stable = 0
    while time.monotonic() < deadline:
        # There is one marker in the submitted user prompt and one in the
        # assistant response. The response itself is captured from its message.
        if count_marker(page, marker) >= before_count + 2:
            response = extract_latest_marked_message(page, marker)
            stop = page.get_by_role("button", name=re.compile(r"stop", re.IGNORECASE)).last
            try:
                stop_visible = stop.count() > 0 and stop.is_visible()
            except Exception:
                stop_visible = False
            if response and response == previous and not stop_visible:
                stable += 1
            else:
                stable = 0
            previous = response
            if stable >= 3:
                return response
        page.wait_for_timeout(1000)
    visible_tail = main_area(page).inner_text(timeout=3000)[-1000:].replace("\n", " ")
    marker_count = count_marker(page, marker)
    extracted = extract_latest_marked_message(page, marker)
    fail(
        f"Timed out waiting for a stable answer marked {marker}; "
        f"marker appearances={marker_count}; extracted response={extracted!r}; "
        f"visible chat tail={visible_tail!r}"
    )


def main() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    turns = config.get("turns")
    if not isinstance(turns, list) or len(turns) != 2:
        fail("smoke_test.json must contain exactly two turns.")
    RUN_DIR.mkdir(parents=True, exist_ok=False)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_default_timeout(15000)
        results = []
        chat_url = ""
        try:
            page.goto("https://duck.ai/", wait_until="domcontentloaded", timeout=60000)
            wait_for_guest_chat(page)
            for turn in turns:
                response = send_and_capture(page, turn)
                if not chat_url:
                    chat_url = page.url
                elif page.url != chat_url:
                    fail("Duck.ai conversation URL changed between turns; refusing to combine chats.")
                results.append(
                    {
                        "turn_id": turn["id"],
                        "prompt": turn["prompt"],
                        "assistant_response": response,
                        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                    }
                )
        except Exception as error:
            write_failure_summary(error, page)
            raise
        finally:
            context.close()
            browser.close()

    conversation = {
        "status": "captured",
        "site": "https://duck.ai/",
        "chat_url": chat_url,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "turn_count": len(results),
        "turns": results,
    }
    (RUN_DIR / "conversation.json").write_text(
        json.dumps(conversation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    lines = ["# Duck.ai multi-turn smoke test", "", f"Chat: {chat_url}", ""]
    for item in results:
        lines.extend(
            [
                f"## {item['turn_id']}",
                "",
                "**User**",
                "",
                item["prompt"],
                "",
                "**Duck.ai**",
                "",
                item["assistant_response"],
                "",
            ]
        )
    (RUN_DIR / "conversation.md").write_text("\n".join(lines), encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("## Duck.ai smoke test passed\n\n")
            summary.write(f"- Captured turns: {len(results)} in one chat\n")
            summary.write(f"- Saved transcript: `{RUN_DIR.relative_to(REPO).as_posix()}/conversation.md`\n")
    print(f"Captured {len(results)} turns in {chat_url}; transcript saved to {RUN_DIR}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"::error title=Duck.ai smoke test::{error}")
        print(f"Duck.ai smoke test failed: {error}", file=sys.stderr)
        raise SystemExit(1)
