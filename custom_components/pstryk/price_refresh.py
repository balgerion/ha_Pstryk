"""One fetch of buy and sell prices, shared by the refresh button and the tomorrow checks."""
import asyncio
import logging

from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def price_coordinators(hass, entry_id):
    data = hass.data.get(DOMAIN, {})
    return [
        coordinator
        for coordinator in (data.get(f"{entry_id}_{price_type}") for price_type in ("buy", "sell"))
        if coordinator is not None
    ]


def has_tomorrow(hass, entry_id) -> bool:
    coordinators = price_coordinators(hass, entry_id)
    return bool(coordinators) and all(c._has_tomorrow for c in coordinators)


def budget_free_at(hass, entry_id):
    coordinators = price_coordinators(hass, entry_id)
    return coordinators[0].api_client.budget_free_at() if coordinators else None


async def async_refresh_prices(hass, entry_id, reason: str, is_current=None) -> str:
    """Fetch buy and sell once: one HTTP request, no retries, only if the hourly budget has room.

    `is_current` (optional) is checked after waiting for the lock; a caller that
    was stopped meanwhile (e.g. by a reload) sends nothing.
    Returns "found" (tomorrow present), "no_tomorrow", "budget" (nothing sent),
    "stopped" or "failed".
    """
    if not price_coordinators(hass, entry_id):
        _LOGGER.warning("Price refresh (%s) skipped: price coordinators are not ready", reason)
        return "failed"

    # One refresh at a time per entry: overlapping presses wait, then meet the budget.
    lock = hass.data[DOMAIN].setdefault(f"{entry_id}_refresh_lock", asyncio.Lock())
    async with lock:
        if is_current is not None and not is_current():
            return "stopped"
        # Look the coordinators up after the wait: a reload may have replaced them.
        coordinators = price_coordinators(hass, entry_id)
        if not coordinators:
            return "stopped"
        _LOGGER.info("Refreshing buy and sell prices (%s)", reason)
        # Buy and sell request the same URL; run concurrently, the API client's
        # in-flight dedup turns them into a single HTTP request.
        results = await asyncio.gather(*(c.async_fetch_once() for c in coordinators))

    if "stopped" in results:
        return "stopped"
    if "budget" in results:
        free_at = budget_free_at(hass, entry_id)
        _LOGGER.info(
            "Price refresh (%s) not sent: API budget full, next slot at %s",
            reason,
            dt_util.as_local(free_at).strftime("%H:%M:%S") if free_at else "unknown",
        )
        return "budget"
    if "failed" in results:
        return "failed"
    return "found" if all(c._has_tomorrow for c in coordinators) else "no_tomorrow"
