#!/usr/bin/env python3
"""Validate Duck.ai target rows and publish a GitHub Actions matrix."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
TARGET_FILE = ROOT / "source_urls.csv"


def _clean(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def read_targets() -> list[dict[str, str]]:
    run_mode = _clean(os.environ.get("RUN_MODE")) or "full_batch"
    smoke_website = _clean(os.environ.get("SMOKE_WEBSITE"))
    if run_mode not in {"full_batch", "smoke_test"}:
        raise RuntimeError("RUN_MODE must be full_batch or smoke_test.")
    manual_url = _clean(os.environ.get("SOURCE_URL"))
    manual_business = _clean(os.environ.get("BUSINESS_NAME"))
    manual_category = _clean(os.environ.get("INDUSTRY_CATEGORY"))
    manual_location = _clean(os.environ.get("LOCATION"))
    if manual_url:
        if run_mode == "smoke_test":
            raise RuntimeError("CSV smoke mode does not accept a manual source_url; select a CSV row with smoke_website.")
        if smoke_website:
            raise RuntimeError("smoke_website can only be used in smoke_test mode.")
        parsed = urlparse(manual_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or any(char.isspace() for char in manual_url):
            raise RuntimeError("Manual source_url must be an absolute HTTP or HTTPS URL.")
        if not manual_business or not manual_category:
            raise RuntimeError("Manual runs with source_url also require business_name and industry_category.")
        return [{
            "site_index": "1",
            "business": manual_business,
            "category": manual_category,
            "location": manual_location,
            "source_url": manual_url,
        }]
    if manual_business or manual_category or manual_location:
        raise RuntimeError("business_name, industry_category, and location can only be used with a manual source_url.")
    if run_mode == "full_batch" and smoke_website:
        raise RuntimeError("smoke_website can only be used in smoke_test mode.")
    if not TARGET_FILE.is_file():
        raise RuntimeError(f"Target CSV not found: {TARGET_FILE}")
    with TARGET_FILE.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = {name.strip().lower() for name in (reader.fieldnames or [])}
        required = {"website", "business", "category"}
        missing = sorted(required - headers)
        if missing:
            raise RuntimeError(f"Target CSV is missing required columns: {', '.join(missing)}")
        targets = []
        seen_urls: set[str] = set()
        for row_number, row in enumerate(reader, start=2):
            normalized = {str(key).strip().lower(): value for key, value in row.items() if key}
            url = _clean(normalized.get("website"))
            business = _clean(normalized.get("business"))
            category = _clean(normalized.get("category"))
            if not any((url, business, category)):
                continue
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or any(char.isspace() for char in url):
                raise RuntimeError(f"Row {row_number}: website must be an absolute HTTP or HTTPS URL.")
            if not business:
                raise RuntimeError(f"Row {row_number}: business is required.")
            if not category:
                raise RuntimeError(f"Row {row_number}: category is required.")
            key = url.rstrip("/").lower()
            if key in seen_urls:
                raise RuntimeError(f"Row {row_number}: duplicate website URL {url!r}.")
            seen_urls.add(key)
            targets.append(
                {
                    "site_index": str(len(targets) + 1),
                    "business": business,
                    "category": category,
                    "location": _clean(normalized.get("location")),
                    "source_url": url,
                }
            )
    if not targets:
        raise RuntimeError("Target CSV has no usable website rows.")
    if len(targets) > 256:
        raise RuntimeError(f"The target CSV contains {len(targets)} websites; GitHub Actions matrices support at most 256.")
    if run_mode == "smoke_test":
        if smoke_website:
            requested = smoke_website.rstrip("/").lower()
            matches = [target for target in targets if target["source_url"].rstrip("/").lower() == requested]
            if len(matches) != 1:
                raise RuntimeError("smoke_website must match exactly one website value in source_urls.csv.")
            targets = matches
        else:
            targets = targets[:1]
        targets[0]["site_index"] = "1"
    return targets


def main() -> None:
    targets = read_targets()
    matrix = json.dumps(targets, ensure_ascii=False, separators=(",", ":"))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"targets={matrix}\n")
    print(f"Validated {len(targets)} target(s).")
    for target in targets:
        print(
            f"Target {target['site_index']}: {target['business']} | "
            f"{target['category']} | {target['source_url']}"
        )


if __name__ == "__main__":
    main()
