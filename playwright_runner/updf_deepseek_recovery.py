from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from .updf_deepseek_session import find_chat_composer


def click_retry_if_visible(page) -> bool:
    retry_selectors = [
        "button:has-text('Retry')",
        "button:has-text('Try again')",
        "button:has-text('Regenerate')",
        ".el-button:has-text('Retry')",
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


def _is_updf_host(url: str) -> bool:
    host = (urlparse(url or "").netloc or "").lower()
    return "ai.updf.com" in host


def _is_malformed_project_chat_url(url: str) -> bool:
    parsed = urlparse(url or "")
    path = (parsed.path or "").lower()
    if "/chat-project/" not in path:
        return False
    query = parse_qs(parsed.query or "", keep_blank_values=True)
    chat_id = "".join(query.get("id", [""])).strip()
    has_message = "message" in query and bool("".join(query.get("message", [""])).strip())
    return (not chat_id) or has_message


def _go_to_new_chat_surface(page, timeout_ms: int, home_url: str) -> bool:
    selectors = [
        "a[href='/new-chat/']",
        "a.menu-item.bot[href='/new-chat/']",
        ".menu-item.bot",
    ]
    for selector in selectors:
        target = page.locator(selector).first
        try:
            if target.count() > 0 and target.is_visible():
                target.click(timeout=2000)
                page.wait_for_timeout(1200)
                return _is_updf_host(page.url or "")
        except Exception:
            continue
    try:
        page.goto(home_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1200)
        return _is_updf_host(page.url or "")
    except Exception:
        return False


def _has_chat_composer(page, timeout_ms: int) -> bool:
    try:
        return find_chat_composer(page, timeout_ms=min(timeout_ms, 5000)) is not None
    except Exception:
        return False


def ensure_updf_deepseek_surface(page, timeout_ms: int, home_url: str = "https://ai.updf.com/new-chat/"):
    current_url = page.url or ""
    if _is_updf_host(current_url):
        if _is_malformed_project_chat_url(current_url):
            if _go_to_new_chat_surface(page, timeout_ms=timeout_ms, home_url=home_url):
                if _has_chat_composer(page, timeout_ms=timeout_ms):
                    return page
        else:
            if _has_chat_composer(page, timeout_ms=timeout_ms):
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
        if _is_updf_host(current_url):
            if _is_malformed_project_chat_url(current_url):
                if _go_to_new_chat_surface(page, timeout_ms=timeout_ms, home_url=home_url):
                    if _has_chat_composer(page, timeout_ms=timeout_ms):
                        return page
                continue
            if _has_chat_composer(page, timeout_ms=timeout_ms):
                return page
    try:
        fresh = page.context.new_page()
        fresh.goto(home_url, wait_until="domcontentloaded", timeout=timeout_ms)
        fresh.wait_for_timeout(900)
        current_url = fresh.url or ""
        if _is_updf_host(current_url):
            if _is_malformed_project_chat_url(current_url):
                if _go_to_new_chat_surface(fresh, timeout_ms=timeout_ms, home_url=home_url):
                    if _has_chat_composer(fresh, timeout_ms=timeout_ms):
                        return fresh
                last_url = current_url or last_url
            else:
                if _has_chat_composer(fresh, timeout_ms=timeout_ms):
                    return fresh
        last_url = current_url or last_url
    except Exception:
        pass
    raise RuntimeError(
        f"Unexpected browser destination or missing composer: {last_url} (expected ai.updf.com with visible chat composer)"
    )


def _ensure_deepthink_enabled(page, timeout_ms: int) -> None:
    selector = ".send-button-wrapper"
    candidate = page.locator(selector).first
    try:
        if candidate.count() == 0:
            return
        # No-op if already enabled.
        classes = candidate.get_attribute("class") or ""
        if "active" in classes.lower():
            return
        text = (candidate.inner_text() or "").strip().lower()
        if "deepthink" in text:
            candidate.click(timeout=min(2000, timeout_ms))
            page.wait_for_timeout(300)
    except Exception:
        return


def start_new_chat_if_available(page, timeout_ms: int, home_url: str = "https://ai.updf.com/new-chat/") -> bool:
    selectors = [
        "button:has-text('New Chat')",
        "button:has-text('New chat')",
        ".chat-history-panel button:has-text('New Chat')",
        ".chat-history-panel button:has-text('New chat')",
    ]
    chat_timeout_ms = min(timeout_ms, 12000)
    for selector in selectors:
        candidate = page.locator(selector).first
        try:
            if candidate.count() > 0 and candidate.is_visible():
                candidate.click(timeout=1500)
                page.wait_for_timeout(1200)
                current_url = page.url or ""
                if not _is_updf_host(current_url):
                    try:
                        ensure_updf_deepseek_surface(page, timeout_ms=chat_timeout_ms, home_url=home_url)
                    except Exception:
                        continue
                _ensure_deepthink_enabled(page, timeout_ms=chat_timeout_ms)
                composer = find_chat_composer(page, timeout_ms=chat_timeout_ms)
                if composer is not None:
                    return True
        except Exception:
            continue
    return False


def refresh_chat(page, timeout_ms: int, home_url: str = "https://ai.updf.com/new-chat/") -> None:
    current_url = page.url or ""
    if _is_malformed_project_chat_url(current_url):
        if _go_to_new_chat_surface(page, timeout_ms=timeout_ms, home_url=home_url):
            _ensure_deepthink_enabled(page, timeout_ms=timeout_ms)
            return
    if not _is_updf_host(current_url):
        try:
            page.goto(home_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1200)
            _ensure_deepthink_enabled(page, timeout_ms=timeout_ms)
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
    _ensure_deepthink_enabled(page, timeout_ms=timeout_ms)
