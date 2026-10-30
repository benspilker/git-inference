"""Extract one labeled code fence from a saved Duck.ai assistant response."""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


EXPECTED = [(kind, part) for kind in ("css", "html") for part in range(1, 4)]
FENCE = re.compile(r"```([A-Za-z0-9_-]+)[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)


def fail(message):
    raise SystemExit(f"capture rejected: {message}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path)
    parser.add_argument("response_text", type=Path, help="Text captured from the newest assistant message only")
    parser.add_argument("--kind", choices=("css", "html"), required=True)
    parser.add_argument("--part", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--replace", action="store_true", help="Replace a previously accepted part after a targeted Duck.ai repair")
    args = parser.parse_args()

    run = args.run_folder.resolve()
    response_path = args.response_text.resolve()
    if not run.is_dir():
        fail(f"run folder does not exist: {run}")
    if not response_path.is_file():
        fail(f"response text does not exist: {response_path}")
    response = response_path.read_text(encoding="utf-8-sig")
    fences = FENCE.findall(response)
    if len(fences) != 1:
        fail(f"expected exactly one fenced code block; found {len(fences)}")
    language, code = fences[0]
    expected_language = "css" if args.kind == "css" else "text"
    if language.lower() != expected_language:
        fail(f"expected a {expected_language} fence, found {language!r}")
    code = code.strip("\r\n") + "\n"
    expected_marker = (f"/* CSS PART {args.part} OF 3 */" if args.kind == "css"
                       else f"<!-- HTML PART {args.part} OF 3 -->")
    first_nonblank = next((line.strip() for line in code.splitlines() if line.strip()), "")
    if first_nonblank != expected_marker:
        fail("in-code part marker is missing or mismatched; likely stale or mislabeled response")
    visible_label = re.search(rf"\b{args.kind}\s+part\s+{args.part}\s+of\s+3\b", response, re.IGNORECASE)
    if not visible_label:
        fail("response text does not identify the expected part")
    if len(code) < 100:
        fail("code block is suspiciously short or truncated")

    chunks = run / "chunks"
    responses = chunks / "responses"
    responses.mkdir(parents=True, exist_ok=True)
    manifest_path = chunks / "capture_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"chat_id": args.chat_id, "parts": []}
    if manifest.get("chat_id") != args.chat_id:
        fail("chat ID differs from the candidate's existing capture manifest")
    key = (args.kind, args.part)
    old_entry = next((entry for entry in manifest["parts"] if (entry["kind"], entry["part"]) == key), None)
    if old_entry and not args.replace:
        fail("this part was already accepted; refusing to overwrite it")

    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if any(entry["sha256"] == digest for entry in manifest["parts"] if entry is not old_entry):
        fail("code hash duplicates an already accepted part")
    if old_entry and digest == old_entry.get("sha256"):
        fail("replacement is identical to the accepted part")
    response_copy = responses / f"{args.kind}-{args.part}.txt"
    chunk_path = chunks / f"{args.kind}-{args.part}.{'css' if args.kind == 'css' else 'html'}"
    same_response_path = response_path == response_copy.resolve()
    if not old_entry and ((response_copy.exists() and not same_response_path) or chunk_path.exists()):
        fail("destination already exists; refusing to overwrite")
    if old_entry:
        revisions = chunks / "revisions"
        revisions.mkdir(parents=True, exist_ok=True)
        old_chunk = run / old_entry["chunk_file"]
        if old_chunk.exists():
            old_chunk.replace(revisions / f"{args.kind}-{args.part}-{old_entry['sha256'][:12]}.{old_chunk.suffix.lstrip('.')}")
        old_response = run / old_entry["response_file"]
        if old_response.exists():
            old_response.replace(revisions / f"{args.kind}-{args.part}-{old_entry['sha256'][:12]}.txt")
    if not same_response_path:
        response_copy.write_text(response, encoding="utf-8")
    chunk_path.write_text(code, encoding="utf-8")
    new_entry = {
        "kind": args.kind,
        "part": args.part,
        "response_file": str(response_copy.relative_to(run)),
        "chunk_file": str(chunk_path.relative_to(run)),
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "characters": len(code),
        "sha256": digest,
    }
    if old_entry:
        new_entry["revision_of"] = old_entry.get("sha256")
        manifest["parts"].remove(old_entry)
    manifest["parts"].append(new_entry)
    manifest["parts"].sort(key=lambda item: EXPECTED.index((item["kind"], item["part"])))
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"accepted": True, "part": f"{args.kind} {args.part}/3", "characters": len(code), "sha256": digest}))


if __name__ == "__main__":
    main()
