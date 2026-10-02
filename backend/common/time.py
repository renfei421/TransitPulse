"""Half-open UTC processing windows and inclusive local-date query boundaries."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def timestamp(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z","+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def day_bounds(start, end, zone="Australia/Sydney"):
    """Public API dates are inclusive calendar dates in the stated local zone."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if a > b:
        raise ValueError("from must not be after to")
    tz = ZoneInfo(zone)
    return (
        datetime.combine(a,datetime.min.time(),tzinfo=tz).astimezone(timezone.utc).isoformat(),
        datetime.combine(b+timedelta(days=1),datetime.min.time(),tzinfo=tz).astimezone(timezone.utc).isoformat(),
    )


def previous_day(now=None, zone="Australia/Sydney"):
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone))
    value = (local.date()-timedelta(days=1)).isoformat()
    return day_bounds(value,value,zone)


def resolve_range(start=None, end=None, window_hours=None):
    if window_hours is not None:
        if start or window_hours <= 0:
            raise ValueError("Positive window_hours cannot be combined with start")
        b = timestamp(end) if end else datetime.now(timezone.utc)
        return (b-timedelta(hours=window_hours)).isoformat(), b.isoformat()
    if start and end and timestamp(start) >= timestamp(end):
        raise ValueError("start must precede end")
    return start, end


def range_query(field, start=None, end=None):
    bounds = {}
    if start:
        bounds["gte"] = start
    if end:
        bounds["lt"] = end
    return {"range":{field:bounds}} if bounds else {"match_all":{}}
