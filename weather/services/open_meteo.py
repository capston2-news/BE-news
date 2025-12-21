# weather/services/open_meteo.py
from __future__ import annotations

from typing import Any, Dict, Optional
import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_TIMEOUT = (4, 10)  # (connect, read)


def geocode(name: str, country_code: str = "VN", count: int = 10, language: str = "vi") -> Dict[str, Any]:
    """
    Open-Meteo Geocoding API: /v1/search?name=...&countryCode=VN&count=...
    """
    name = (name or "").strip()
    if len(name) < 2:
        return {"results": []}

    params = {
        "name": name,
        "count": max(1, min(int(count), 20)),
        "language": language,
        "countryCode": country_code,  # theo docs: countryCode (ISO-3166-1 alpha2)
        "format": "json",
    }
    r = requests.get(GEOCODE_URL, params=params, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json()


def forecast(lat: float, lon: float, timezone: str = "Asia/Ho_Chi_Minh", days: int = 7) -> Dict[str, Any]:
    """
    Open-Meteo Forecast API: /v1/forecast?latitude=..&longitude=..
    - Nếu có daily -> nên set timezone (docs yêu cầu) :contentReference[oaicite:1]{index=1}
    """
    days = max(1, min(int(days), 16))

    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": timezone,  # required when daily is used
        "forecast_days": days,
        "current": ",".join([
            "temperature_2m",
            "relative_humidity_2m",
            "apparent_temperature",
            "is_day",
            "precipitation",
            "weather_code",
            "wind_speed_10m",
            "wind_direction_10m",
        ]),
        "hourly": ",".join([
            "temperature_2m",
            "precipitation_probability",
            "precipitation",
            "weather_code",
            "wind_speed_10m",
        ]),
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
            "weather_code",
            "wind_speed_10m_max",
            "sunrise",
            "sunset",
        ]),
    }

    r = requests.get(FORECAST_URL, params=params, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json()

def current_only(lat: float, lon: float, timezone: str = "Asia/Ho_Chi_Minh"):
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": timezone,
        "current": "temperature_2m,weather_code",
    }
    r = requests.get(FORECAST_URL, params=params, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json()