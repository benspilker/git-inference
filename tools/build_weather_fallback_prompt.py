#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build weather JSON + prompt for fallback summarization.")
    parser.add_argument("--question-file", required=True, help="Path to extracted user question text.")
    parser.add_argument("--out-prompt-file", required=True, help="Path to write generated summarization prompt.")
    parser.add_argument("--out-data-file", required=True, help="Path to write fetched weather JSON snapshot.")
    return parser.parse_args()


def extract_question(raw: str) -> str:
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and ln.strip() != "```"]
    question = lines[-1] if lines else raw.strip()
    question = re.sub(r"^\[[^\]]+\]\s*", "", question).strip()
    return question


def extract_location(question: str) -> str:
    patterns = [
        r"\b(?:weather|forecast|temperature)\b(?:[^a-zA-Z]+(?:today|tonight|now|currently|this week))?(?:[^a-zA-Z]+\b(?:in|for|at)\b)\s+([A-Za-z .'\-]+(?:,\s*[A-Za-z .'\-]+)?)",
        r"\b(?:in|for|at)\b\s+([A-Za-z .'\-]+(?:,\s*[A-Za-z .'\-]+)?)\s*\??$",
    ]
    for pattern in patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            return match.group(1).strip(" ?.,")
    return ""


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def build_snapshot(location: str) -> dict:
    candidates: list[str] = []
    base = location.strip()
    if base:
        candidates.append(base)
    if "," in base:
        city_only = base.split(",", 1)[0].strip()
        if city_only and city_only not in candidates:
            candidates.append(city_only)

    geo = {}
    results = []
    chosen_query = ""
    for candidate in candidates:
        geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": candidate, "count": 1, "language": "en", "format": "json"}
        )
        geo = fetch_json(geo_url)
        results = geo.get("results") or []
        if results:
            chosen_query = candidate
            break
    if not results:
        raise RuntimeError(f"No geocoding result for location: {location}")

    top = results[0]
    lat = top["latitude"]
    lon = top["longitude"]
    resolved_name = ", ".join([str(x) for x in [top.get("name"), top.get("admin1"), top.get("country")] if x])

    forecast_params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "precipitation",
                "rain",
                "snowfall",
                "weather_code",
                "wind_speed_10m",
                "wind_direction_10m",
            ]
        ),
        "daily": ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "rain_sum",
                "snowfall_sum",
                "precipitation_probability_max",
                "wind_speed_10m_max",
            ]
        ),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "forecast_days": 1,
    }
    forecast_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(forecast_params)
    weather = fetch_json(forecast_url)

    return {
        "fetched_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "open-meteo",
        "geocode_query_used": chosen_query or location,
        "resolved_location": resolved_name,
        "query_location": location,
        "coords": {"latitude": lat, "longitude": lon},
        "timezone": weather.get("timezone"),
        "utc_offset_seconds": weather.get("utc_offset_seconds"),
        "current_units": weather.get("current_units"),
        "current": weather.get("current"),
        "daily_units": weather.get("daily_units"),
        "daily": weather.get("daily"),
    }


def build_prompt(question: str, snapshot: dict) -> str:
    return (
        "You already have live weather data in JSON below.\n"
        "Do not perform web search.\n"
        "Do not say WEB_SEARCH_UNAVAILABLE.\n"
        "Use only the provided JSON and answer as Juniper in concise natural language.\n\n"
        f"User question:\n{question}\n\n"
        "Weather data JSON:\n"
        + json.dumps(snapshot, indent=2, ensure_ascii=False)
        + "\n\n"
        "Return a short weather answer with:\n"
        "- current conditions\n"
        "- today's high/low\n"
        "- precipitation risk\n"
        "- one practical note."
    )


def main() -> int:
    args = parse_args()
    question_path = Path(args.question_file)
    out_prompt_path = Path(args.out_prompt_file)
    out_data_path = Path(args.out_data_file)

    raw = question_path.read_text(encoding="utf-8")
    question = extract_question(raw)
    location = extract_location(question)
    if not location:
        raise RuntimeError("Could not infer a location from weather question.")

    snapshot = build_snapshot(location)
    out_data_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_prompt_path.write_text(build_prompt(question, snapshot), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
