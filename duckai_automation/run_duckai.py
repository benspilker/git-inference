#!/usr/bin/env python3
"""Generate a website in Duck.ai, capture six labeled chunks, and save a run."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
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
CANDIDATE_INDEX = os.environ.get("CANDIDATE_INDEX", "1")
if not CANDIDATE_INDEX.isdigit() or int(CANDIDATE_INDEX) not in (1, 2, 3):
    raise RuntimeError("CANDIDATE_INDEX must be 1, 2, or 3.")
RUN_DIR = REPO / "duckai-runs" / f"run-{RUN_ID}-{RUN_ATTEMPT}-candidate-{CANDIDATE_INDEX}"
CAPTURE_SCRIPT = ROOT / "scripts" / "capture_chunk.py"
ASSEMBLE_SCRIPT = ROOT / "scripts" / "assemble_chunks.py"
CHECK_SCRIPT = ROOT / "scripts" / "check_website.py"

BASE_CONTRACT = """Follow the supplied facts and design brief. Build a polished, responsive, business-specific website. Use exactly three CSS responses followed by exactly three HTML responses, one complete code block per response. CSS responses use a css fence; HTML responses use a text fence containing raw HTML. Each response must state its exact part label. The first nonblank line inside its code block must be exactly `/* CSS PART n OF 3 */` or `<!-- HTML PART n OF 3 -->` for that part. Keep the CSS and HTML coherent and link styles.css. Use CSS variables for core colors, typography, spacing, and container width. Use varied compositions and meaningful, relevant imagery; include at least three distinct images. Do not invent real business facts, metrics, reviews, credentials, contact details, or hours.

The hero photo must be edge-to-edge across the full hero region, cover-cropped behind readable overlay copy on desktop and mobile. Include a complete, visibly designed semantic footer after main content, with the business name, at least two useful links, accurate available contact/location information, and copyright. On mobile, use a keyboard-accessible three-bar hamburger with a programmatic name and state; do not show a Menu text label. Do not use arrow glyphs in CTA buttons. If the design brief names an HTML5 UP template, adapt its structure into an original business-specific design, preserve its name/source/CC BY 3.0 attribution in an HTML comment, and keep template credits out of visible page content. The final HTML response must close with </body></html>.

If the source website has a contact form, preserve its purpose and relevant fields. Ask for image choices that match the real business and section; do not invent alt-text claims. Avoid repeated generic card grids, decorative gradients/blobs without purpose, filler copy, and unsupported testimonials, statistics, awards, or credentials.

