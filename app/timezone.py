"""Timezone helpers for East Africa Time (EAT, UTC+03:00)."""
from datetime import datetime, date
from zoneinfo import ZoneInfo

EAT = ZoneInfo("Africa/Nairobi")


def eat_now() -> datetime:
    """Return current East African time as a naive datetime for DB compatibility."""
    return datetime.now(EAT).replace(tzinfo=None)


def eat_today() -> date:
    """Return current date in East African time."""
    return datetime.now(EAT).date()
