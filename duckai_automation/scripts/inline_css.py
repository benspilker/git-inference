#!/usr/bin/env python3
"""Embed the generated styles.css into a sibling inlined/index.html."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


STYLESHEET_LINK = re.compile(
    r"<link\b(?=[^>]*\brel\s*=\s*(['\"])stylesheet\1)"
    r"(?=[^>]*\bhref\s*=\s*(['\"])styles\.css\2)[^>]*>",
    re.IGNORECASE,
)
STYLE_ELEMENT = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path, help="Assembled source HTML")
    parser.add_argument("css", type=Path, help="Assembled stylesheet")
    parser.add_argument("output", type=Path, help="Standalone HTML output path")
    args = parser.parse_args()

    html_path = args.html.resolve()
    css_path = args.css.resolve()
    output_path = args.output.resolve()
    if html_path == output_path:
        parser.error("output must not overwrite the original HTML")
    if not html_path.is_file() or not css_path.is_file():
        parser.error("both source HTML and CSS files must exist")

    html = html_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8").strip()
    if not css:
        parser.error("stylesheet is empty")
    if re.search(r"</style\s*>", css, re.IGNORECASE):
        parser.error("stylesheet contains a closing style tag and cannot be embedded safely")

    links = list(STYLESHEET_LINK.finditer(html))
    if len(links) > 1:
        parser.error(f"expected at most one link to styles.css; found {len(links)}")

    existing_styles = [block.strip() for block in STYLE_ELEMENT.findall(html)]
    original_style_count = len(STYLE_ELEMENT.findall(html))
    style_element = f"<style>\n{css}\n</style>"
    if css in existing_styles:
        inlined = STYLESHEET_LINK.sub("", html, count=1) if links else html
    elif links:
        inlined = STYLESHEET_LINK.sub(lambda _: style_element, html, count=1)
    else:
        head_close = list(re.finditer(r"</head\s*>", html, re.IGNORECASE))
        if len(head_close) != 1:
            parser.error("no styles.css link was found and HTML must have exactly one closing </head> for CSS insertion")
        insertion = head_close[0].start()
        inlined = html[:insertion] + style_element + "\n" + html[insertion:]

    if STYLESHEET_LINK.search(inlined):
        parser.error("styles.css link remains after inlining")
    style_blocks = STYLE_ELEMENT.findall(inlined)
    expected_style_count = original_style_count if css in existing_styles else original_style_count + 1
    if len(style_blocks) != expected_style_count or not any(block.strip() == css for block in style_blocks):
        parser.error("inlined stylesheet verification failed")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(inlined, encoding="utf-8")
    print(f"Created {output_path} ({output_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
