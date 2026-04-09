from __future__ import annotations

import re
import time


def first_visible_locator(page, selectors: list[str], timeout_ms: int):
    per_selector_timeout = min(700, max(250, int(timeout_ms / max(1, len(selectors)))))
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=per_selector_timeout)
            return locator
        except Exception:
            continue
    return None


def _body_text_lower(page) -> str:
    try:
        return (page.inner_text("body") or "").strip().lower()
    except Exception:
        return ""


def page_indicates_transient_error(page) -> bool:
    text = _body_text_lower(page)
    if not text:
        return False
    markers = (
        "502. that’s an error",
        "502. that's an error",
        "server encountered a temporary error",
        "please try again in 30 seconds",
        "something went wrong",
        "unusual traffic",
    )
    return any(marker in text for marker in markers)


def dismiss_blocking_modals(page) -> None:
    close_selectors = [
        "button[aria-label='Close']",
        "button:has-text('Close')",
        "button:has-text('Got it')",
        "button:has-text('Continue')",
        "button:has-text('Dismiss')",
    ]
    for close_selector in close_selectors:
        try:
            btn = page.locator(close_selector).first
            if btn.count() > 0 and btn.is_visible():
                try:
                    btn.click(timeout=1500)
                except Exception:
                    handle = btn.element_handle()
                    if handle is not None:
                        handle.evaluate("(el) => el.click()")
                page.wait_for_timeout(250)
        except Exception:
            continue


def find_chat_composer(page, timeout_ms: int):
    selectors = [
        "rich-textarea .ql-editor[role='textbox'][aria-label*='prompt']",
        "rich-textarea .ql-editor[role='textbox'][aria-label*='Gemini']",
        ".ql-editor[role='textbox'][aria-label*='prompt']",
        ".ql-editor[role='textbox'][aria-label*='Gemini']",
        "textarea[aria-label*='Enter a prompt']",
        "textarea[placeholder*='Enter a prompt']",
        "textarea[aria-label*='Ask Gemini']",
        "textarea[placeholder*='Ask Gemini']",
        "main textarea",
        "textarea",
        "[contenteditable='true'][aria-label*='prompt'][role='textbox']",
        "[contenteditable='true'][aria-label*='Ask Gemini'][role='textbox']",
        "[contenteditable='true'][role='textbox']",
    ]
    deadline = time.time() + max(2.0, timeout_ms / 1000.0)
    last_reload_at = 0.0
    while time.time() < deadline:
        dismiss_blocking_modals(page)
        composer = first_visible_locator(page, selectors, timeout_ms=min(2500, max(1000, timeout_ms)))
        if composer is not None:
            try:
                if composer.is_enabled():
                    return composer
            except Exception:
                return composer

        if page_indicates_transient_error(page):
            now = time.time()
            if now - last_reload_at >= 6:
                page.wait_for_timeout(1500)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception:
                    pass
                last_reload_at = time.time()
                continue

        page.wait_for_timeout(350)
    return None


