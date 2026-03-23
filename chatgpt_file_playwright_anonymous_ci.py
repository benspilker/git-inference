#!/usr/bin/env python3
"""Read prompt text from a file, send it to ChatGPT in browser, and save response text."""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def ensure_package(module_name: str, pip_name: str | None = None):
    target = pip_name or module_name.split(".")[0]
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", target])
        return importlib.import_module(module_name)


def ensure_browser_installed() -> None:
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


def _first_visible_locator(page, selectors: list[str], timeout_ms: int):
    per_selector_timeout = max(1000, int(timeout_ms / max(1, len(selectors))))
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=per_selector_timeout)
            return locator
        except Exception:
            continue
    return None


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
        return value[1:-1]
    return value


def load_env_file_if_present(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_wrapping_quotes(value.strip())
        if key and key not in os.environ:
            os.environ[key] = value


def _decode_password_b64(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
        return decoded.decode("utf-8")
    except Exception as exc:
        raise ValueError("Invalid base64 password value.") from exc


def _set_composer_text(composer, text: str) -> None:
    element_handle = composer.element_handle()
    if element_handle is None:
        raise RuntimeError("Failed to resolve ChatGPT composer element handle.")

    # Works for both textarea and contenteditable editors (e.g., ProseMirror).
    element_handle.evaluate(
        """(el, value) => {
            const isEditable = el.getAttribute('contenteditable') === 'true' || el.isContentEditable;
            if (isEditable) {
                el.focus();
                el.textContent = '';
                const lines = value.split('\\n');
                for (let i = 0; i < lines.length; i++) {
                    if (i > 0) {
                        el.appendChild(document.createElement('br'));
                    }
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


def _set_clipboard_text(page, text: str) -> bool:
    try:
        page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://chatgpt.com")
    except Exception:
        pass
    try:
        return bool(
            page.evaluate(
                """async (value) => {
                    try {
                        await navigator.clipboard.writeText(value);
                        return true;
                    } catch (_) {
                        return false;
                    }
                }""",
                text,
            )
        )
    except Exception:
        return False


def _enable_network_logging(page, log_file: Path, enabled: bool = True) -> None:
    if not enabled:
        return

    log_file.parent.mkdir(parents=True, exist_ok=True)

    def _append(line: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {line}\n")
        except Exception:
            pass

    interesting_fragments = [
        "/backend-api/",
        "/conversation",
        "/responses",
        "/auth/",
        "/api/",
    ]

    def _is_interesting(url: str) -> bool:
        return any(fragment in url for fragment in interesting_fragments)

    def on_request(request):
        url = request.url
        if not _is_interesting(url):
            return
        _append(f"REQ {request.method} {url}")

    def on_response(response):
        url = response.url
        if not _is_interesting(url):
            return
        status = response.status
        _append(f"RES {status} {response.request.method} {url}")
        if status >= 400:
            try:
                body = response.text()
            except Exception:
                body = ""
            body = (body or "").replace("\n", " ").strip()
            if len(body) > 600:
                body = body[:600] + "...<truncated>"
            if body:
                _append(f"ERR_BODY {body}")

    page.on("request", on_request)
    page.on("response", on_response)
    _append("Network logging enabled.")


def _dismiss_blocking_modals(page) -> None:
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

        # First try keyboard escape, then explicit close actions.
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass

        closed = False
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
                    closed = True
                    break
            except Exception:
                continue

        if not closed:
            # Last resort: remove modal overlay so composer is clickable.
            try:
                handle = modal.element_handle()
                if handle is not None:
                    handle.evaluate("(el) => el.remove()")
            except Exception:
                pass


def _send_current_prompt(page, composer, prompt_text: str, use_ctrl_v: bool = False) -> None:
    _dismiss_blocking_modals(page)
    try:
        composer.click(timeout=3000)
    except Exception:
        _dismiss_blocking_modals(page)
        try:
            composer.click(force=True, timeout=3000)
        except Exception:
            handle = composer.element_handle()
            if handle is not None:
                handle.evaluate("(el) => { if (el.focus) el.focus(); }")
    pasted = False
    if use_ctrl_v and _set_clipboard_text(page, prompt_text):
        try:
            composer.press("Control+A")
            composer.press("Backspace")
            composer.press("Control+V")
            page.wait_for_timeout(250)
            pasted = True
        except Exception:
            pasted = False

    if not pasted:
        _set_composer_text(composer, prompt_text)

    send_selectors = [
        "button[data-testid='send-button']",
        "button[aria-label*='Send']:not([data-testid='composer-plus-btn'])",
        "button[aria-label*='send']:not([data-testid='composer-plus-btn'])",
        # Right-side circular send button without relying on brittle class names.
        "form button:not([data-testid='composer-plus-btn']):has(svg[data-icon='arrow-up'])",
    ]
    sent = False
    deadline = time.time() + 15
    while time.time() < deadline and not sent:
        for selector in send_selectors:
            btn = page.locator(selector).first
            try:
                if btn.count() == 0 or not btn.is_visible():
                    continue
                if not btn.is_enabled():
                    continue
                try:
                    label = (btn.get_attribute("aria-label") or "").lower()
                except Exception:
                    label = ""
                if any(x in label for x in ["add", "upload", "photo", "file", "plus"]):
                    continue
                try:
                    btn.click(timeout=2000)
                    sent = True
                    break
                except Exception:
                    handle = btn.element_handle()
                    if handle is not None:
                        handle.evaluate("(el) => el.click()")
                        sent = True
                        break
            except Exception:
                continue
        if not sent:
            page.wait_for_timeout(300)

    if not sent:
        try:
            composer.click()
            composer.press("Enter")
        except Exception:
            page.keyboard.press("Enter")


def _wait_for_user_turn_increment(page, before_count: int, timeout_ms: int = 15000) -> bool:
    turns = page.locator("[data-testid^='conversation-turn-'][data-turn='user']")
    deadline = time.time() + max(1, timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            if turns.count() > before_count:
                return True
        except Exception:
            pass
        page.wait_for_timeout(250)
    return False


def _refocus_chat_session(page) -> bool:
    # Heuristic selectors for latest chat in left history panel.
    selectors = [
        "nav[aria-label='Chat history'] a[href*='/c/']",
        "nav[aria-label='Chat history'] [data-testid*='history-item']",
        "aside nav a[href*='/c/']",
        "nav a[href*='/c/']",
        "[data-testid*='history-item']",
        "a[aria-label*='chat']",
    ]
    for selector in selectors:
        item = page.locator(selector).first
        try:
            if item.count() > 0 and item.is_visible():
                item.click()
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


def _extract_response_text(assistant_messages, before_count: int, before_last_text: str) -> str:
    after_count = assistant_messages.count()
    if after_count == 0:
        return ""

    if after_count > before_count:
        text = assistant_messages.nth(after_count - 1).inner_text().strip()
        return text

    # Fallback: some UI states update the latest assistant block in place.
    text = assistant_messages.last.inner_text().strip()
    if text and text != before_last_text:
        return text
    return ""


def _assistant_turns(page):
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


def _wait_for_valid_response(
    page,
    assistant_messages,
    before_count: int,
    before_last_text: str,
    wait_seconds: int,
    early_error_break_seconds: int = 0,
) -> str:
    deadline = time.time() + max(1, wait_seconds)
    refocused = False
    error_started_at = None
    while time.time() < deadline:
        text = _extract_response_text(assistant_messages, before_count, before_last_text)
        lowered = text.lower()
        if text and "something went wrong" not in lowered:
            return text

        # In some sessions, clicking latest chat in sidebar refreshes response rendering.
        if (not refocused) and ("something went wrong" in lowered):
            refocused = _refocus_chat_session(page)
            if refocused:
                deadline = max(deadline, time.time() + min(wait_seconds, 60))
                page.wait_for_timeout(1200)
                continue

        # Optional fast-fail into retry workflow, but only after the error
        # stays visible for a few seconds.
        try:
            visible_error = page.locator("text=Something went wrong").first.is_visible()
            retry_visible = page.locator("button:has-text('Retry')").first.is_visible()
        except Exception:
            visible_error = False
            retry_visible = False
        if visible_error and retry_visible and early_error_break_seconds > 0:
            if error_started_at is None:
                error_started_at = time.time()
            elif (time.time() - error_started_at) >= early_error_break_seconds:
                return ""
        else:
            error_started_at = None

        page.wait_for_timeout(1000)
    return ""


def _stabilize_and_refresh_response(
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
        # If a stop button is visible, generation is likely still in progress.
        stop_visible = False
        try:
            stop_visible = page.locator(
                "button[data-testid='stop-button'], button:has-text('Stop generating')"
            ).first.is_visible()
        except Exception:
            stop_visible = False

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


def _click_retry_if_visible(page) -> bool:
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
                        # Fallback for stubborn UI layers.
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


def _enter_text(locator, value: str, label: str) -> None:
    try:
        locator.focus()
    except Exception:
        pass

    try:
        locator.fill(value)
    except Exception:
        element_handle = locator.element_handle()
        if element_handle is None:
            raise RuntimeError(f"Failed to resolve input element for {label}.")
        element_handle.evaluate(
            """(el, v) => {
                el.focus();
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )

    try:
        typed = locator.input_value().strip()
    except Exception:
        typed = ""
    if typed != value:
        raise RuntimeError(f"Failed to enter {label}.")


def _looks_like_welcome_back_reauth(page) -> bool:
    url = (page.url or "").lower()
    if "auth.openai.com" not in url and "chatgpt.com" not in url:
        return False

    try:
        has_welcome_back = page.locator("text=Welcome back").first.count() > 0
    except Exception:
        has_welcome_back = False

    if not has_welcome_back:
        return False

    email_fields = page.locator(
        "input[type='email'], input[name='email'], input[autocomplete='email'], input[autocomplete='username']"
    )
    try:
        return email_fields.count() > 0
    except Exception:
        return False


def _find_chat_composer(page, timeout_ms: int):
    composer_selectors = [
        "#prompt-textarea",
        "textarea[placeholder*='Message']",
        "[contenteditable='true'][id*='prompt']",
        "[contenteditable='true'][aria-label*='Message']",
    ]
    return _first_visible_locator(page, composer_selectors, timeout_ms=timeout_ms)


def _is_openai_auth_url(url: str) -> bool:
    value = (url or "").lower()
    return "auth.openai.com" in value


def _recover_to_chatgpt_if_needed(page, chat_url: str, timeout_ms: int):
    composer = _find_chat_composer(page, timeout_ms=2500)
    if composer is not None:
        return composer

    if _looks_like_welcome_back_reauth(page):
        page.goto(chat_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1500)
        composer = _find_chat_composer(page, timeout_ms=timeout_ms)
        if composer is not None:
            return composer

    page.goto(chat_url, wait_until="domcontentloaded", timeout=timeout_ms)
    return _find_chat_composer(page, timeout_ms=timeout_ms)


def _start_new_chat_if_available(page, timeout_ms: int) -> None:
    # Prefer keyboard shortcut first to force a truly new chat thread.
    try:
        page.keyboard.press("Control+Shift+O")
        page.wait_for_timeout(1200)
        composer = _find_chat_composer(page, timeout_ms=1500)
        if composer is not None:
            turns = page.locator("[data-testid^='conversation-turn-']")
            try:
                if turns.count() == 0:
                    return
            except Exception:
                pass
    except Exception:
        pass

    selectors = [
        "[data-testid='create-new-chat-button']",
        "button:has-text('New chat')",
        "a:has-text('New chat')",
        "a[href*='new-chat']",
    ]
    current_turns = page.locator("[data-testid^='conversation-turn-']")
    before_turn_count = 0
    try:
        before_turn_count = current_turns.count()
    except Exception:
        before_turn_count = 0

    for selector in selectors:
        candidate = page.locator(selector).first
        try:
            if candidate.count() > 0 and candidate.is_visible():
                candidate.click()
                page.wait_for_timeout(1200)
                # New chat should clear current thread turns quickly.
                deadline = time.time() + min(8, max(2, timeout_ms / 1000.0))
                while time.time() < deadline:
                    try:
                        if current_turns.count() == 0:
                            composer = _find_chat_composer(page, timeout_ms=1500)
                            if composer is not None:
                                return
                    except Exception:
                        pass
                    page.wait_for_timeout(300)

                composer = _find_chat_composer(page, timeout_ms=timeout_ms)
                if composer is not None:
                    # If turns are unchanged, this click likely did not create a new thread.
                    try:
                        if current_turns.count() < before_turn_count:
                            return
                        if current_turns.count() == 0:
                            return
                    except Exception:
                        return
        except Exception:
            continue

    # Last resort: keyboard shortcut used by ChatGPT for new chat.
    try:
        page.keyboard.press("Control+Shift+O")
        page.wait_for_timeout(1200)
    except Exception:
        return


def _redirect_from_openai_login_to_chat(page, chat_url: str, timeout_ms: int) -> None:
    current_url = (page.url or "").lower()
    if _is_openai_auth_url(current_url):
        page.goto(chat_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1500)


def _ensure_logged_in(
    page,
    email: str | None,
    password: str | None,
    timeout_ms: int,
    force_login: bool = False,
    login_url: str = "https://auth.openai.com/log-in",
    manual_login_wait_seconds: int = 0,
) -> None:
    composer_selectors = [
        "#prompt-textarea",
        "textarea[placeholder*='Message']",
        "[contenteditable='true'][id*='prompt']",
        "[contenteditable='true'][aria-label*='Message']",
    ]
    composer = _first_visible_locator(page, composer_selectors, timeout_ms=2500)
    if composer is not None and not force_login:
        return

    if not email or not password:
        return

    if force_login:
        page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1000)

    login_trigger_selectors = [
        "button:has-text('Log in')",
        "a:has-text('Log in')",
        "button:has-text('Login')",
        "a:has-text('Login')",
    ]
    login_trigger = _first_visible_locator(page, login_trigger_selectors, timeout_ms=5000)
    if login_trigger is not None:
        login_trigger.click()
        page.wait_for_timeout(1200)

    password_input = None
    for _ in range(3):
        email_input = _first_visible_locator(
            page,
            [
                "input[type='email']",
                "input[name='email']",
                "input[name='identifier']",
                "input[name='username']",
                "input[autocomplete='username']",
            ],
            timeout_ms=6000,
        )
        if email_input is not None:
            _enter_text(email_input, email, "email")
            try:
                continue_btn = page.locator(
                    "button:has-text('Continue'), button:has-text('Next'), #identifierNext button"
                ).first
                if continue_btn.is_visible() and continue_btn.is_enabled():
                    continue_btn.click()
                else:
                    email_input.press("Enter")
            except Exception:
                email_input.press("Enter")
            page.wait_for_timeout(1500)

        password_input = _first_visible_locator(
            page,
            [
                "input[type='password']",
                "input[name='password']",
                "input[name='Passwd']",
                "input[autocomplete='current-password']",
            ],
            timeout_ms=6000,
        )
        if password_input is not None:
            break

    if password_input is None:
        current_url = page.url or ""
        if manual_login_wait_seconds > 0:
            print(
                f"Automated login stalled. Waiting {manual_login_wait_seconds}s for manual completion...",
                file=sys.stderr,
            )
            page.wait_for_timeout(manual_login_wait_seconds * 1000)
            composer = _first_visible_locator(page, composer_selectors, timeout_ms=timeout_ms)
            if composer is not None:
                return
        if "accounts.google.com" in current_url:
            raise RuntimeError(
                "Google sign-in flow detected, but password field did not appear after email retries. "
                "This likely requires manual verification."
            )
        raise RuntimeError("Could not find password input for ChatGPT login.")

    _enter_text(password_input, password, "password")
    password_was_submitted = False
    try:
        submit_btn = page.locator(
            "button:has-text('Continue'), button:has-text('Next'), #passwordNext button"
        ).first
        if submit_btn.is_visible() and submit_btn.is_enabled():
            submit_btn.click()
            password_was_submitted = True
        else:
            password_input.press("Enter")
            password_was_submitted = True
    except Exception:
        password_input.press("Enter")
        password_was_submitted = True

    # Deterministic post-password handling: wait 20s for any OAuth/checkpoint hops,
    # then let caller redirect to chatgpt.com regardless of intermediate auth pages.
    if password_was_submitted:
        page.wait_for_timeout(10000)
        return

    composer = _first_visible_locator(page, composer_selectors, timeout_ms=8000)
    if composer is None:
        raise RuntimeError("Login submitted, but ChatGPT composer did not appear.")


def prune_top_level_json_sections(text: str, keys_to_remove: list[str]) -> str:
    if not keys_to_remove:
        return text
    try:
        data = json.loads(text)
    except Exception:
        return text
    if not isinstance(data, dict):
        return text

    changed = False
    for key in keys_to_remove:
        if key in data:
            del data[key]
            changed = True
    if not changed:
        return text
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_prompt_text(
    input_file: Path,
    instructions_file: Path | None = None,
    omit_sections: list[str] | None = None,
) -> tuple[str, str | None]:
    input_text = input_file.read_text(encoding="utf-8").strip()
    if not input_text:
        raise ValueError(f"Input file is empty: {input_file}")
    input_text = prune_top_level_json_sections(input_text, omit_sections or [])

    if instructions_file is None:
        return input_text, None

    instructions_text = instructions_file.read_text(encoding="utf-8").strip()
    if not instructions_text:
        raise ValueError(f"Instructions file is empty: {instructions_file}")

    return input_text, instructions_text


def split_text_into_chunks(text: str, chunks: int) -> list[str]:
    if chunks <= 1:
        return [text]
    cleaned = text.strip()
    if not cleaned:
        return [cleaned]
    n = len(cleaned)
    chunk_size = max(1, (n + chunks - 1) // chunks)
    parts: list[str] = []
    start = 0
    while start < n:
        end = min(n, start + chunk_size)
        parts.append(cleaned[start:end].strip())
        start = end
    return [p for p in parts if p]


def _find_balanced_json_block(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]
        start = text.find("{", start + 1)
    return None


def _extract_json_payload(text: str) -> dict | None:
    fence_matches = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    for candidate in fence_matches:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue

    raw_block = _find_balanced_json_block(text)
    if not raw_block:
        return None
    try:
        parsed = json.loads(raw_block)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


def _dedupe_objects(items: list[dict], key_fields: list[str]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key_parts = [str(item.get(field, "")).strip().lower() for field in key_fields]
        if all(not part for part in key_parts):
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        else:
            key = "|".join(key_parts)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _merge_map_payloads(payloads: list[dict]) -> dict:
    merged = {
        "top_findings": [],
        "actions": [],
        "retiring_features": [],
        "orphaned_resources": [],
        "cost_opportunities": [],
        "notes": [],
    }

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in merged.keys():
            value = payload.get(key, [])
            if isinstance(value, list):
                merged[key].extend(value)

    merged["top_findings"] = _dedupe_objects(merged["top_findings"], ["title", "category", "severity"])
    merged["actions"] = _dedupe_objects(merged["actions"], ["action", "category", "priority"])
    merged["retiring_features"] = _dedupe_objects(
        merged["retiring_features"], ["resource", "detail", "source"]
    )
    merged["orphaned_resources"] = _dedupe_objects(
        merged["orphaned_resources"], ["resource", "resource_type", "source"]
    )
    merged["cost_opportunities"] = _dedupe_objects(
        merged["cost_opportunities"], ["item", "detail", "priority"]
    )

    note_strings: list[str] = []
    for note in merged["notes"]:
        if isinstance(note, str):
            text = note.strip()
            if text:
                note_strings.append(text)
    merged["notes"] = sorted(set(note_strings))
    return merged


def _build_chunk_map_prompt(chunk_text: str, chunk_idx: int, total_chunks: int) -> str:
    schema = """{
  "top_findings": [
    {
      "title": "string",
      "category": "string",
      "severity": "Critical|High|Medium|Low",
      "impact": "string",
      "priority": "P1|P2|P3"
    }
  ],
  "actions": [
    {
      "action": "string",
      "category": "string",
      "priority": "P1|P2|P3",
      "effort": "string",
      "impact": "string"
    }
  ],
  "retiring_features": [
    {
      "resource": "string",
      "detail": "string",
      "severity": "string",
      "source": "string"
    }
  ],
  "orphaned_resources": [
    {
      "resource": "string",
      "resource_type": "string",
      "detail": "string",
      "severity": "string",
      "source": "string"
    }
  ],
  "cost_opportunities": [
    {
      "item": "string",
      "detail": "string",
      "estimated_savings": "string",
      "priority": "P1|P2|P3"
    }
  ],
  "notes": ["string"]
}"""
    return (
        f"You are in map phase for chunk {chunk_idx}/{total_chunks}.\n"
        "Extract only actionable remediation data from this chunk.\n"
        "Return ONLY valid JSON matching the schema below.\n"
        "No markdown. No prose. If data is missing, use empty arrays.\n\n"
        f"Schema:\n{schema}\n\n"
        f"Chunk data:\n{chunk_text}"
    )


def _build_reduce_prompt(merged_payload: dict, instructions_text: str | None) -> str:
    base_instruction = instructions_text or (
        "Summarize the data into concise markdown with actionable remediation priorities."
    )
    return (
        "Ignore any earlier constraints from previous messages that required JSON-only output.\n"
        f"{base_instruction}\n\n"
        "Use ONLY the structured dataset below.\n"
        "Requirements:\n"
        "- Produce one cohesive markdown response.\n"
        "- Do not repeat headings or repeated items.\n"
        "- Consolidate overlaps and keep one best entry per issue.\n"
        "- Focus on prioritized remediation actions.\n"
        "- Keep it concise and practical.\n\n"
        "Structured dataset:\n"
        f"```json\n{json.dumps(merged_payload, indent=2, ensure_ascii=False)}\n```"
    )


def _build_context_chunk_prompt(chunk_text: str, chunk_idx: int, total_chunks: int) -> str:
    return (
        f"You are receiving chunk {chunk_idx}/{total_chunks} of a split dataset.\n"
        "Store this chunk for later synthesis.\n"
        "Do not summarize yet.\n"
        f"Reply exactly: ACK {chunk_idx}/{total_chunks}\n\n"
        f"Chunk data:\n{chunk_text}"
    )


def _build_final_chunk_prompt(
    chunk_text: str,
    chunk_idx: int,
    total_chunks: int,
    instructions_text: str | None,
) -> str:
    base_instruction = instructions_text or (
        "Summarize the data into concise markdown with actionable remediation priorities."
    )
    return (
        f"{base_instruction}\n\n"
        f"You now have all {total_chunks} chunks in this same chat thread.\n"
        "Use all previous chunks plus this final chunk to produce one cohesive final answer.\n"
        "Do not return JSON.\n\n"
        f"Final chunk ({chunk_idx}/{total_chunks}) data:\n{chunk_text}"
    )


def _looks_like_map_payload_json(text: str) -> bool:
    payload = _extract_json_payload(text)
    if not isinstance(payload, dict):
        return False
    required = {
        "top_findings",
        "actions",
        "retiring_features",
        "orphaned_resources",
        "cost_opportunities",
        "notes",
    }
    return required.issubset(payload.keys())


def _send_prompt_and_collect_response(
    page,
    composer,
    assistant_messages,
    prompt_text: str,
    chat_url: str,
    timeout_ms: int,
    wait_seconds: int,
    post_response_wait_seconds: int,
    response_settle_seconds: int,
    max_settle_wait_seconds: int,
    use_ctrl_v: bool,
    start_new_chat: bool = False,
) -> str:
    user_turns = page.locator("[data-testid^='conversation-turn-'][data-turn='user']")
    before_user_count = 0
    try:
        before_user_count = user_turns.count()
    except Exception:
        before_user_count = 0
    before_count = assistant_messages.count()
    before_last_text = ""
    if before_count > 0:
        try:
            before_last_text = assistant_messages.last.inner_text().strip()
        except Exception:
            before_last_text = ""
    _send_current_prompt(page, composer, prompt_text, use_ctrl_v=use_ctrl_v)
    _wait_for_user_turn_increment(page, before_user_count, timeout_ms=15000)
    page.wait_for_timeout(1200)
    _refocus_chat_session(page)
    assistant_messages = _assistant_turns(page)

    response_text = _wait_for_valid_response(
        page,
        assistant_messages,
        before_count=before_count,
        before_last_text=before_last_text,
        wait_seconds=wait_seconds,
        early_error_break_seconds=6,
    )

    if not response_text and _click_retry_if_visible(page):
        page.wait_for_timeout(3000)
        composer = _find_chat_composer(page, timeout_ms=timeout_ms)
        if composer is not None:
            user_turns = page.locator("[data-testid^='conversation-turn-'][data-turn='user']")
            try:
                before_user_count = user_turns.count()
            except Exception:
                before_user_count = 0
            before_count = assistant_messages.count()
            before_last_text = ""
            if before_count > 0:
                try:
                    before_last_text = assistant_messages.last.inner_text().strip()
                except Exception:
                    before_last_text = ""
            _send_current_prompt(page, composer, prompt_text, use_ctrl_v=use_ctrl_v)
            _wait_for_user_turn_increment(page, before_user_count, timeout_ms=15000)
            page.wait_for_timeout(1200)
            _refocus_chat_session(page)
            assistant_messages = _assistant_turns(page)
            response_text = _wait_for_valid_response(
                page,
                assistant_messages,
                before_count=before_count,
                before_last_text=before_last_text,
                wait_seconds=wait_seconds,
                early_error_break_seconds=0,
            )

    if not response_text:
        composer = _find_chat_composer(page, timeout_ms=timeout_ms)
        if composer is not None:
            user_turns = page.locator("[data-testid^='conversation-turn-'][data-turn='user']")
            try:
                before_user_count = user_turns.count()
            except Exception:
                before_user_count = 0
            before_count = assistant_messages.count()
            before_last_text = ""
            if before_count > 0:
                try:
                    before_last_text = assistant_messages.last.inner_text().strip()
                except Exception:
                    before_last_text = ""
            _send_current_prompt(page, composer, prompt_text, use_ctrl_v=use_ctrl_v)
            _wait_for_user_turn_increment(page, before_user_count, timeout_ms=15000)
            page.wait_for_timeout(1200)
            _refocus_chat_session(page)
            assistant_messages = _assistant_turns(page)
            response_text = _wait_for_valid_response(
                page,
                assistant_messages,
                before_count=before_count,
                before_last_text=before_last_text,
                wait_seconds=wait_seconds,
                early_error_break_seconds=0,
            )

    if not response_text:
        page.goto(chat_url, wait_until="domcontentloaded", timeout=timeout_ms)
        _redirect_from_openai_login_to_chat(page, chat_url=chat_url, timeout_ms=timeout_ms)
        composer = _recover_to_chatgpt_if_needed(page, chat_url=chat_url, timeout_ms=timeout_ms)
        if composer is not None and start_new_chat:
            _start_new_chat_if_available(page, timeout_ms=timeout_ms)
            composer = _find_chat_composer(page, timeout_ms=timeout_ms)
        if composer is not None:
            assistant_messages = _assistant_turns(page)
            user_turns = page.locator("[data-testid^='conversation-turn-'][data-turn='user']")
            try:
                before_user_count = user_turns.count()
            except Exception:
                before_user_count = 0
            before_count = assistant_messages.count()
            before_last_text = ""
            if before_count > 0:
                try:
                    before_last_text = assistant_messages.last.inner_text().strip()
                except Exception:
                    before_last_text = ""
            _send_current_prompt(page, composer, prompt_text, use_ctrl_v=use_ctrl_v)
            _wait_for_user_turn_increment(page, before_user_count, timeout_ms=15000)
            page.wait_for_timeout(1200)
            _refocus_chat_session(page)
            assistant_messages = _assistant_turns(page)
            response_text = _wait_for_valid_response(
                page,
                assistant_messages,
                before_count=before_count,
                before_last_text=before_last_text,
                wait_seconds=wait_seconds,
                early_error_break_seconds=0,
            )

    if not response_text:
        raise RuntimeError("Assistant response was empty for prompt submission.")

    refreshed = _stabilize_and_refresh_response(
        page,
        assistant_messages,
        post_response_wait_seconds=post_response_wait_seconds,
        response_settle_seconds=response_settle_seconds,
        max_settle_wait_seconds=max_settle_wait_seconds,
    )
    if refreshed and "something went wrong" not in refreshed.lower():
        response_text = refreshed
    return response_text


def run(
    input_file: Path,
    instructions_file: Path | None,
    output_file: Path,
    email: str | None,
    password: str | None,
    iterations: int,
    wait_seconds: int,
    headless: bool,
    timeout_ms: int,
    user_data_dir: Path,
    url: str,
    error_screenshot: Path | None,
    force_login: bool,
    login_url: str,
    manual_login_wait_seconds: int,
    incognito: bool,
    use_ctrl_v: bool,
    network_log_file: Path | None,
    network_log: bool,
    chunks: int,
    omit_sections: list[str],
    post_response_wait_seconds: int,
    response_settle_seconds: int,
    max_settle_wait_seconds: int,
    map_reduce: bool,
    finalize_on_last_chunk: bool,
    start_new_chat: bool,
) -> None:
    playwright_sync_api = ensure_package("playwright.sync_api")
    sync_playwright = playwright_sync_api.sync_playwright

    initial_input_text, instructions_text = build_prompt_text(
        input_file,
        instructions_file=instructions_file,
        omit_sections=omit_sections,
    )
    input_chunks = split_text_into_chunks(initial_input_text, chunks)

    user_data_dir.mkdir(parents=True, exist_ok=True)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = None
        if incognito:
            browser = p.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context()
        else:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )

        page = context.pages[0] if context.pages else context.new_page()
        if network_log_file is not None:
            _enable_network_logging(page, network_log_file, enabled=network_log)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            _ensure_logged_in(
                page,
                email=email,
                password=password,
                timeout_ms=timeout_ms,
                force_login=force_login,
                login_url=login_url,
                manual_login_wait_seconds=manual_login_wait_seconds,
            )
            _redirect_from_openai_login_to_chat(page, chat_url=url, timeout_ms=timeout_ms)

            composer = _recover_to_chatgpt_if_needed(page, chat_url=url, timeout_ms=timeout_ms)
            if composer is None:
                raise RuntimeError(
                    "Could not find the ChatGPT composer. Log in manually in this browser window and rerun."
                )
            if start_new_chat:
                _start_new_chat_if_available(page, timeout_ms=timeout_ms)
                composer = _find_chat_composer(page, timeout_ms=timeout_ms)
                if composer is None:
                    raise RuntimeError("Could not find composer after creating a new chat.")

            if finalize_on_last_chunk and len(input_chunks) > 1:
                if iterations != 1:
                    print(
                        "Warning: --iterations is ignored when --finalize-on-last-chunk is used.",
                        file=sys.stderr,
                    )
                output_text = ""
                total_chunks = len(input_chunks)
                for chunk_idx, chunk_input_text in enumerate(input_chunks, start=1):
                    composer = _find_chat_composer(page, timeout_ms=timeout_ms)
                    if composer is None:
                        raise RuntimeError(
                            f"Could not find composer for chunk {chunk_idx}/{total_chunks} in final-on-last-chunk mode."
                        )
                    if chunk_idx < total_chunks:
                        prompt_text = _build_context_chunk_prompt(
                            chunk_input_text, chunk_idx=chunk_idx, total_chunks=total_chunks
                        )
                    else:
                        prompt_text = _build_final_chunk_prompt(
                            chunk_input_text,
                            chunk_idx=chunk_idx,
                            total_chunks=total_chunks,
                            instructions_text=instructions_text,
                        )
                    chunk_response = _send_prompt_and_collect_response(
                        page=page,
                        composer=composer,
                        assistant_messages=_assistant_turns(page),
                        prompt_text=prompt_text,
                        chat_url=url,
                        timeout_ms=timeout_ms,
                        wait_seconds=wait_seconds,
                        post_response_wait_seconds=post_response_wait_seconds,
                        response_settle_seconds=response_settle_seconds,
                        max_settle_wait_seconds=max_settle_wait_seconds,
                        use_ctrl_v=use_ctrl_v,
                        start_new_chat=start_new_chat,
                    )
                    if chunk_idx == total_chunks:
                        output_text = chunk_response
                if not output_text:
                    raise RuntimeError("Assistant response was empty in final-on-last-chunk mode.")
            elif map_reduce and len(input_chunks) > 1:
                if iterations != 1:
                    print(
                        "Warning: --iterations is ignored when map-reduce mode is used with multiple chunks.",
                        file=sys.stderr,
                    )
                map_payloads: list[dict] = []
                for chunk_idx, chunk_input_text in enumerate(input_chunks, start=1):
                    composer = _find_chat_composer(page, timeout_ms=timeout_ms)
                    if composer is None:
                        raise RuntimeError(f"Could not find composer for map chunk {chunk_idx}/{len(input_chunks)}.")

                    map_prompt = _build_chunk_map_prompt(
                        chunk_input_text,
                        chunk_idx=chunk_idx,
                        total_chunks=len(input_chunks),
                    )
                    map_response = _send_prompt_and_collect_response(
                        page=page,
                        composer=composer,
                        assistant_messages=_assistant_turns(page),
                        prompt_text=map_prompt,
                        chat_url=url,
                        timeout_ms=timeout_ms,
                        wait_seconds=wait_seconds,
                        post_response_wait_seconds=post_response_wait_seconds,
                        response_settle_seconds=response_settle_seconds,
                        max_settle_wait_seconds=max_settle_wait_seconds,
                        use_ctrl_v=use_ctrl_v,
                        start_new_chat=start_new_chat,
                    )

                    payload = _extract_json_payload(map_response)
                    if payload is None:
                        retry_map_prompt = (
                            f"{map_prompt}\n\n"
                            "IMPORTANT: Output must be a single valid JSON object only. "
                            "No markdown, no explanation."
                        )
                        composer = _find_chat_composer(page, timeout_ms=timeout_ms)
                        if composer is None:
                            raise RuntimeError(
                                f"Could not find composer for map retry chunk {chunk_idx}/{len(input_chunks)}."
                            )
                        map_response = _send_prompt_and_collect_response(
                            page=page,
                            composer=composer,
                            assistant_messages=_assistant_turns(page),
                            prompt_text=retry_map_prompt,
                            chat_url=url,
                            timeout_ms=timeout_ms,
                            wait_seconds=wait_seconds,
                            post_response_wait_seconds=post_response_wait_seconds,
                            response_settle_seconds=response_settle_seconds,
                            max_settle_wait_seconds=max_settle_wait_seconds,
                            use_ctrl_v=use_ctrl_v,
                            start_new_chat=start_new_chat,
                        )
                        payload = _extract_json_payload(map_response)

                    if payload is None:
                        raise RuntimeError(
                            f"Chunk {chunk_idx}/{len(input_chunks)} map output was not valid JSON."
                        )
                    map_payloads.append(payload)

                merged_payload = _merge_map_payloads(map_payloads)
                if start_new_chat:
                    _start_new_chat_if_available(page, timeout_ms=timeout_ms)
                    composer = _find_chat_composer(page, timeout_ms=timeout_ms)
                    if composer is None:
                        raise RuntimeError("Could not find composer for reduce phase.")

                reduce_prompt = _build_reduce_prompt(merged_payload, instructions_text)
                output_text = _send_prompt_and_collect_response(
                    page=page,
                    composer=composer,
                    assistant_messages=_assistant_turns(page),
                    prompt_text=reduce_prompt,
                    chat_url=url,
                    timeout_ms=timeout_ms,
                    wait_seconds=wait_seconds,
                    post_response_wait_seconds=post_response_wait_seconds,
                    response_settle_seconds=response_settle_seconds,
                    max_settle_wait_seconds=max_settle_wait_seconds,
                    use_ctrl_v=use_ctrl_v,
                    start_new_chat=start_new_chat,
                )
                if _looks_like_map_payload_json(output_text):
                    if start_new_chat:
                        _start_new_chat_if_available(page, timeout_ms=timeout_ms)
                        composer = _find_chat_composer(page, timeout_ms=timeout_ms)
                        if composer is None:
                            raise RuntimeError("Could not find composer for reduce markdown retry.")
                    reduce_retry_prompt = (
                        f"{_build_reduce_prompt(merged_payload, instructions_text)}\n\n"
                        "IMPORTANT: Return markdown only. Do not return JSON. "
                        "Do not wrap in triple backticks."
                    )
                    output_text = _send_prompt_and_collect_response(
                        page=page,
                        composer=composer,
                        assistant_messages=_assistant_turns(page),
                        prompt_text=reduce_retry_prompt,
                        chat_url=url,
                        timeout_ms=timeout_ms,
                        wait_seconds=wait_seconds,
                        post_response_wait_seconds=post_response_wait_seconds,
                        response_settle_seconds=response_settle_seconds,
                        max_settle_wait_seconds=max_settle_wait_seconds,
                        use_ctrl_v=use_ctrl_v,
                        start_new_chat=start_new_chat,
                    )
            else:
                all_chunk_outputs: list[str] = []
                response_text = ""
                for chunk_idx, chunk_input_text in enumerate(input_chunks, start=1):
                    current_input_text = chunk_input_text
                    response_text = ""
                    for _ in range(1, iterations + 1):
                        prompt_text = current_input_text
                        if instructions_text:
                            prompt_text = f"{instructions_text}\n\n{current_input_text}"
                        response_text = _send_prompt_and_collect_response(
                            page=page,
                            composer=composer,
                            assistant_messages=_assistant_turns(page),
                            prompt_text=prompt_text,
                            chat_url=url,
                            timeout_ms=timeout_ms,
                            wait_seconds=wait_seconds,
                            post_response_wait_seconds=post_response_wait_seconds,
                            response_settle_seconds=response_settle_seconds,
                            max_settle_wait_seconds=max_settle_wait_seconds,
                            use_ctrl_v=use_ctrl_v,
                            start_new_chat=start_new_chat,
                        )
                        current_input_text = response_text
                    if not response_text:
                        raise RuntimeError(
                            f"Assistant response was empty for chunk {chunk_idx}/{len(input_chunks)}."
                        )
                    all_chunk_outputs.append(response_text)

                if len(all_chunk_outputs) == 1:
                    output_text = all_chunk_outputs[0]
                else:
                    formatted_parts = [
                        f"--- CHUNK {i}/{len(all_chunk_outputs)} RESPONSE ---\n{value}"
                        for i, value in enumerate(all_chunk_outputs, start=1)
                    ]
                    output_text = "\n\n".join(formatted_parts)
            output_file.write_text(output_text + "\n", encoding="utf-8")
        except Exception:
            if error_screenshot is not None:
                try:
                    error_screenshot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(error_screenshot), full_page=True)
                    html_path = error_screenshot.with_suffix(".html")
                    html_path.write_text(page.content(), encoding="utf-8")
                    print(f"Saved failure diagnostics: {error_screenshot} and {html_path}", file=sys.stderr)
                except Exception:
                    pass
            raise
        finally:
            context.close()
            if browser is not None:
                browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send prompt from a file to ChatGPT using Playwright and save the response."
    )
    parser.add_argument("--input-file", required=True, help="Path to input prompt text file.")
    parser.add_argument(
        "--instructions-file",
        help="Optional instructions text file to prepend before input prompt text.",
    )
    parser.add_argument("--output-file", required=True, help="Path to output response text file.")
    parser.add_argument(
        "--email",
        help="Optional ChatGPT account email for login flow when session is not already authenticated.",
    )
    parser.add_argument(
        "--password-b64-env",
        default="MS_PASSWORD_B64",
        help="Environment variable containing base64-encoded ChatGPT password (default: MS_PASSWORD_B64).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of chained ChatGPT runs; each response becomes next input (default: 1).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=60,
        help="Seconds to wait after sending prompt before reading response (default: 60).",
    )
    parser.add_argument(
        "--url",
        default="https://chatgpt.com/",
        help="ChatGPT URL to open (default: https://chatgpt.com/).",
    )
    parser.add_argument("--headless", action="store_true", help="Run browser headless.")
    parser.add_argument(
        "--timeout-ms", type=int, default=60000, help="Timeout in ms for page loads and selector waits."
    )
    parser.add_argument(
        "--user-data-dir",
        default=".playwright-chatgpt-profile",
        help="Persistent Chromium profile dir for keeping ChatGPT login session.",
    )
    parser.add_argument(
        "--error-screenshot",
        default="chatgpt_playwright_error.png",
        help="Screenshot path for failure diagnostics; HTML is saved alongside it.",
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Always run the email/password login flow first before sending prompts.",
    )
    parser.add_argument(
        "--login-url",
        default="https://auth.openai.com/log-in",
        help="Login page URL used when --force-login is enabled.",
    )
    parser.add_argument(
        "--manual-login-wait-seconds",
        type=int,
        default=0,
        help="Optional headed-mode wait for manual login checkpoints before failing.",
    )
    parser.add_argument(
        "--incognito",
        action="store_true",
        help="Use a non-persistent browser context (does not save login/session state).",
    )
    parser.add_argument(
        "--use-ctrl-v",
        action="store_true",
        help="Use clipboard paste (Ctrl+V) to enter prompt text; falls back to direct set if paste fails.",
    )
    parser.add_argument(
        "--network-log",
        action="store_true",
        help="Enable network request/response logging for ChatGPT/API endpoints.",
    )
    parser.add_argument(
        "--network-log-file",
        default="chatgpt_network.log",
        help="Path to network diagnostic log file (default: chatgpt_network.log).",
    )
    parser.add_argument(
        "--chunks",
        type=int,
        default=1,
        help="Split input prompt into this many chunks and process each chunk separately (default: 1).",
    )
    parser.add_argument(
        "--map-reduce",
        action="store_true",
        default=True,
        help="Use map-reduce processing for multi-chunk input to produce one cohesive final response (default: enabled).",
    )
    parser.add_argument(
        "--no-map-reduce",
        dest="map_reduce",
        action="store_false",
        help="Disable map-reduce and keep legacy per-chunk output behavior.",
    )
    parser.add_argument(
        "--finalize-on-last-chunk",
        action="store_true",
        help=(
            "Use exactly N chunk prompts in one chat thread: chunks 1..N-1 are context-only, "
            "and chunk N returns the final cohesive response."
        ),
    )
    parser.add_argument(
        "--start-new-chat",
        action="store_true",
        help="Force creation of a new chat thread before sending prompts (disabled by default).",
    )
    parser.add_argument(
        "--omit-sections",
        default="",
        help="Comma-separated top-level JSON keys to remove from input before sending.",
    )
    parser.add_argument(
        "--post-response-wait-seconds",
        type=int,
        default=10,
        help="Extra seconds to wait after response is detected before final extraction (default: 10).",
    )
    parser.add_argument(
        "--response-settle-seconds",
        type=int,
        default=8,
        help="Seconds the assistant text must remain unchanged before considering it complete (default: 8).",
    )
    parser.add_argument(
        "--max-settle-wait-seconds",
        type=int,
        default=180,
        help="Max seconds to wait for response settling after initial detection (default: 180).",
    )
    return parser.parse_args()


def main() -> int:
    load_env_file_if_present(".env")
    args = parse_args()
    input_file = Path(args.input_file).expanduser().resolve()
    instructions_file = Path(args.instructions_file).expanduser().resolve() if args.instructions_file else None
    output_file = Path(args.output_file).expanduser().resolve()
    user_data_dir = Path(args.user_data_dir).expanduser().resolve()
    error_screenshot = Path(args.error_screenshot).expanduser().resolve() if args.error_screenshot else None
    network_log_file = Path(args.network_log_file).expanduser().resolve() if args.network_log_file else None

    if not input_file.exists():
        print(f"Input file does not exist: {input_file}", file=sys.stderr)
        return 1
    if instructions_file is not None and not instructions_file.exists():
        print(f"Instructions file does not exist: {instructions_file}", file=sys.stderr)
        return 1
    if args.iterations < 1:
        print("--iterations must be at least 1", file=sys.stderr)
        return 1
    if args.manual_login_wait_seconds < 0:
        print("--manual-login-wait-seconds must be >= 0", file=sys.stderr)
        return 1
    if args.chunks < 1:
        print("--chunks must be at least 1", file=sys.stderr)
        return 1
    if args.post_response_wait_seconds < 0:
        print("--post-response-wait-seconds must be >= 0", file=sys.stderr)
        return 1
    if args.response_settle_seconds < 1:
        print("--response-settle-seconds must be at least 1", file=sys.stderr)
        return 1
    if args.max_settle_wait_seconds < 1:
        print("--max-settle-wait-seconds must be at least 1", file=sys.stderr)
        return 1
    omit_sections = [s.strip() for s in args.omit_sections.split(",") if s.strip()]

    password = None
    env_b64 = os.getenv(args.password_b64_env, "").strip()
    if env_b64:
        try:
            password = _decode_password_b64(env_b64)
        except Exception:
            print(
                f"Failed to decode base64 password from env var '{args.password_b64_env}'.",
                file=sys.stderr,
            )
            return 1

    ensure_package("playwright.sync_api")
    try:
        run(
            input_file=input_file,
            instructions_file=instructions_file,
            output_file=output_file,
            email=args.email,
            password=password,
            iterations=args.iterations,
            wait_seconds=args.wait_seconds,
            headless=args.headless,
            timeout_ms=args.timeout_ms,
            user_data_dir=user_data_dir,
            url=args.url,
            error_screenshot=error_screenshot,
            force_login=args.force_login,
            login_url=args.login_url,
            manual_login_wait_seconds=args.manual_login_wait_seconds,
            incognito=args.incognito,
            use_ctrl_v=args.use_ctrl_v,
            network_log_file=network_log_file,
            network_log=args.network_log,
            chunks=args.chunks,
            map_reduce=args.map_reduce,
            finalize_on_last_chunk=args.finalize_on_last_chunk,
            start_new_chat=args.start_new_chat,
            omit_sections=omit_sections,
            post_response_wait_seconds=args.post_response_wait_seconds,
            response_settle_seconds=args.response_settle_seconds,
            max_settle_wait_seconds=args.max_settle_wait_seconds,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "executable doesn't exist" in msg or "please run the following command to download new browsers" in msg:
            ensure_browser_installed()
            run(
                input_file=input_file,
                instructions_file=instructions_file,
                output_file=output_file,
                email=args.email,
                password=password,
                iterations=args.iterations,
                wait_seconds=args.wait_seconds,
                headless=args.headless,
                timeout_ms=args.timeout_ms,
                user_data_dir=user_data_dir,
                url=args.url,
                error_screenshot=error_screenshot,
                force_login=args.force_login,
                login_url=args.login_url,
                manual_login_wait_seconds=args.manual_login_wait_seconds,
                incognito=args.incognito,
                use_ctrl_v=args.use_ctrl_v,
                network_log_file=network_log_file,
                network_log=args.network_log,
                chunks=args.chunks,
                map_reduce=args.map_reduce,
                finalize_on_last_chunk=args.finalize_on_last_chunk,
                omit_sections=omit_sections,
                post_response_wait_seconds=args.post_response_wait_seconds,
                response_settle_seconds=args.response_settle_seconds,
                max_settle_wait_seconds=args.max_settle_wait_seconds,
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    print(f"Saved assistant response to: {output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
