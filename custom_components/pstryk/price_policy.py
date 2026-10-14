"""When the integration may spend API requests: prices first, cost on leftovers.

Pure functions with no Home Assistant imports. Times are local (Europe/Warsaw).
"""
from datetime import datetime, timedelta

# Pstryk publishes tomorrow's prices shortly after 12:00 (seen 2026-10-01 to
# 10-04; on 10-04 the API still had none at 12:00:04).
TOMORROW_EXPECTED_FROM = (12, 5)

# Checks for tomorrow's prices: 12:10, 12:30, 12:50, ... every 20 minutes until
# they are in. Three per hour is the whole hourly budget, so cost waits.
TOMORROW_FIRST_CHECK = (12, 10)
TOMORROW_INTERVAL_MINUTES = 20
TOMORROW_LAST_CHECK = (23, 50)

# Cost runs at :50 only on budget the prices leave free: never while tomorrow's
# prices are missing after 12:00, never at 11:50 (its requests would still count
# against the 12:10-12:50 checks) or 23:50 (the 00:01 fetch).
COST_RUN_MINUTE = 50
COST_SKIP_HOURS = frozenset({11, 23})
COST_KEEP_FREE = 1


def today_published(entries: list[dict], now: datetime) -> bool:
    """Whether cached price entries hold today's real prices, current hour included.

    Before publication Pstryk sends rows anyway: buy with price null, sell 0.0,
    both with tge_price null. Entries cached before tge_price was stored lack
    the key and count as published (the caller also applies the placeholder check).
    """
    day = now.strftime("%Y-%m-%d")
    hour = now.strftime("%Y-%m-%dT%H")
    published = [
        p for p in entries
        if p.get("start", "").startswith(day)
        and p.get("price") is not None
        and ("tge_price" not in p or p["tge_price"] is not None)
    ]
    return len(published) >= 20 and any(p["start"].startswith(hour) for p in published)


def tomorrow_fetch_due(has_tomorrow: bool, now: datetime) -> bool:
    """Whether a start-up with today's prices cached should still fetch, for tomorrow."""
    return not has_tomorrow and (now.hour, now.minute) >= TOMORROW_EXPECTED_FROM


def next_tomorrow_check(now: datetime) -> datetime:
    """The next check for tomorrow's prices strictly after `now`."""
    first = now.replace(hour=TOMORROW_FIRST_CHECK[0], minute=TOMORROW_FIRST_CHECK[1], second=0, microsecond=0)
    last = now.replace(hour=TOMORROW_LAST_CHECK[0], minute=TOMORROW_LAST_CHECK[1], second=0, microsecond=0)
    if now < first:
        return first
    minutes_since_first = int((now - first).total_seconds() // 60)
    check = first + timedelta(
        minutes=(minutes_since_first // TOMORROW_INTERVAL_MINUTES + 1) * TOMORROW_INTERVAL_MINUTES
    )
    if check <= last:
        return check
    return (now + timedelta(days=1)).replace(
        hour=TOMORROW_FIRST_CHECK[0], minute=TOMORROW_FIRST_CHECK[1], second=0, microsecond=0
    )


def next_cost_run(now: datetime) -> datetime:
    """The next :50 strictly after `now`."""
    run = now.replace(minute=COST_RUN_MINUTE, second=0, microsecond=0)
    if run <= now:
        run = (now + timedelta(hours=1)).replace(minute=COST_RUN_MINUTE, second=0, microsecond=0)
    return run


def cost_run_allowed(now: datetime, has_tomorrow: bool) -> bool:
    if now.hour in COST_SKIP_HOURS:
        return False
    return has_tomorrow or now.hour < 12