def assistant_turns(page):
    selectors = [
        "main model-response message-content",
        "main model-response",
        "main response-container message-content",
        "main response-container",
        "main message-content",
        "main [class*='response-content']",
        "main [class*='markdown']",
        "main article",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    return page.locator("main article")


def extract_response_text(assistant_messages, before_count: int, before_last_text: str) -> str:
    def _normalize(text: str) -> str:
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^\s*gemini\s+said:\s*\n+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*gemini\s*:\s*\n+", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    after_count = assistant_messages.count()
    if after_count == 0:
        return ""
    if after_count > before_count:
        return _normalize(assistant_messages.nth(after_count - 1).inner_text())
    text = _normalize(assistant_messages.last.inner_text())
    if text and text != before_last_text:
        return text
    return ""


def looks_like_non_answer_text(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    blocked_markers = [
        "new session started",
        "internal automation issue",
        "please send your message again",
        "sign in to continue",
    ]
    return any(marker in lowered for marker in blocked_markers)


def _is_contenteditable(composer) -> bool:
    try:
        handle = composer.element_handle()
        if handle is None:
            return False
        return bool(handle.evaluate("(el) => el.getAttribute('contenteditable') === 'true' || el.isContentEditable"))
    except Exception:
        return False


def _read_composer_text(composer) -> str:
    handle = composer.element_handle()
    if handle is None:
        return ""
    try:
        return str(
            handle.evaluate(
                """(el) => {
                    const isEditable = el.getAttribute('contenteditable') === 'true' || el.isContentEditable;
                    if (isEditable) return (el.innerText || el.textContent || '').trim();
                    return (el.value || '').trim();
                }"""
            )
        ).strip()
    except Exception:
        return ""


def set_composer_text(page, composer, text: str) -> None:
    handle = composer.element_handle()
    if handle is None:
        raise RuntimeError("Failed to resolve composer element.")

    if _is_contenteditable(composer):
        try:
            composer.press("Control+A")
            composer.press("Backspace")
        except Exception:
            pass
        lines = text.split("\n")
        for idx, line in enumerate(lines):
            if line:
                page.keyboard.type(line, delay=6)
            if idx < len(lines) - 1:
                page.keyboard.press("Shift+Enter")
        if _read_composer_text(composer):
            return
        try:
            handle.evaluate(
                """(el, value) => {
                    el.focus();
                    el.textContent = '';
                    const lines = value.split('\\n');
                    for (let i = 0; i < lines.length; i++) {
                        if (i > 0) el.appendChild(document.createElement('br'));
                        el.appendChild(document.createTextNode(lines[i]));
                    }
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                text,
            )
        except Exception:
            pass
        return

    try:
        composer.fill(text)
        return
    except Exception:
        pass

    handle.evaluate(
        """(el, value) => {
            el.focus();
            el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        text,
    )


def send_prompt(page, composer, prompt_text: str) -> None:
    dismiss_blocking_modals(page)
    try:
        composer.click(timeout=3000)
    except Exception:
        composer.click(force=True, timeout=3000)
    set_composer_text(page, composer, prompt_text)
    send_selectors = [
        "button.send-button.submit",
        "button[aria-label='Send message']",
        "button[aria-label*='Send message']",
        "button[aria-label*='Send']",
        "button[data-test-id*='send']",
        "button[type='submit']",
    ]
    deadline = time.time() + 18
    while time.time() < deadline:
        for selector in send_selectors:
            try:
                buttons = page.locator(selector)
                count = min(buttons.count(), 8)
            except Exception:
                continue
            for i in range(count):
                btn = buttons.nth(i)
                try:
                    if not btn.is_visible() or not btn.is_enabled():
                        continue
                    btn.click(timeout=2200)
                    page.wait_for_timeout(450)
                    if not _read_composer_text(composer):
                        return
                    try:
                        handle = btn.element_handle()
                        if handle is not None:
                            handle.evaluate("(el) => el.click()")
                            page.wait_for_timeout(450)
                            if not _read_composer_text(composer):
                                return
                    except Exception:
                        pass
                    continue
                except Exception:
                    continue
        page.wait_for_timeout(300)
    for combo in ("Control+Enter", "Meta+Enter", "Enter"):
        try:
            composer.press(combo)
            page.wait_for_timeout(450)
            if not _read_composer_text(composer):
                return
        except Exception:
            continue
    remaining = _read_composer_text(composer)
    if remaining:
        raise RuntimeError("Failed to submit prompt in Gemini composer.")


def wait_for_valid_response(
    page,
    assistant_messages,
    before_count: int,
    before_last_text: str,
    wait_seconds: int,
) -> str:
    deadline = time.time() + max(1, wait_seconds)
    while time.time() < deadline:
        text = extract_response_text(assistant_messages, before_count, before_last_text)
        lowered = text.lower()
        if text and "something went wrong" not in lowered and not looks_like_non_answer_text(text):
            return text
        page.wait_for_timeout(1000)
    return ""


def stabilize_response(
    page,
    assistant_messages,
    post_response_wait_seconds: int,
    response_settle_seconds: int,
    max_settle_wait_seconds: int,
) -> str:
    if post_response_wait_seconds > 0:
        page.wait_for_timeout(post_response_wait_seconds * 1000)
    stable_for = 0
    waited = 0
    previous = ""
    while waited < max_settle_wait_seconds:
        stop_visible = False
        try:
            stop_visible = page.locator(
                "button:has-text('Stop generating'), button:has-text('Stop'), button[aria-label*='Stop']"
            ).first.is_visible()
        except Exception:
            pass
        try:
            current = assistant_messages.last.inner_text().strip()
        except Exception:
            current = ""
        if current and current == previous and not stop_visible:
            stable_for += 1
        else:
            stable_for = 0
        previous = current
        if stable_for >= max(1, response_settle_seconds):
            break
        page.wait_for_timeout(1000)
        waited += 1
    return previous
