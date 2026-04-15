from __future__ import annotations

from urllib.parse import urlparse

from .qwen_session import find_chat_composer


def click_retry_if_visible(page) -> bool:
    retry_selectors = [
        "button:has-text('Retry')",
        "button:has-text('Try again')",
        "button:has-text('Regenerate')",
        "[data-testid='retry-button']",
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


def _is_qwen_host(url: str) -> bool:
    host = (urlparse(url or "").netloc or "").lower()
    return "qwen.ai" in host


def ensure_qwen_surface(page, timeout_ms: int, home_url: str = "https://chat.qwen.ai/"):
    current_url = page.url or ""
    if _is_qwen_host(current_url):
        return page
    last_url = current_url
    for _ in range(2):
        try:
            page.goto(home_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        page.wait_for_timeout(900)
        current_url = page.url or ""
        last_url = current_url or last_url
        if _is_qwen_host(current_url):
            return page
    try:
        fresh = page.context.new_page()
        fresh.goto(home_url, wait_until="domcontentloaded", timeout=timeout_ms)
        fresh.wait_for_timeout(900)
        current_url = fresh.url or ""
        if _is_qwen_host(current_url):
            return fresh
        last_url = current_url or last_url
    except Exception:
        pass
    raise RuntimeError(
        f"Unexpected browser destination: {last_url} (expected host containing qwen.ai)"
    )


def start_new_chat_if_available(page, timeout_ms: int, home_url: str = "https://chat.qwen.ai/") -> bool:
    chat_timeout_ms = min(timeout_ms, 12000)
    # open sidebar first if needed
    try:
        history_btn = page.locator("button[aria-label='Chat history']").first
        if history_btn.count() > 0 and history_btn.is_visible():
            history_btn.click(timeout=1200)
            page.wait_for_timeout(400)
    except Exception:
        pass

    selectors = [
        ".new-chat-button-ui",
        ".new-chat .new-chat-button-ui",
        ".new-chat",
        "button[title='New chat']",
        "button:has-text('New chat')",
        "button:has-text('New Chat')",
        "[data-testid='new-chat-button']",
    ]
    for selector in selectors:
        candidate = page.locator(selector).first
        try:
            if candidate.count() > 0 and candidate.is_visible():
                candidate.click(timeout=1500)
                page.wait_for_timeout(1200)
                current_url = page.url or ""
                if not _is_qwen_host(current_url):
                    try:
                        ensure_qwen_surface(page, timeout_ms=chat_timeout_ms, home_url=home_url)
                    except Exception:
                        continue
                composer = find_chat_composer(page, timeout_ms=chat_timeout_ms)
                if composer is not None:
                    return True
        except Exception:
            continue
    return False


def refresh_chat(page, timeout_ms: int, home_url: str = "https://chat.qwen.ai/") -> None:
    current_url = page.url or ""
    if not _is_qwen_host(current_url):
        try:
            page.goto(home_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1200)
            return
        except Exception:
            pass
    try:
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:
        try:
            current_url = page.url
            if current_url:
                page.goto(current_url, wait_until="domcontentloaded", timeout=timeout_ms)
            else:
                page.goto(home_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
    page.wait_for_timeout(1200)


