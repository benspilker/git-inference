#!/usr/bin/env python3
"""One-query Google Playwright smoke test intended to run under Xvfb."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import async_playwright


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="hello world")
    parser.add_argument("--artifacts-dir", type=Path, default=Path("artifacts/google-xvfb-probe"))
    parser.add_argument("--timeout-ms", type=int, default=45000)
    return parser.parse_args()


async def extract_results(page) -> list[dict]:
    """Use the same organic-anchor selectors as the website lead-hunting skill."""
    data = await page.evaluate("""() => {
      const root = document.querySelector('#rso') || document.querySelector('#search') || document.body;
      const visible = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
      const cards = [...root.querySelectorAll('.tF2Cxc')];
      const containers = cards.length ? cards : [...root.querySelectorAll('.MjjYud')];
      return containers.map(box => {
        const a = box.querySelector('a:has(h3)') || [...box.querySelectorAll('a[href]')]
          .find(link => (link.innerText || link.textContent || '').trim().length > 8
                       && !/^(read more|about us|navigation|services)$/i.test((link.innerText || '').trim()));
        if (!a) return null;
        const heading = box.querySelector('h3');
        return {href: a.href, text: ((heading && heading.innerText) || a.innerText || a.textContent || '').trim(),
                visible: visible(a)};
      }).filter(x => x && x.visible && x.href && /^https?:/.test(x.href) && x.text.length > 2);
    }""")

    results, seen = [], set()
    for item in data:
        href = item.get("href", "")
        if href in seen:
            continue
        seen.add(href)
        parts = urlsplit(href)
        if parts.hostname and parts.hostname.endswith("google.com") and parts.path == "/url":
            query = parse_qs(parts.query)
            href = (query.get("q") or query.get("url") or [""])[0]
            parts = urlsplit(href)
        host = (parts.hostname or "").lower().removeprefix("www.")
        if host == "google.com" or host.endswith(".google.com"):
            continue
        results.append({"title": item.get("text", "")[:250], "domain": host, "url": href})
    return results


async def challenge_evidence(page, url: str, title: str, body: str) -> list[str]:
    evidence = []
    if urlsplit(url).path.lower().startswith("/sorry/"):
        evidence.append("Google /sorry/ challenge URL")
    lowered = body.lower()
    if "our systems have detected unusual traffic" in lowered or "unusual traffic from your computer network" in lowered:
        evidence.append("visible unusual-traffic challenge text")
    prompt = any(text in lowered for text in (
        "verify you are not a robot", "verify you're not a robot", "confirm you are not a robot",
        "select all squares with", "complete the captcha",
    ))
    widget = False
    frames = page.locator("iframe[src*='recaptcha'], iframe[title*='reCAPTCHA']")
    for index in range(min(await frames.count(), 5)):
        if await frames.nth(index).is_visible():
            widget = True
            break
    if not widget:
        try:
            widget = await page.locator("#captcha-form, #recaptcha, [data-sitekey]").first.is_visible()
        except Exception:
            widget = False
    if prompt and widget:
        evidence.append("visible verification prompt and challenge widget")
    if "sorry" in title.lower() and prompt:
        evidence.append("challenge title and verification prompt")
    return evidence


async def main() -> int:
    args = parse_args()
    if not args.query.strip():
        raise ValueError("query must not be empty")
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.artifacts_dir / "result.json"
    screenshot_path = args.artifacts_dir / "google-page.png"
    search_url = "https://www.google.com/search?" + urlencode({"q": args.query})
    result = {
        "query": args.query,
        "requested_url": search_url,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "browser_mode": "headed Chromium under Xvfb",
        "status": "error",
    }

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=False)
            try:
                context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
                page = await context.new_page()
                response = await page.goto(search_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                await page.wait_for_timeout(2000)
                title = await page.title()
                final_url = page.url
                body = await page.locator("body").inner_text(timeout=10000)
                selectors = await page.evaluate("""() => ({
                  rso: !!document.querySelector('#rso'),
                  search: !!document.querySelector('#search'),
                  tF2Cxc: document.querySelectorAll('.tF2Cxc').length,
                  MjjYud: document.querySelectorAll('.MjjYud').length,
                  h3: document.querySelectorAll('h3').length
                })""")
                challenges = await challenge_evidence(page, final_url, title, body)
                results = [] if challenges else await extract_results(page)
                await page.screenshot(path=str(screenshot_path), full_page=True, timeout=15000)
                result.update({
                    "http_status": response.status if response else None,
                    "final_url": final_url,
                    "page_title": title,
                    "challenge_evidence": challenges,
                    "selectors": selectors,
                    "organic_results_extracted": len(results),
                    "sample_results": results[:5],
                    "screenshot": screenshot_path.name,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                if challenges:
                    result["status"] = "blocked"
                    print("Google presented a challenge; stopping without interaction.", flush=True)
                    return_code = 2
                elif results:
                    result["status"] = "results_found"
                    return_code = 0
                else:
                    result["status"] = "no_results_extracted"
                    return_code = 3
                return return_code
            finally:
                await browser.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        print(result["error"], file=sys.stderr)
        return_code = 4
    finally:
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
