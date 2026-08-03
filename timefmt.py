from __future__ import annotations

from datetime import datetime, timezone

from config import cfg


def local_dt(value: str | None) -> str:
    """Render a stored UTC timestamp in the shop's timezone.

    Falls back to UTC if the zone name is unknown or the tz database isn't
    installed, which is the common case on a bare Windows Python.
    """
    if not value:
        return "—"
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return value
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo(cfg.timezone))
    except Exception:
        pass
    offset = dt.strftime("%z") or "+0000"
    hours = int(offset[:3])
    mins = int(offset[0] + offset[3:])
    label = f"UTC{hours:+d}" + (f":{abs(mins):02d}" if mins else "")
    return dt.strftime("%b %d, %Y, %I:%M %p ") + f"({label})"
