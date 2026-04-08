from __future__ import annotations

import json
import re
from pathlib import Path


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


def find_balanced_json_block(text: str) -> str | None:
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


def extract_json_payload(text: str) -> dict | None:
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
    raw_block = find_balanced_json_block(text)
    if not raw_block:
        return None
    try:
        parsed = json.loads(raw_block)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None
