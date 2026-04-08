from __future__ import annotations

import json
from pathlib import Path

from .prompt_contracts import extract_json_payload
from .stage_runner import run_stage_once, write_stage_outputs


def split_text_into_chunks(text: str, chunks: int) -> list[str]:
    if chunks <= 1:
        return [text]
    cleaned = text.strip()
    if not cleaned:
        return [cleaned]
    n = len(cleaned)
    chunk_size = max(1, (n + chunks - 1) // chunks)
    parts = []
    start = 0
    while start < n:
        end = min(n, start + chunk_size)
        parts.append(cleaned[start:end].strip())
        start = end
    return [p for p in parts if p]


def dedupe_objects(items: list[dict], key_fields: list[str]) -> list[dict]:
    deduped = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key_parts = [str(item.get(field, "")).strip().lower() for field in key_fields]
        key = "|".join(key_parts) if any(key_parts) else json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def merge_map_payloads(payloads: list[dict]) -> dict:
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

    merged["top_findings"] = dedupe_objects(merged["top_findings"], ["title", "category", "severity"])
    merged["actions"] = dedupe_objects(merged["actions"], ["action", "category", "priority"])
    merged["retiring_features"] = dedupe_objects(merged["retiring_features"], ["resource", "detail", "source"])
    merged["orphaned_resources"] = dedupe_objects(merged["orphaned_resources"], ["resource", "resource_type", "source"])
    merged["cost_opportunities"] = dedupe_objects(merged["cost_opportunities"], ["item", "detail", "priority"])
    merged["notes"] = sorted(set(str(n).strip() for n in merged["notes"] if str(n).strip()))
    return merged


def build_chunk_map_prompt(chunk_text: str, chunk_idx: int, total_chunks: int) -> str:
    schema = """{
  "top_findings": [],
  "actions": [],
  "retiring_features": [],
  "orphaned_resources": [],
  "cost_opportunities": [],
  "notes": []
}"""
    return (
        f"You are in map phase for chunk {chunk_idx}/{total_chunks}.\n"
        "Extract only actionable structured data.\n"
        "Return ONLY valid JSON matching the schema below.\n\n"
        f"Schema:\n{schema}\n\nChunk data:\n{chunk_text}"
    )


def build_reduce_prompt(merged_payload: dict, instructions_text: str | None) -> str:
    base_instruction = instructions_text or "Summarize the data into concise markdown with actionable priorities."
    return (
        f"{base_instruction}\n\n"
        "Use ONLY the structured dataset below.\n"
        "Return markdown only.\n\n"
        f"```json\n{json.dumps(merged_payload, indent=2, ensure_ascii=False)}\n```"
    )


def build_context_chunk_prompt(chunk_text: str, chunk_idx: int, total_chunks: int) -> str:
    return (
        f"You are receiving chunk {chunk_idx}/{total_chunks} of a split dataset.\n"
        "Store this chunk for later synthesis.\n"
        "Do not summarize yet.\n"
        f"Reply exactly: ACK {chunk_idx}/{total_chunks}\n\n"
        f"Chunk data:\n{chunk_text}"
    )


def build_final_chunk_prompt(chunk_text: str, chunk_idx: int, total_chunks: int, instructions_text: str | None) -> str:
    base_instruction = instructions_text or "Produce one cohesive final answer."
    return (
        f"{base_instruction}\n\n"
        f"You now have all {total_chunks} chunks in this same chat thread.\n"
        "Use all previous chunks plus this final chunk.\n"
        "Do not return JSON.\n\n"
        f"Final chunk ({chunk_idx}/{total_chunks}) data:\n{chunk_text}"
    )


def run_chunk_plan(
    page,
    prompt_text: str,
    instructions_text: str | None,
    chunks: int,
    mode: str,
    timeout_ms: int,
    wait_seconds: int,
    post_response_wait_seconds: int,
    response_settle_seconds: int,
    max_settle_wait_seconds: int,
) -> str:
    input_chunks = split_text_into_chunks(prompt_text, chunks)
    if len(input_chunks) == 1:
        return run_stage_once(
            page=page,
            timeout_ms=timeout_ms,
            prompt_text=(f"{instructions_text}\n\n{prompt_text}" if instructions_text else prompt_text),
            wait_seconds=wait_seconds,
            post_response_wait_seconds=post_response_wait_seconds,
            response_settle_seconds=response_settle_seconds,
            max_settle_wait_seconds=max_settle_wait_seconds,
        )

    if mode == "finalize_on_last_chunk":
        output_text = ""
        total_chunks = len(input_chunks)
        for chunk_idx, chunk_input_text in enumerate(input_chunks, start=1):
            prompt = (
                build_context_chunk_prompt(chunk_input_text, chunk_idx, total_chunks)
                if chunk_idx < total_chunks
                else build_final_chunk_prompt(chunk_input_text, chunk_idx, total_chunks, instructions_text)
            )
            response = run_stage_once(
                page=page,
                timeout_ms=timeout_ms,
                prompt_text=prompt,
                wait_seconds=wait_seconds,
                post_response_wait_seconds=post_response_wait_seconds,
                response_settle_seconds=response_settle_seconds,
                max_settle_wait_seconds=max_settle_wait_seconds,
            )
            if chunk_idx == total_chunks:
                output_text = response
        return output_text

    if mode == "map_reduce":
        map_payloads = []
        for chunk_idx, chunk_input_text in enumerate(input_chunks, start=1):
            prompt = build_chunk_map_prompt(chunk_input_text, chunk_idx, len(input_chunks))
            response = run_stage_once(
                page=page,
                timeout_ms=timeout_ms,
                prompt_text=prompt,
                wait_seconds=wait_seconds,
                post_response_wait_seconds=post_response_wait_seconds,
                response_settle_seconds=response_settle_seconds,
                max_settle_wait_seconds=max_settle_wait_seconds,
            )
            payload = extract_json_payload(response)
            if payload is None:
                raise RuntimeError(f"Chunk {chunk_idx}/{len(input_chunks)} map output was not valid JSON.")
            map_payloads.append(payload)
        merged = merge_map_payloads(map_payloads)
        reduce_prompt = build_reduce_prompt(merged, instructions_text)
        return run_stage_once(
            page=page,
            timeout_ms=timeout_ms,
            prompt_text=reduce_prompt,
            wait_seconds=wait_seconds,
            post_response_wait_seconds=post_response_wait_seconds,
            response_settle_seconds=response_settle_seconds,
            max_settle_wait_seconds=max_settle_wait_seconds,
        )

    # legacy multi-chunk
    outputs = []
    for chunk_text in input_chunks:
        prompt = f"{instructions_text}\n\n{chunk_text}" if instructions_text else chunk_text
        outputs.append(
            run_stage_once(
                page=page,
                timeout_ms=timeout_ms,
                prompt_text=prompt,
                wait_seconds=wait_seconds,
                post_response_wait_seconds=post_response_wait_seconds,
                response_settle_seconds=response_settle_seconds,
                max_settle_wait_seconds=max_settle_wait_seconds,
            )
        )
    if len(outputs) == 1:
        return outputs[0]
    return "\n\n".join(f"--- CHUNK {i}/{len(outputs)} RESPONSE ---\n{value}" for i, value in enumerate(outputs, start=1))
