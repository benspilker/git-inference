from __future__ import annotations

import json
import time
from pathlib import Path


def enable_network_logging(page, log_file: Path, enabled: bool = True) -> None:
    if not enabled:
        return

    log_file.parent.mkdir(parents=True, exist_ok=True)

    def append(line: str) -> None:
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

    def is_interesting(url: str) -> bool:
        return any(fragment in url for fragment in interesting_fragments)

    def on_request(request):
        url = request.url
        if is_interesting(url):
            append(f"REQ {request.method} {url}")

    def on_response(response):
        url = response.url
        if not is_interesting(url):
            return
        append(f"RES {response.status} {response.request.method} {url}")
        if response.status >= 400:
            try:
                body = response.text()
            except Exception:
                body = ""
            body = (body or "").replace("\n", " ").strip()
            if len(body) > 600:
                body = body[:600] + "...<truncated>"
            if body:
                append(f"ERR_BODY {body}")

    page.on("request", on_request)
    page.on("response", on_response)
    append("Network logging enabled.")


def save_failure_diagnostics(
    page,
    error_screenshot: Path | None,
    metadata: dict | None = None,
    html_path: Path | None = None,
) -> None:
    if error_screenshot is None:
        return
    try:
        error_screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(error_screenshot), full_page=True)
        resolved_html_path = html_path or error_screenshot.with_suffix(".html")
        resolved_html_path.write_text(page.content(), encoding="utf-8")
        if metadata is not None:
            meta_path = error_screenshot.with_suffix(".meta.json")
            meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass
