from datetime import timezone
from zoneinfo import ZoneInfo


def resolve_timezone(name: str):
    return timezone.utc if name == "UTC" else ZoneInfo(name)
