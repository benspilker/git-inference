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
import traceback
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
if not CANDIDATE_INDEX.isdigit() or not 1 <= int(CANDIDATE_INDEX) <= 6:
    raise RuntimeError("CANDIDATE_INDEX must be between 1 and 6.")
RUN_DIR = REPO / "duckai-runs" / f"run-{RUN_ID}-{RUN_ATTEMPT}-candidate-{CANDIDATE_INDEX}"
CAPTURE_SCRIPT = ROOT / "scripts" / "capture_chunk.py"
ASSEMBLE_SCRIPT = ROOT / "scripts" / "assemble_chunks.py"
INLINE_SCRIPT = ROOT / "scripts" / "inline_css.py"
BROWSER_QA_SCRIPT = ROOT / "scripts" / "browser_qa.py"
CHECK_SCRIPT = ROOT / "scripts" / "check_website.py"

BASE_CONTRACT = """Follow the supplied facts and design brief. Build a polished, responsive, business-specific website. Use exactly three CSS responses followed by exactly three HTML responses, one complete code block per response. CSS responses use a css fence; HTML responses use a text fence containing raw HTML. Each response must state its exact part label. The first nonblank line inside its code block must be exactly `/* CSS PART n OF 3 */` or `<!-- HTML PART n OF 3 -->` for that part. Keep the CSS and HTML coherent and link styles.css. Use CSS variables for core colors, typography, spacing, and container width. Include at least three distinct image files, represented by at least three unique image URLs and at least three HTML `<img>` elements with accurate alt text. Do not reuse the same photo with different crop parameters to meet the count. Use varied compositions and meaningful, relevant imagery. Do not invent real business facts, metrics, reviews, credentials, contact details, or hours.

The hero photo must be edge-to-edge across the full hero region, cover-cropped behind readable overlay copy on desktop and mobile. Keep the primary CTA visible in the first viewport on desktop and mobile; use a click-to-call link when a verified phone number is available, otherwise provide a safe contact action. Include every requested section. If hours are requested but the source is unclear or conflicting, include an “Hours & availability” section that asks visitors to call to confirm, without stating unsupported times. Include a complete, visibly designed semantic footer after main content, with the business name, at least two useful links, accurate available contact/location information, and copyright. On mobile, use a keyboard-accessible three-bar hamburger with a programmatic name and state; do not show a Menu text label. Do not use arrow glyphs in CTA buttons. If the design brief names an HTML5 UP template, adapt its structure into an original business-specific design, preserve its name/source/CC BY 3.0 attribution in an HTML comment, and keep template credits out of visible page content. The final HTML response must close with </body></html>.

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


def enable_web_search(page) -> None:
    """Enable Duck.ai web search for the source assessment and verify the toggle."""
    tools_button = page.get_by_role("button", name=re.compile(r"tools", re.IGNORECASE)).last
    if not tools_button.count() or not tools_button.is_visible():
        fail("Duck.ai's Tools control is unavailable; cannot assess the source website in-chat.")
    tools_button.click(timeout=5000)
    page.get_by_role("menu").wait_for(state="visible", timeout=5000)

    search_option = page.get_by_role("menuitemradio", name=re.compile(r"web\s*search", re.IGNORECASE)).last
    if not search_option.count():
        search_option = page.get_by_role("menuitemcheckbox", name=re.compile(r"web\s*search", re.IGNORECASE)).last
    if not search_option.count():
        search_option = page.get_by_role("checkbox", name=re.compile(r"web\s*search", re.IGNORECASE)).last
    if not search_option.count():
        search_option = page.get_by_role("switch", name=re.compile(r"web\s*search", re.IGNORECASE)).last
    if not search_option.count() or not search_option.is_visible():
        fail("Duck.ai's Tools menu did not expose Web Search; cannot verify source-site access.")

    checked = (search_option.get_attribute("aria-checked") or "").lower() == "true"
    pressed = (search_option.get_attribute("aria-pressed") or "").lower() == "true"
    if not (checked or pressed):
        search_option.click(timeout=5000)
        page.wait_for_timeout(300)
        if not search_option.count() or not search_option.is_visible():
            # Selecting a tool closes the popover in the current Duck.ai UI.
            tools_button.click(timeout=5000)
            page.get_by_role("menu").wait_for(state="visible", timeout=5000)
            search_option = page.get_by_role(
                "menuitemradio", name=re.compile(r"web\s*search", re.IGNORECASE)
            ).last
            if not search_option.count():
                search_option = page.get_by_role(
                    "menuitemcheckbox", name=re.compile(r"web\s*search", re.IGNORECASE)
                ).last
            if not search_option.count():
                search_option = page.get_by_role(
                    "checkbox", name=re.compile(r"web\s*search", re.IGNORECASE)
                ).last
            if not search_option.count():
                search_option = page.get_by_role(
                    "switch", name=re.compile(r"web\s*search", re.IGNORECASE)
                ).last
            if not search_option.count() or not search_option.is_visible():
                fail("Duck.ai closed its Tools menu after selection and did not reopen Web Search.")
            page.wait_for_timeout(200)
    checked = (search_option.get_attribute("aria-checked") or "").lower() == "true"
    pressed = (search_option.get_attribute("aria-pressed") or "").lower() == "true"
    if not (checked or pressed):
        # Some menu versions expose a checked child input rather than the state
        # on the option itself. Require an observable selected state either way.
        selected = search_option.locator("input[type='checkbox']")
        if not selected.count() or not selected.last.is_checked():
            fail("Could not verify that Duck.ai Web Search is enabled.")
    print("Enabled Duck.ai Web Search for the source-site assessment.")


def send_source_assessment(page, source_url: str) -> tuple[str, str, dict]:
    """Ask Duck.ai to inspect the live site and capture a structured, sourced audit."""
    label = "SOURCE SITE AUDIT"
    label_pattern = re.compile(r"\bSOURCE\s+SITE\s+AUDIT\b", re.IGNORECASE)
    prompt = f"""Inspect the public website at {source_url} using Web Search. This is a source assessment only; do not write redesign code yet.

