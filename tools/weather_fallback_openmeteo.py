#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def extract_location(text: str) -> str:
    patterns = [
        r"weather(?:\s+like)?(?:\s+today|\s+now)?\s+in\s+([A-Za-z0-9 .,'-]+)",
        r"\bin\s+([A-Za-z0-9 .,'-]+)(?:\?|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1).strip(" .,\n\t?")
            if candidate:
                return candidate
    return "Indianapolis, Indiana"


def c_to_f(value: float | None) -> float | None:
    if value is None:
        return None
    return (value * 9 / 5) + 32


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: weather_fallback_openmeteo.py <question_file> <output_file>", file=sys.stderr)
        return 2

    question_file = Path(sys.argv[1])
    output_file = Path(sys.argv[2])
    question = question_file.read_text(encoding="utf-8", errors="ignore")

    location = extract_location(question)
    geo_url = (
        "https://geocoding-api.open-meteo.com/v1/search?"
        + urllib.parse.urlencode({"name": location, "count": 1, "language": "en", "format": "json"})
    )
    with urllib.request.urlopen(geo_url, timeout=20) as resp:
        geo_payload = json.loads(resp.read().decode("utf-8"))

    results = geo_payload.get("results") or []
    if not results:
        raise RuntimeError(f"Could not geocode location: {location}")
    best = results[0]

    lat = best["latitude"]
    lon = best["longitude"]
    display_name_parts = [best.get("name"), best.get("admin1"), best.get("country")]
    display_name = ", ".join([p for p in display_name_parts if isinstance(p, str) and p.strip()])

    weather_url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode(
            {
                "latitude": lat,
                "longitude": lon,
                "timezone": "auto",
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "forecast_days": 1,
            }
        )
    )
    with urllib.request.urlopen(weather_url, timeout=20) as resp:
        weather_payload = json.loads(resp.read().decode("utf-8"))

    current = weather_payload.get("current", {})
    daily = weather_payload.get("daily", {})
    codes = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Depositing rime fog",
        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",
        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",
        71: "Slight snow",
        73: "Moderate snow",
        75: "Heavy snow",
        80: "Rain showers",
        81: "Rain showers",
        82: "Heavy rain showers",
        95: "Thunderstorm",
        96: "Thunderstorm with hail",
        99: "Thunderstorm with hail",
    }

    code = current.get("weather_code")
    description = codes.get(code, f"Weather code {code}")
    temp_c = current.get("temperature_2m")
    wind_kmh = current.get("wind_speed_10m")
    tmax = (daily.get("temperature_2m_max") or [None])[0]
    tmin = (daily.get("temperature_2m_min") or [None])[0]

    parts = [f"Weather for {display_name}:"]
    if temp_c is not None:
        parts.append(f"Current {temp_c:.1f}C ({c_to_f(temp_c):.1f}F), {description}.")
    else:
        parts.append(f"Current conditions: {description}.")
    if tmax is not None and tmin is not None:
        parts.append(f"Today high/low: {tmax:.1f}C/{tmin:.1f}C ({c_to_f(tmax):.1f}F/{c_to_f(tmin):.1f}F).")
    if wind_kmh is not None:
        parts.append(f"Wind: {wind_kmh:.1f} km/h.")
    parts.append("Source: Open-Meteo live API on " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    output_file.write_text(" ".join(parts) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
