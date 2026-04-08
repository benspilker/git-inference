from __future__ import annotations

from .browser_session import find_chat_composer


def click_retry_if_visible(page) -> bool:
    retry_selectors = [
        "button:has-text('Retry')",
        "button:has(div:has-text('Retry'))",
        "div.text-token-text-error button:has-text('Retry')",
        "div.text-token-text-error button",
        "[data-testid='retry-button']",
        ".text-token-text-error button",
    ]
    for selector in retry_selectors:
        try:
            buttons = page.locator(selector)
            count = min(buttons.count(), 5)
            for i in range(count):
                retry_btn = buttons.nth(i)
                if retry_btn.is_visible() and retry_btn.is_enabled():
                    try:
                        retry_btn.click(timeout=3000)
                    except Exception:
                        handle = retry_btn.element_handle()
                        if handle is not None:
                            handle.evaluate("(el) => el.click()")
                        else:
                            continue
                    page.wait_for_timeout(1500)
                    return True
        except Exception:
            continue
    return False


def start_new_chat_if_available(page, timeout_ms: int) -> bool:
    try:
        page.keyboard.press("Control+Shift+O")
        page.wait_for_timeout(1200)
        composer = find_chat_composer(page, timeout_ms=1500)
        if composer is not None:
            return True
    except Exception:
        pass

    selectors = [
        "[data-testid='create-new-chat-button']",
        "button:has-text('New chat')",
        "a:has-text('New chat')",
        "a[href*='new-chat']",
    ]
    for selector in selectors:
        candidate = page.locator(selector).first
        try:
            if candidate.count() > 0 and candidate.is_visible():
                candidate.click()
                page.wait_for_timeout(1200)
                composer = find_chat_composer(page, timeout_ms=timeout_ms)
                if composer is not None:
                    return True
        except Exception:
            continue
    return False


def refresh_chat(page, timeout_ms: int) -> None:
    page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(1200)
