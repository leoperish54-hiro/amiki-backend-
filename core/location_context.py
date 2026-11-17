"""Validation and minimisation for optional, time-limited device location context."""

from datetime import datetime, timezone
from typing import Any, Mapping

MAX_LOCATION_AGE_SECONDS = 15 * 60
MAX_FUTURE_SKEW_SECONDS = 60


def format_location_context(value: Any, *, now: datetime | None = None) -> str | None:
    """Return a minimised prompt fragment only for a fresh, valid location."""
    if not isinstance(value, Mapping):
        return None
    try:
        latitude = float(value["latitude"])
        longitude = float(value["longitude"])
        accuracy = float(value["accuracy_meters"])
        timestamp = datetime.fromisoformat(str(value["timestamp"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
        timestamp = timestamp.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    if not (0 < accuracy <= 10000):
        return None
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - timestamp).total_seconds()
    if age > MAX_LOCATION_AGE_SECONDS or age < -MAX_FUTURE_SKEW_SECONDS:
        return None

    # Round coordinates to neighbourhood-scale precision before model use.
    parts = [
        f"Approximate coordinates: {latitude:.3f}, {longitude:.3f}",
        f"reported accuracy: about {round(accuracy)} metres",
        f"captured: {timestamp.isoformat()} UTC",
    ]
    country_code = value.get("country_code")
    if isinstance(country_code, str) and len(country_code) == 2 and country_code.isalpha():
        parts.append(f"country code: {country_code.upper()}")
    for key, label, limit in (("country_name", "country", 80), ("locality", "local area", 100)):
        item = value.get(key)
        if isinstance(item, str):
            safe_item = " ".join(item.split())
            safe_item = "".join(ch for ch in safe_item if ch.isalnum() or ch in " .,'-")
            if safe_item:
                parts.append(f"{label}: {safe_item[:limit]}")
    return "; ".join(parts) + "."
