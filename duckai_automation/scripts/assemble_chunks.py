"""Verify six captured Duck.ai chunks and assemble index.html/styles.css."""

import argparse
import hashlib
import json
import re
from pathlib import Path


EXPECTED = [(kind, part) for kind in ("css", "html") for part in range(1, 4)]


def fail(message):
    raise SystemExit(f"assembly rejected: {message}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path)
    args = parser.parse_args()
    run = args.run_folder.resolve()
    chunks = run / "chunks"
    manifest_path = chunks / "capture_manifest.json"
    if not manifest_path.is_file():
        fail("capture manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parts = manifest.get("parts", [])
    actual = [(item.get("kind"), item.get("part")) for item in parts]
    if actual != EXPECTED:
        fail(f"expected six parts in CSS 1-3 then HTML 1-3 order; found {actual}")
    css, html = [], []
    for entry in parts:
        path = run / entry["chunk_file"]
        if not path.is_file():
            fail(f"chunk file is missing: {path}")
        code = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if digest != entry.get("sha256"):
            fail(f"chunk changed since capture: {path.name}")
        kind, part = entry["kind"], entry["part"]
        expected_marker = (f"/* CSS PART {part} OF 3 */" if kind == "css"
                           else f"<!-- HTML PART {part} OF 3 -->")
        first_nonblank = next((line.strip() for line in code.splitlines() if line.strip()), "")
        if first_nonblank != expected_marker:
            fail(f"part marker mismatch: {path.name}")
        (css if kind == "css" else html).append(code.rstrip() + "\n")
    html_text = "\n".join(html)
    if not re.search(r"</body>\s*</html>\s*$", html_text, re.IGNORECASE):
        fail("assembled HTML does not end with </body></html>")
    files = run / "files"
    files.mkdir(parents=True, exist_ok=True)
    (files / "styles.css").write_text("\n".join(css), encoding="utf-8")
    (files / "index.html").write_text(html_text, encoding="utf-8")
    print(json.dumps({"assembled": True, "chat_id": manifest.get("chat_id"), "css_parts": 3, "html_parts": 3,
                      "index_html_bytes": (files / "index.html").stat().st_size,
                      "styles_css_bytes": (files / "styles.css").stat().st_size}))


if __name__ == "__main__":
    main()
