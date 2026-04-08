from __future__ import annotations

import re
import time
from pathlib import Path


def first_visible_locator(page, selectors: list[str], timeout_ms: int):
    per_selector_timeout = max(1000, int(timeout_ms / max(1, len(selectors))))
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=per_selector_timeout)
            return locator
        except Exception:
            continue
    return None


def dismiss_blocking_modals(page) -> None:
    modal_selectors = [
        "#modal-moonshine-nux-v2",
        "[data-testid='modal-moonshine-nux-v2']",
    ]
    close_selectors = [
        "button[aria-label='Close']",
        "button:has-text('Close')",
        "button:has-text('Got it')",
        "button:has-text('Continue')",
        "button:has-text('Skip')",
        "button:has-text('Done')",
    ]
    for modal_selector in modal_selectors:
        modal = page.locator(modal_selector).first
        try:
            if modal.count() == 0 or not modal.is_visible():
                continue
        except Exception:
            continue
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass
        for close_selector in close_selectors:
            try:
                btn = modal.locator(close_selector).first
                if btn.count() > 0 and btn.is_visible():
                    try:
                        btn.click(timeout=1500)
                    except Exception:
                        handle = btn.element_handle()
                        if handle is not None:
                            handle.evaluate("(el) => el.click()")
                    page.wait_for_timeout(250)
                    break
            except Exception:
                continue


def find_chat_composer(page, timeout_ms: int):
    selectors = [
        "#prompt-textarea",
        "[data-testid='prompt-textarea']",
        "textarea[data-testid='prompt-textarea']",
        "form textarea#prompt-textarea",
        "form textarea[placeholder*='Message']",
        "form textarea",
        "textarea[placeholder*='Message']",
        "textarea[aria-label*='Message']",
        "textarea",
        "[contenteditable='true'][id*='prompt']",
        "[contenteditable='true'][data-testid*='prompt']",
        "[contenteditable='true'][data-lexical-editor='true']",
        "[contenteditable='true'][aria-label*='Message']",
        "[contenteditable='true'][role='textbox']",
        "div[contenteditable='true'][role='textbox']",
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
        "[data-testid^='conversation-turn-'][data-turn='assistant'] [data-message-author-role='assistant']",
        "[data-testid^='conversation-turn-'][data-turn='assistant']",
        "[data-message-author-role='assistant']",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    return page.locator("[data-message-author-role='assistant']")


def extract_response_text(assistant_messages, before_count: int, before_last_text: str) -> str:
    def _normalize(text: str) -> str:
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^\s*chatgpt\s+said:\s*\n+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*chatgpt\s*:\s*\n+", "", cleaned, flags=re.IGNORECASE)
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
        "i hit an internal automation issue while generating a response",
        "please send your message again",
        "new session started",
    ]
    return any(marker in lowered for marker in blocked_markers)


def set_composer_text(composer, text: str) -> None:
    handle = composer.element_handle()
    if handle is None:
        raise RuntimeError("Failed to resolve composer element.")
    handle.evaluate(
        """(el, value) => {
            const isEditable = el.getAttribute('contenteditable') === 'true' || el.isContentEditable;
            if (isEditable) {
                el.focus();
                el.textContent = '';
                const lines = value.split('\\n');
                for (let i = 0; i < lines.length; i++) {
                    if (i > 0) el.appendChild(document.createElement('br'));
                    el.appendChild(document.createTextNode(lines[i]));
                }
                el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return;
            }
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
    set_composer_text(composer, prompt_text)
    send_selectors = [
        "button[data-testid='send-button']",
        "button[aria-label*='Send']:not([data-testid='composer-plus-btn'])",
        "button[aria-label*='send']:not([data-testid='composer-plus-btn'])",
        "form button:not([data-testid='composer-plus-btn']):has(svg[data-icon='arrow-up'])",
    ]
    deadline = time.time() + 15
    while time.time() < deadline:
        for selector in send_selectors:
            btn = page.locator(selector).first
            try:
                if btn.count() == 0 or not btn.is_visible() or not btn.is_enabled():
                    continue
                btn.click(timeout=2000)
                return
            except Exception:
                continue
        page.wait_for_timeout(300)
    composer.press("Enter")


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
                "button[data-testid='stop-button'], button:has-text('Stop generating')"
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