Return only the requested part and a short part label outside its code block. Do not add another code block or combine parts."""


def fail(message: str) -> None:
    raise RuntimeError(message)


def find_composer(page):
    candidates = [
        page.get_by_placeholder("Ask anything privately"),
        page.locator("[data-testid='duckai-chat-input'] textarea"),
        page.locator("textarea[placeholder*='Ask']"),
        page.locator("textarea"),
        page.locator("[contenteditable='true'][role='textbox']"),
    ]
    for locator in candidates:
        try:
            if locator.count() and locator.last.is_visible() and locator.last.is_enabled():
                return locator.last
        except Exception:
            continue
    return None


def wait_for_guest_composer(page, timeout_ms: int = 45000):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        composer = find_composer(page)
        if composer is not None:
            return composer
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=1500).lower()
        except Exception:
            pass
        gate_terms = (
            "verify you are human",
            "verify that you are human",
            "captcha",
            "sign in to continue",
            "usage limit reached",
            "temporarily unavailable",
        )
        if any(term in body for term in gate_terms):
            fail("Duck.ai guest chat is gated or unavailable. Stop without trying to bypass the gate.")
        page.wait_for_timeout(500)
    fail("Could not find Duck.ai's guest-chat composer before the timeout.")


def main_text(page) -> str:
    try:
        main = page.locator("main").first
        if main.count():
            return main.inner_text(timeout=3000)
    except Exception:
        pass
    return page.locator("body").inner_text(timeout=3000)


def preformatted_blocks(page):
    main = page.locator("main").first
    scope = main if main.count() else page.locator("body")
    return scope.locator("pre")


def message_text_for_block(block, label: str, code: str) -> str:
    # Find the nearest rendered message wrapper containing this one new code
    # block and its visible part label. Never fall back to page-wide text.
    visible_text = block.evaluate(
        """(element, expectedLabel) => {
          const normalize = value => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          for (let node = element; node && node !== document.body && node.tagName !== 'MAIN'; node = node.parentElement) {
            const text = (node.innerText || '').trim();
            if (node.querySelectorAll('pre').length === 1 && normalize(text).includes(normalize(expectedLabel))) {
              return text;
            }
          }
          return '';
        }""",
        label,
    )
    if not visible_text:
        # Duck.ai sometimes renders the required part name only in the code
        # comment, outside any shared DOM wrapper with the code block. The
        # caller has already confirmed this is the one newly added block and
        # validated its exact first-line marker, so preserve that verified label.
        marker = f"/* {label} */" if label.startswith("CSS") else f"<!-- {label} -->"
        if re.search(rf"^\s*{re.escape(marker)}\s*$", code, re.IGNORECASE | re.MULTILINE):
            return label
        return ""
    # Inner text includes rendered code, but the web UI does not retain Markdown
    # fences. Remove the one code block's visible text before rebuilding its fence.
    context_text = visible_text.replace(code, "", 1).strip()
    if re.search(rf"\b{re.escape(label)}\b", context_text, re.IGNORECASE):
        return context_text
    # The in-code marker is authoritative when Duck.ai's DOM places the part
    # label inside the code block rather than in the surrounding message text.
    marker = f"/* {label} */" if label.startswith("CSS") else f"<!-- {label} -->"
    if re.search(rf"^\s*{re.escape(marker)}\s*$", code, re.IGNORECASE | re.MULTILINE):
        return label
    return ""


def send_and_capture(page, prompt: str, kind: str, part: int) -> tuple[str, str]:
    label = f"{kind.upper()} PART {part} OF 3"
    label_pattern = re.compile(rf"\b{kind}\s+part\s+{part}\s+of\s+3\b", re.IGNORECASE)
    composer = wait_for_guest_composer(page)
    before_text = main_text(page)
    before_label_count = len(label_pattern.findall(before_text))
    before_blocks = preformatted_blocks(page).count()

    composer.fill(prompt, timeout=15000)
    send_candidates = (
        page.get_by_role("button", name=re.compile(r"send", re.IGNORECASE)),
        page.locator("button[aria-label*='send' i]"),
        page.locator("button[data-testid*='send' i]"),
        page.locator("form button[type='submit']"),
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
            fail("Duck.ai's composer was found, but neither a Send control nor Enter could submit the prompt.")

    deadline = time.monotonic() + 240
    last_code = ""
    stable_samples = 0
    while time.monotonic() < deadline:
        blocks = preformatted_blocks(page)
        current_text = main_text(page)
        label_count = len(label_pattern.findall(current_text))
        block_count = blocks.count()
        if label_count > before_label_count and block_count == before_blocks + 1:
            code = blocks.nth(block_count - 1).inner_text().strip("\r\n")
            has_marker = bool(re.search(rf"^\s*{re.escape('/* CSS PART ' + str(part) + ' OF 3 */' if kind == 'css' else '<!-- HTML PART ' + str(part) + ' OF 3 -->')}\s*$", code, re.IGNORECASE | re.MULTILINE))
            if code == last_code and has_marker:
                stable_samples += 1
            else:
                stable_samples = 0
            last_code = code
            stop_button = page.get_by_role("button", name=re.compile(r"stop", re.IGNORECASE)).last
            stop_visible = False
            try:
                stop_visible = stop_button.count() > 0 and stop_button.is_visible()
            except Exception:
                pass
            if stable_samples >= 3 and not stop_visible:
                response_context = message_text_for_block(blocks.nth(block_count - 1), label, code)
                if not response_context:
                    fail(f"Could not scope the new code block to the newest {label} assistant message.")
                # Duck.ai renders Markdown without exposing its original backticks
                # in DOM text. Preserve the message-scoped visible label/text and
                # wrap only that message's single rendered code block for capture.
                fence = "css" if kind == "css" else "text"
                response = f"{response_context}\n```{fence}\n{code}\n```\n"
                chat_url = page.url
                if not chat_url or urlparse(chat_url).hostname != "duck.ai":
                    fail(f"Unexpected Duck.ai conversation URL: {chat_url!r}")
                return response, chat_url
        elif block_count > before_blocks + 1:
            fail(f"Duck.ai returned multiple new code blocks for {label}; refusing ambiguous capture.")
        page.wait_for_timeout(1000)
    fail(f"Timed out waiting for a complete, stable {label} response with exactly one code block.")


def send_acknowledgment_and_wait(page, expected_chat_url: str) -> None:
    """Send the required human-style handoff turn and wait for Duck.ai to finish it."""
    acknowledgment = "acknowledged"
    before_text = main_text(page)
    before_ack_count = len(re.findall(r"\backnowledged\b", before_text, re.IGNORECASE))
    before_assistant_count = len(re.findall(r"Duck\.ai said", before_text, re.IGNORECASE))
    composer = wait_for_guest_composer(page)
    composer.fill(acknowledgment, timeout=15000)
    send_candidates = (
        page.get_by_role("button", name=re.compile(r"send", re.IGNORECASE)),
        page.locator("button[aria-label*='send' i]"),
        page.locator("button[data-testid*='send' i]"),
        page.locator("form button[type='submit']"),
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
            fail("Could not submit the standalone acknowledged turn.")

    deadline = time.monotonic() + 180
    previous_text = ""
    stable_samples = 0
    while time.monotonic() < deadline:
        text = main_text(page)
        ack_count = len(re.findall(r"\backnowledged\b", text, re.IGNORECASE))
        assistant_count = len(re.findall(r"Duck\.ai said", text, re.IGNORECASE))
        stop = page.get_by_role("button", name=re.compile(r"stop", re.IGNORECASE)).last
        try:
            stop_visible = stop.count() > 0 and stop.is_visible()
        except Exception:
            stop_visible = False
        if ack_count > before_ack_count and assistant_count > before_assistant_count and text == previous_text and not stop_visible:
            stable_samples += 1
        else:
            stable_samples = 0
        previous_text = text
        if stable_samples >= 3:
            if page.url != expected_chat_url:
                fail("Duck.ai conversation URL changed after the acknowledged turn.")
            print("Sent acknowledged as a standalone Duck.ai turn and waited for its response.")
            return
        page.wait_for_timeout(1000)
    fail("Timed out waiting for Duck.ai to finish the standalone acknowledged turn.")


def capture_chunk(kind: str, part: int, chat_url: str, response: str) -> None:
    response_path = RUN_DIR / "chunks" / "responses" / f"{kind}-{part}.txt"
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(response, encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(CAPTURE_SCRIPT),
            str(RUN_DIR),
            str(response_path),
            "--kind",
            kind,
            "--part",
            str(part),
            "--chat-id",
            chat_url,
        ],
        check=True,
        cwd=REPO,
    )


def build_initial_prompt(website_request: str, design_brief: str) -> str:
    return (
        f"Website request:\n{website_request.strip()}\n\n"
        f"Verified facts and design brief:\n{design_brief.strip()}\n\n"
        f"{BASE_CONTRACT}\n\n"
        "Begin with CSS PART 1 OF 3 only."
    )


def build_next_prompt(kind: str, part: int) -> str:
    label = f"{kind.upper()} PART {part} OF 3"
    return (
        f"Continue the same website and conversation. Return {label} only, with exactly one complete "
        f"{'css' if kind == 'css' else 'text'} fenced code block and the required in-code marker. "
        "Preserve compatibility with the previous parts and do not repeat earlier chunks."
    )


def capture_previews() -> None:
    html_file = RUN_DIR / "files" / "index.html"
    screenshot_dir = RUN_DIR / "debug" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for name, width, height in (("desktop", 1440, 900), ("mobile", 390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(html_file.as_uri(), wait_until="load", timeout=45000)
                page.wait_for_timeout(1500)
                page.screenshot(path=str(screenshot_dir / f"{name}.png"), full_page=False)
                page.close()
        finally:
            browser.close()


def run_checker(website_request: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), str(RUN_DIR), website_request],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    (RUN_DIR / "debug").mkdir(parents=True, exist_ok=True)
    report_path = RUN_DIR / "debug" / "checker_report.json"
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {"final_success": False, "checker_exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    report["checker_exit_code"] = result.returncode
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def run() -> None:
    website_request = os.environ.get("WEBSITE_REQUEST", "").strip()
    design_brief = os.environ.get("DESIGN_BRIEF", "").strip()
    if not website_request and (ROOT / "website_request.txt").is_file():
        website_request = (ROOT / "website_request.txt").read_text(encoding="utf-8").strip()
    if not design_brief and (ROOT / "design_brief.txt").is_file():
        design_brief = (ROOT / "design_brief.txt").read_text(encoding="utf-8").strip()
    if not website_request:
        fail("WEBSITE_REQUEST is required.")
    if not design_brief:
        fail("DESIGN_BRIEF is required. Include verified facts and the named template/source/license, if using HTML5 UP.")

    RUN_DIR.mkdir(parents=True, exist_ok=False)
    (RUN_DIR / "debug").mkdir(parents=True)
    turn_log = {"chat_url": "", "turns": []}
    with tempfile.TemporaryDirectory(prefix="duckai-website-profile-") as profile_dir:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1440, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            chat_url = ""
            try:
                page.goto("https://duck.ai/", wait_until="domcontentloaded", timeout=60000)
                wait_for_guest_composer(page)
                for kind, part in [("css", 1), ("css", 2), ("css", 3), ("html", 1), ("html", 2), ("html", 3)]:
                    if kind == "html" and part == 1:
                        send_acknowledgment_and_wait(page, chat_url)
                        turn_log["turns"].append({"type": "user_acknowledgment", "text": "acknowledged", "assistant_response_completed": True})
                    prompt = build_initial_prompt(website_request, design_brief) if not chat_url else build_next_prompt(kind, part)
                    response, current_url = send_and_capture(page, prompt, kind, part)
                    if chat_url and current_url != chat_url:
                        fail("Duck.ai conversation URL changed during capture; refusing to mix chats.")
                    chat_url = current_url
                    turn_log["chat_url"] = chat_url
                    capture_chunk(kind, part, chat_url, response)
                    turn_log["turns"].append({"type": "captured_chunk", "kind": kind, "part": part})
                    print(f"Captured {kind.upper()} PART {part} OF 3")
            finally:
                context.close()

    subprocess.run([sys.executable, str(ASSEMBLE_SCRIPT), str(RUN_DIR)], check=True, cwd=REPO)
    capture_previews()
    report = run_checker(website_request)
    notes = {
        "candidate_index": int(CANDIDATE_INDEX),
        "status": "generated_pending_human_review",
        "chat_url": chat_url,
        "final_success": bool(report.get("final_success")),
        "visual_review_status": report.get("visual_review_status", "pending"),
        "note": "Review the rendered images and desktop/mobile screenshots before treating this candidate as visually approved.",
    }
    (RUN_DIR / "run_notes.json").write_text(json.dumps(notes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (RUN_DIR / "conversation_turns.json").write_text(json.dumps(turn_log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", ""))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with summary.open("a", encoding="utf-8") as handle:
            handle.write(f"## Duck.ai website candidate {CANDIDATE_INDEX} of 3\n\n")
            handle.write(f"- Output: `{RUN_DIR.relative_to(REPO).as_posix()}/`\n")
            handle.write(f"- Structural/checker success: `{str(bool(report.get('final_success'))).lower()}`\n")
            handle.write("- Visual review: pending human screenshot and image relevance review.\n")
            handle.write("- This candidate folder will be committed with the other completed candidates.\n")
    print(f"Run saved in {RUN_DIR}")


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"Duck.ai automation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
