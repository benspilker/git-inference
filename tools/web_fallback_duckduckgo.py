#!/usr/bin/env python3
from __future__ import annotations

import html
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def normalize_result_url(url: str) -> str:
    raw = (url or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    if "duckduckgo.com/l/?" in raw:
        parsed = urllib.parse.urlparse(raw)
        q = urllib.parse.parse_qs(parsed.query)
        uddg = (q.get("uddg") or [""])[0].strip()
        if uddg:
            return urllib.parse.unquote(uddg)
    return raw


def parse_results(search_html: str, limit: int = 5) -> list[dict]:
    results: list[dict] = []
    blocks = re.split(r'<div[^>]+class="[^"]*result[^"]*"[^>]*>', search_html, flags=re.IGNORECASE)
    for block in blocks:
        anchor = re.search(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not anchor:
            continue
        href = normalize_result_url(anchor.group(1))
        title = strip_tags(anchor.group(2))
        if not href or not title:
            continue

        snippet_match = re.search(
            r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>|<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        snippet = ""
        if snippet_match:
            snippet = strip_tags(snippet_match.group(1) or snippet_match.group(2) or "")

        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def search_duckduckgo(query: str) -> list[dict]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return parse_results(raw, limit=5)


def build_text_response(query: str, results: list[dict]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not results:
        return (
            f'I could not extract live web results for "{query}". '
            f"Source attempted: DuckDuckGo HTML at {timestamp}."
        )

    lines = [
        f'Live web fallback results for "{query}":',
    ]
    for idx, item in enumerate(results, start=1):
        line = f"{idx}. {item['title']}"
        if item.get("snippet"):
            line += f" — {item['snippet']}"
        line += f" (Source: {item['url']})"
        lines.append(line)
    lines.append(f"Retrieved via DuckDuckGo HTML on {timestamp}.")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: web_fallback_duckduckgo.py <question_file> <output_file>", file=sys.stderr)
        return 2

    question_file = Path(sys.argv[1])
    output_file = Path(sys.argv[2])
    query = question_file.read_text(encoding="utf-8", errors="ignore").strip()
    if not query:
        raise RuntimeError("Question file is empty.")

    results = search_duckduckgo(query)
    output_file.write_text(build_text_response(query, results) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

