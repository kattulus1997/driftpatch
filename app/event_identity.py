from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5


def daily_event_id(scenario_id: str, day: date) -> str:
    """Return the sole event identifier authorized for a scenario and UTC day."""
    return str(uuid5(NAMESPACE_URL, f"driftpatch:public-demo:{day}:{scenario_id}"))


def parse_issued_day(value: str, *, now: datetime | None = None) -> date:
    try:
        issued_day = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("issued_day must use YYYY-MM-DD") from exc

    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    if issued_day not in {today, today - timedelta(days=1)}:
        raise ValueError("issued_day must be today or yesterday in UTC")
    return issued_day


def task_id(event_id: str, attempt_id: str) -> str:
    """Return a Cloud Tasks-compatible identity unique to one durable attempt."""
    return f"run-{event_id}-{attempt_id}"
