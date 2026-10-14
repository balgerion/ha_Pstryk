"""Check the API for tomorrow's prices from 12:10, every 20 minutes, until they are in.

Each check is one request for buy and sell together, without retries, and only
if the hourly budget has room. A check that finds nothing, fails or meets a
full budget simply waits for the next one. Once tomorrow's prices are cached the
checks stop for the day, and the cost update may use the budget again.
"""
import logging

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .price_policy import next_tomorrow_check
from .price_refresh import async_refresh_prices, has_tomorrow

_LOGGER = logging.getLogger(__name__)


class PstrykTomorrowPoller:

    def __init__(self, hass, entry_id):
        self.hass = hass
        self.entry_id = entry_id
        self._unsub = None
        self._stopped = False

    @callback
    def start(self):
        self._schedule()

    @callback
    def stop(self):
        self._stopped = True
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _schedule(self):
        if self._stopped:
            return
        next_check = next_tomorrow_check(dt_util.now())
        _LOGGER.debug("Next check for tomorrow's prices at %s", next_check.strftime("%Y-%m-%d %H:%M:%S"))
        self._unsub = async_track_point_in_time(
            self.hass, self._handle_check, dt_util.as_utc(next_check)
        )

    async def _handle_check(self, _):
        self._unsub = None
        try:
            await self.async_check()
        except Exception:
            _LOGGER.exception("Check for tomorrow's prices failed")
        finally:
            self._schedule()

    async def async_check(self) -> str:
        if self._stopped:
            return "stopped"
        if has_tomorrow(self.hass, self.entry_id):
            return "found"
        result = await async_refresh_prices(
            self.hass, self.entry_id, "tomorrow check", is_current=lambda: not self._stopped
        )
        if result == "stopped":
            return result
        if result == "found":
            _LOGGER.info("Found tomorrow's prices at %s", dt_util.now().strftime("%H:%M"))
        else:
            _LOGGER.info("Tomorrow's prices not in yet (%s); next check in 20 minutes", result)
        return result