Review the homepage and relevant pages linked from its navigation, especially services, products/parts, about, and contact pages when present. Return one JSON object in exactly one `json` code block, preceded by the exact label `{label}`. Include:
- `access_status`: `accessed` only if you actually retrieved and reviewed the requested website; otherwise `unavailable`.
- `pages_reviewed`: an array of objects with `url`, `page_title`, and a short description of visible content.
- `business_facts`: concise, neutral facts visible on those pages; do not copy paragraphs verbatim.
- `audience_and_goals`: likely audience and primary contact/conversion path, separating evidence from inference.
- `brand_and_visual_observations`: observations about the existing design and imagery, not instructions to copy them.
- `content_structure`: the important sections/pages a redesign should retain.
- `unknown_or_verify`: details that are unclear, inconsistent, or should be confirmed with the business.
- `design_reference`: recommend one specific HTML5 UP template only if you can inspect its official demo; include `name`, `url`, `license_url`, and `patterns_to_adapt`. If you cannot verify a suitable template and its license, use `null`.
- `sources`: exact source URLs you actually reviewed, including the requested domain.

Do not invent facts, claim a page was reviewed when it was not, reproduce source copy, or reuse protected brand assets. Preserve observed facts separately from design opinions. Keep the audit concise enough to guide a one-page redesign."""

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
        composer.press("Enter", timeout=5000)

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
            try:
                json.loads(code)
                valid_json = True
            except json.JSONDecodeError:
                valid_json = False
            if code == last_code and valid_json:
                stable_samples += 1
            else:
                stable_samples = 0
            last_code = code
            stop_button = page.get_by_role("button", name=re.compile(r"stop", re.IGNORECASE)).last
            try:
                stop_visible = stop_button.count() > 0 and stop_button.is_visible()
            except Exception:
                stop_visible = False
            if stable_samples >= 3 and not stop_visible:
                response_context = message_text_for_block(blocks.nth(block_count - 1), label, code)
                if not response_context:
                    fail("Could not scope the source assessment to the newest assistant response.")
                response = f"{response_context}\n```json\n{code}\n```\n"
                chat_url = page.url
                if not chat_url or urlparse(chat_url).hostname != "duck.ai":
                    fail(f"Unexpected Duck.ai conversation URL: {chat_url!r}")
                assessment = json.loads(code)
                return response, chat_url, assessment
        elif block_count > before_blocks + 1:
            fail("Duck.ai returned multiple new code blocks for the source assessment.")
        page.wait_for_timeout(1000)
    fail("Timed out waiting for a structured, stable source-site assessment.")


def validate_source_assessment(assessment: dict, source_url: str) -> None:
    if assessment.get("access_status") != "accessed":
        fail("Duck.ai could not confirm it accessed the source site; stopping before code generation.")
    requested = urlparse(source_url)
    requested_host = (requested.hostname or "").lower().removeprefix("www.")
    reviewed_hosts = {
        (urlparse(str(item.get("url", ""))).hostname or "").lower().removeprefix("www.")
        for item in assessment.get("pages_reviewed", [])
        if isinstance(item, dict)
    }
    source_hosts = {
        (urlparse(url).hostname or "").lower().removeprefix("www.")
        for url in assessment.get("sources", [])
        if isinstance(url, str)
    }
    all_hosts = reviewed_hosts | source_hosts
    if not any(host == requested_host or host.endswith("." + requested_host) for host in all_hosts):
        fail("Duck.ai's assessment did not cite or list a reviewed page on the requested source domain.")
    if not assessment.get("pages_reviewed"):
        fail("Duck.ai's source assessment did not list any reviewed pages.")
    reference = assessment.get("design_reference")
    if not isinstance(reference, dict) or not all(reference.get(key) for key in ("name", "url", "license_url")):
        fail("Duck.ai did not provide a verifiable named design reference and license; stopping before code generation.")
    if not reference.get("patterns_to_adapt"):
        fail("Duck.ai's design reference did not identify any template patterns to adapt.")
    template_url = urlparse(reference["url"])
    license_url = urlparse(reference["license_url"])
    if (template_url.hostname or "").lower() not in {"html5up.net", "www.html5up.net"}:
        fail("The proposed design reference is not hosted on the official HTML5 UP domain.")
    license_host = (license_url.hostname or "").lower()
    is_html5up_license = license_host in {"html5up.net", "www.html5up.net"} and license_url.path.rstrip("/") == "/license"
    is_creative_commons_license = (
        license_host == "creativecommons.org" and "/licenses/by/3.0" in license_url.path
    )
    if not (is_html5up_license or is_creative_commons_license):
        fail("The proposed design reference did not provide an official HTML5 UP or CC BY 3.0 license URL.")


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


def build_initial_prompt(
    website_request: str,
    design_brief: str,
    source_url: str = "",
    design_reference: dict | None = None,
) -> str:
    source_context = (
        f"The source website {source_url} was assessed earlier in this same conversation. "
        "Use that captured assessment as your factual reference; do not ask the user to repeat it. "
        "Preserve verified content and the original business's conversion goals, but write original copy. "
        "Do not reuse the source site's copy, logo, or photos; use appropriately licensed, relevant imagery.\n\n"
        if source_url
        else ""
    )
    template_context = ""
    if design_reference:
        template_context = (
            "Use this verified HTML5 UP design reference selected after the source assessment: "
            f"{design_reference['name']} ({design_reference['url']}); license: {design_reference['license_url']}. "
            f"Adapt these patterns to the business: {json.dumps(design_reference['patterns_to_adapt'], ensure_ascii=False)}. "
            "Do not copy template branding or unrelated template copy. Include the template name, source URL, "
            "CC BY 3.0 license URL, and adaptation note in a non-rendering HTML source comment.\n\n"
        )
    return (
        f"Website request:\n{website_request.strip()}\n\n"
        f"Design guidance:\n{design_brief.strip()}\n\n"
        f"{source_context}"
        f"{template_context}"
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


def run_browser_qa(website_request: str) -> None:
    html_file = RUN_DIR / "files" / "index.html"
    css_file = RUN_DIR / "files" / "styles.css"
    inlined_file = RUN_DIR / "files" / "inlined" / "index.html"
    subprocess.run(
        [sys.executable, str(INLINE_SCRIPT), str(html_file), str(css_file), str(inlined_file)],
        check=True,
        cwd=REPO,
    )
    subprocess.run(
        [sys.executable, str(BROWSER_QA_SCRIPT), str(RUN_DIR), website_request],
        check=True,
        cwd=REPO,
    )


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
    source_url = os.environ.get("SOURCE_URL", "").strip()
    if not website_request and (ROOT / "website_request.txt").is_file():
        website_request = (ROOT / "website_request.txt").read_text(encoding="utf-8").strip()
    if not design_brief and (ROOT / "design_brief.txt").is_file():
        design_brief = (ROOT / "design_brief.txt").read_text(encoding="utf-8").strip()
    if not source_url and (ROOT / "source_url.txt").is_file():
        source_url = (ROOT / "source_url.txt").read_text(encoding="utf-8").strip()
    if not website_request:
        fail("WEBSITE_REQUEST is required.")
    if not design_brief:
        fail("DESIGN_BRIEF is required. Include verified facts and the named template/source/license, if using HTML5 UP.")
    if source_url:
        parsed_source = urlparse(source_url)
        if parsed_source.scheme not in {"http", "https"} or not parsed_source.hostname:
            fail("SOURCE_URL must be an absolute http(s) URL.")
    else:
        match = re.search(r"https?://[^\s)]+", website_request)
        if match:
            source_url = match.group(0).rstrip(".,;]")

    RUN_DIR.mkdir(parents=True, exist_ok=False)
    (RUN_DIR / "debug").mkdir(parents=True)
    (RUN_DIR / "debug" / "inputs.json").write_text(
        json.dumps(
            {"website_request": website_request, "design_brief": design_brief, "source_url": source_url},
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
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
                if source_url:
                    enable_web_search(page)
                    response, current_url, assessment = send_source_assessment(page, source_url)
                    if chat_url and current_url != chat_url:
                        fail("Duck.ai conversation URL changed during source assessment.")
                    chat_url = current_url
                    (RUN_DIR / "debug" / "source_assessment.json").write_text(
                        json.dumps(assessment, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                    )
                    (RUN_DIR / "debug" / "source_assessment.md").write_text(response, encoding="utf-8")
                    turn_log["chat_url"] = chat_url
                    turn_log["turns"].append(
                        {"type": "source_site_assessment", "source_url": source_url, "assistant_response_completed": True}
                    )
                    validate_source_assessment(assessment, source_url)
                    print(f"Captured and validated Duck.ai's source assessment for {source_url}")
                for kind, part in [("css", 1), ("css", 2), ("css", 3), ("html", 1), ("html", 2), ("html", 3)]:
                    if kind == "html" and part == 1:
                        send_acknowledgment_and_wait(page, chat_url)
                        turn_log["turns"].append({"type": "user_acknowledgment", "text": "acknowledged", "assistant_response_completed": True})
                    prompt = (
                        build_initial_prompt(
                            website_request,
                            design_brief,
                            source_url,
                            json.loads((RUN_DIR / "debug" / "source_assessment.json").read_text(encoding="utf-8")).get("design_reference")
                            if source_url
                            else None,
                        )
                        if not turn_log["turns"] or turn_log["turns"][-1].get("type") == "source_site_assessment"
                        else build_next_prompt(kind, part)
                    )
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
    run_browser_qa(website_request)
    report = run_checker(website_request)
    notes = {
        "candidate_index": int(CANDIDATE_INDEX),
        "automated_success": bool(report.get("automated_success")),
        "status": (
            "automated_checks_passed_pending_human_review"
            if report.get("automated_success")
            else "automated_checks_failed"
        ),
        "chat_url": chat_url,
        "source_url": source_url or None,
        "source_assessment_status": "pass" if source_url else "not_requested",
        "design_reference": (
            json.loads((RUN_DIR / "debug" / "source_assessment.json").read_text(encoding="utf-8")).get("design_reference")
            if source_url
            else None
        ),
        "final_success": bool(report.get("final_success")),
        "browser_qa_status": report.get("browser_qa_status", "missing"),
        "visual_review_status": report.get("visual_review_status", "pending"),
        "note": "Review the rendered images and desktop/mobile screenshots before treating this candidate as visually approved.",
    }
    (RUN_DIR / "run_notes.json").write_text(json.dumps(notes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (RUN_DIR / "conversation_turns.json").write_text(json.dumps(turn_log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", ""))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with summary.open("a", encoding="utf-8") as handle:
            handle.write(f"## Duck.ai website candidate attempt {CANDIDATE_INDEX}\n\n")
            handle.write(f"- Output: `{RUN_DIR.relative_to(REPO).as_posix()}/`\n")
            handle.write(f"- Automated checks passed: `{str(bool(report.get('automated_success'))).lower()}`\n")
            handle.write(f"- Browser QA: `{report.get('browser_qa_status', 'missing')}`\n")
            if source_url:
                handle.write(f"- Source assessment: completed in this chat for `{source_url}`; evidence saved in `debug/source_assessment.json`.\n")
                handle.write("- Design reference: recorded in the source assessment, with its license.\n")
            handle.write("- Visual review: pending human screenshot and image relevance review.\n")
            handle.write("- This candidate folder will be committed with the candidate batch.\n")
    print(f"Run saved in {RUN_DIR}")


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        message = f"Duck.ai automation failed during candidate attempt {CANDIDATE_INDEX}: {error}"
        print(message, file=sys.stderr)
        if RUN_DIR.exists():
            (RUN_DIR / "debug").mkdir(parents=True, exist_ok=True)
            (RUN_DIR / "debug" / "failure.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with Path(summary_path).open("a", encoding="utf-8") as handle:
                handle.write(f"\n### Candidate attempt {CANDIDATE_INDEX} failed\n\n{message}\n")
        raise SystemExit(1)
