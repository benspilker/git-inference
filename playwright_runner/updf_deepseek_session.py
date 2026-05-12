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


def dismiss_blocking_modals(page) -> None:
    close_selectors = [
        "button:has-text('Close')",
        "button:has-text('Dismiss')",
        "button:has-text('Cancel')",
        "button:has-text('Skip')",
        "button[aria-label='Close']",
        ".el-message-box__btns button:has-text('Cancel')",
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
        "textarea.el-textarea__inner",
        ".chat-editor textarea",
        ".chat-input textarea",
        "textarea[placeholder*='Ask']",
        "textarea",
    ]
    deadline = time.time() + max(1.0, timeout_ms / 1000.0)
    while time.time() < deadline:
        dismiss_blocking_modals(page)
        composer = first_visible_locator(page, selectors, timeout_ms=min(2500, max(1000, timeout_ms)))
        if composer is not None:
            try:
                if composer.is_enabled():
                    return composer
            except Exception:
                return composer
        page.wait_for_timeout(350)
    return None


def assistant_turns(page):
    selectors = [
        ".chat-message-row.from-assistant .chat-message-content",
        ".chat-message-row.from-assistant .chat-message-md",
        ".chat-message-row.from-assistant",
        ".chat-message-list .from-assistant",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    return page.locator(".chat-message-row.from-assistant")


def extract_response_text(assistant_messages, before_count: int, before_last_text: str) -> str:
    def _normalize(text: str) -> str:
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^\s*updf ai\s*:\s*\n+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*ai\s*:\s*\n+", "", cleaned, flags=re.IGNORECASE)
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
        "loading...",
        "internal automation issue",
        "please send your message again",
        "sign in to continue",
        "log in to continue",
        "network error",
        "something went wrong",
    ]
    return any(marker in lowered for marker in blocked_markers)


def _read_composer_text(composer) -> str:
    handle = composer.element_handle()
    if handle is None:
        return ""
    try:
        return str(handle.evaluate("(el) => (el.value || el.innerText || el.textContent || '').trim()")).strip()
    except Exception:
        return ""


def set_composer_text(composer, text: str) -> None:
    handle = composer.element_handle()
    if handle is None:
        raise RuntimeError("Failed to resolve composer element.")
    try:
        composer.fill(text)
        return
    except Exception:
        pass

    handle.evaluate(
        """(el, value) => {
            el.focus();
            if ('value' in el) {
                el.value = value;
            } else {
                el.textContent = value;
            }
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
    set_composer_text(composer, prompt_text)
    send_selectors = [
        ".send-button",
        ".send-button-wrapper .send-button",
        ".icon-send",
        ".chat-editor .send-button",
        ".chat-input .send-button",
        "button:has(.icon-send)",
    ]
    deadline = time.time() + 15
    while time.time() < deadline:
        for selector in send_selectors:
            btn = page.locator(selector).first
            try:
                if btn.count() == 0 or not btn.is_visible():
                    continue
                try:
                    if not btn.is_enabled():
                        continue
                except Exception:
                    pass
                btn.click(timeout=2000)
                page.wait_for_timeout(450)
                if not _read_composer_text(composer):
                    return
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
        try:
            current = assistant_messages.last.inner_text().strip()
        except Exception:
            current = ""
        if current and current == previous:
            stable_for += 1
        else:
            stable_for = 0
        previous = current
        if stable_for >= max(1, response_settle_seconds):
            break
        page.wait_for_timeout(1000)
        waited += 1
    return previous

