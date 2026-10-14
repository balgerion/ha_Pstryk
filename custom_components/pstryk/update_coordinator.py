import logging
import json
import os
from datetime import timedelta
import asyncio
from typing import Any
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util
from .const import (
    API_URL,
    PRICING_ENDPOINT,
    DOMAIN,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY
)
from .api_client import PstrykAPIClient, BudgetExhausted, ClientClosed
from .price_policy import today_published, tomorrow_fetch_due
from .price_components import extract_components

_LOGGER = logging.getLogger(__name__)


def convert_price(value):
    if value is None:
        return None
    try:
        return round(float(str(value).replace(",", ".").strip()), 2)
    except (ValueError, TypeError) as e:
        _LOGGER.warning("Price conversion error: %s", e)
        return None


def is_likely_placeholder_data(prices_for_day):
    if not prices_for_day:
        return True

    price_values = [p.get("price") for p in prices_for_day if p.get("price") is not None]

    if len(price_values) < 20:
        _LOGGER.debug("Only %d prices for the day, likely incomplete data", len(price_values))
        return True

    most_common_value = max(set(price_values), key=price_values.count)
    count_most_common = price_values.count(most_common_value)
    if count_most_common / len(price_values) > 0.9:
        _LOGGER.debug("%d/%d prices have value %s, likely placeholders",
                      count_most_common, len(price_values), most_common_value)
        return True

    return False


class PstrykDataUpdateCoordinator(DataUpdateCoordinator):

    def __init__(self, hass, api_client: PstrykAPIClient, price_type, mqtt_48h_mode=False, retry_attempts=None, retry_delay=None, entry_id=None):
        self.hass = hass
        self.api_client = api_client
        self.price_type = price_type
        self.entry_id = entry_id
        self.mqtt_48h_mode = mqtt_48h_mode
        self._unsub_hourly = None
        self._unsub_midnight = None
        self._had_tomorrow_prices = False
        self._unsub_budget_retry = None

        integration_path = os.path.dirname(os.path.abspath(__file__))
        self._cache_file = os.path.join(integration_path, f"cache_{price_type}.json")
        self._has_tomorrow = False

        if retry_attempts is None:
            retry_attempts = DEFAULT_RETRY_ATTEMPTS
        if retry_delay is None:
            retry_delay = DEFAULT_RETRY_DELAY

        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{price_type}",
        )

    def _extract_price_value(self, frame):
        metrics = frame.get("metrics", {})
        pricing = metrics.get("pricing", {})
        price_key = "price_gross" if self.price_type == "buy" else "price_prosumer_gross"

        return convert_price(frame.get(price_key, pricing.get(price_key)))
    async def _load_cache(self) -> dict[str, Any] | None:
        if not os.path.exists(self._cache_file):
            return None

        def _read() -> dict[str, Any] | None:
            try:
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                    if data.get("invalid"):
                        _LOGGER.warning("Cache for %s is marked INVALID: %s (failed at: %s)",
                                      self.price_type,
                                      data.get("reason", "unknown"),
                                      data.get("failed_at", "unknown"))
                        return None

                    return data
            except Exception as err:
                _LOGGER.warning("Failed to read cache for %s: %s", self.price_type, err)
                return None

        return await asyncio.to_thread(_read)

    async def _save_cache(self, data: dict[str, Any]) -> None:
        def _write() -> None:
            try:
                data["last_updated"] = dt_util.now().isoformat()
                with open(self._cache_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                _LOGGER.debug("Saved cache for %s to %s", self.price_type, self._cache_file)
            except Exception as err:
                _LOGGER.warning("Failed to write cache for %s: %s", self.price_type, err)

        await asyncio.to_thread(_write)

    async def _load_own_cache(self) -> dict[str, Any] | None:
        """The cache, if this config entry wrote it.

        The files are named by price type only and outlive their entry; an
        entry added later may belong to another meter with another tariff. A
        cache without an owner (older versions) is not used either.
        """
        cached = await self._load_cache()
        if cached and self.entry_id and cached.get("owner") != self.entry_id:
            _LOGGER.info("Cache for %s was written by another entry; not using it", self.price_type)
            return None
        return cached

    def _check_has_valid_tomorrow(self, data: dict) -> bool:
        now = dt_util.now()
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        all_prices = data.get("prices", [])
        tomorrow_prices = [p for p in all_prices if p.get("start", "").startswith(tomorrow)]

        return len(tomorrow_prices) >= 20 and not is_likely_placeholder_data(tomorrow_prices)

    async def _check_and_publish_mqtt(self, new_data):
        if not self.mqtt_48h_mode:
            return

        now = dt_util.now()
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        all_prices = new_data.get("prices", [])
        tomorrow_prices = [p for p in all_prices if p["start"].startswith(tomorrow)]

        has_valid_tomorrow_prices = (
            len(tomorrow_prices) >= 20 and
            not is_likely_placeholder_data(tomorrow_prices)
        )

        if not self._had_tomorrow_prices and has_valid_tomorrow_prices:
            _LOGGER.info("Valid tomorrow prices detected for %s, triggering immediate MQTT publish", self.price_type)

            entry_id = self.entry_id

            if entry_id:
                buy_coordinator = self.hass.data[DOMAIN].get(f"{entry_id}_buy")
                sell_coordinator = self.hass.data[DOMAIN].get(f"{entry_id}_sell")

                if not buy_coordinator or not sell_coordinator:
                    _LOGGER.debug("Coordinators not yet initialized, skipping MQTT publish for now")
                    return

                from .const import CONF_MQTT_TOPIC_BUY, CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_SELL
                entry = self.hass.config_entries.async_get_entry(entry_id)
                mqtt_topic_buy = entry.options.get(CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY)
                mqtt_topic_sell = entry.options.get(CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL)

                async def _publish_after_refresh():
                    await asyncio.sleep(5)
                    from .mqtt_common import publish_mqtt_prices
                    success = await publish_mqtt_prices(self.hass, entry_id, mqtt_topic_buy, mqtt_topic_sell)
                    if success:
                        _LOGGER.info("Successfully published 48h prices to MQTT after detecting valid tomorrow prices")
                    else:
                        _LOGGER.error("Failed to publish to MQTT after detecting tomorrow prices")

                self.hass.async_create_task(_publish_after_refresh())

        self._had_tomorrow_prices = has_valid_tomorrow_prices

    async def _async_update_data(self):
        try:
            return await self._fetch_prices(self.retry_attempts)
        except BudgetExhausted as err:
            # Prices come first: keep what we have and fetch as soon as a slot frees.
            self._arm_budget_retry(err.free_at)
            if self.today_usable(self.data):
                _LOGGER.info("%s prices: %s; keeping current data", self.price_type, err)
                return self._for_today(self.data)
            raise

    def today_usable(self, data) -> bool:
        """Whether `data` holds today's published prices, current hour included."""
        if not data:
            return False
        now = dt_util.now()
        today = now.strftime("%Y-%m-%d")
        today_entries = [p for p in data.get("prices", []) if p.get("start", "").startswith(today)]
        if not today_published(today_entries, now):
            return False
        # tge_price tells publication exactly; only older caches without it need the heuristic.
        return any("tge_price" in p for p in today_entries) or not is_likely_placeholder_data(today_entries)

    def _for_today(self, data):
        """`data` with prices_today rebuilt for the current local date (it may be from yesterday)."""
        today = dt_util.now().strftime("%Y-%m-%d")
        return {**data, "prices_today": [p for p in data.get("prices", []) if p.get("start", "").startswith(today)]}

    async def async_load_startup_cache(self) -> bool:
        """Cache first. Use the cache if it holds today's published prices.

        Returns True when no API request is needed; False when start-up should
        fetch (no usable cache, or tomorrow's prices are due and missing).
        Unusable cached rows are not exposed: data stays None until a fetch.
        """
        cached = await self._load_own_cache()
        if not self.today_usable(cached):
            return False
        self.data = {**self._for_today(cached), "is_cached": True}
        self._has_tomorrow = self._check_has_valid_tomorrow(self.data)
        self.last_update_success = True
        return not tomorrow_fetch_due(self._has_tomorrow, dt_util.now())

    async def async_startup(self) -> None:
        """Start-up: cache first, then as few API requests as the cache allows.

        Today's prices cached and nothing due: no request. Today's cached and
        only tomorrow's due: one attempt, and the cached prices stay in use
        whatever it returns (the tomorrow checks carry on from there). No
        usable cache: a fetch with the configured retries; its failure is raised.
        """
        if await self.async_load_startup_cache():
            return
        if self.data is not None:
            await self.async_fetch_once()
            return
        self.data = await self._async_update_data()
        self.last_update_success = True

    @callback
    def _arm_budget_retry(self, free_at):
        if self._unsub_budget_retry:
            self._unsub_budget_retry()
            self._unsub_budget_retry = None
        if self.api_client.closed:
            return
        retry_at = (free_at or dt_util.utcnow() + timedelta(minutes=5)) + timedelta(seconds=5)
        _LOGGER.info("Retrying %s price fetch at %s", self.price_type,
                     dt_util.as_local(retry_at).strftime("%H:%M:%S"))

        async def _retry(_):
            self._unsub_budget_retry = None
            await self.async_request_refresh()

        self._unsub_budget_retry = async_track_point_in_time(self.hass, _retry, retry_at)

    async def _fetch_prices(self, max_retries):
        _LOGGER.debug("Starting %s price update (48h mode: %s)", self.price_type, self.mqtt_48h_mode)

        today_local = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
        window_end_local = today_local + timedelta(days=2)

        start_utc = dt_util.as_utc(today_local)
        end_utc = dt_util.as_utc(window_end_local)

        start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        endpoint = PRICING_ENDPOINT.format(start=start_str, end=end_str)
        url = f"{API_URL}{endpoint}"

        _LOGGER.debug("Requesting %s data from %s", self.price_type, url)

        try:
            data = await self.api_client.fetch(
                url,
                max_retries=max_retries,
                base_delay=self.retry_delay
            )

            frames = data.get("frames", [])
            if not frames:
                _LOGGER.warning("No frames returned for %s prices", self.price_type)

            prices = []

            for f in frames:
                val = self._extract_price_value(f)
                if val is None:
                    continue

                start = dt_util.parse_datetime(f["start"])
                end = dt_util.parse_datetime(f["end"])

                if not start or not end:
                    _LOGGER.warning("Invalid datetime format in frames for %s", self.price_type)
                    continue

                local_start = dt_util.as_local(start).strftime("%Y-%m-%dT%H:%M:%S")
                pricing = f.get("metrics", {}).get("pricing", {})
                prices.append({
                    "start": local_start,
                    "price": val,
                    **extract_components(pricing, self.price_type),
                    "is_cheap": pricing.get("is_cheap", False),
                    "is_expensive": pricing.get("is_expensive", False),
                })

            today_local = dt_util.now().strftime("%Y-%m-%d")
            prices_today = [p for p in prices if p["start"].startswith(today_local)]

            _LOGGER.debug("Successfully fetched %s price data: today_prices=%d, total_prices=%d",
                         self.price_type, len(prices_today), len(prices))

            new_data = {
                "prices_today": prices_today,
                "prices": prices,
                "is_cached": False,
                "owner": self.entry_id,
                # When these prices were fetched; kept in the cache across restarts.
                "last_updated": dt_util.now().isoformat(),
            }

            self._has_tomorrow = self._check_has_valid_tomorrow(new_data)
            new_data["has_tomorrow"] = self._has_tomorrow

            await self._save_cache(new_data)

            if self.mqtt_48h_mode:
                await self._check_and_publish_mqtt(new_data)

            return new_data

        except (BudgetExhausted, ClientClosed):
            raise

        except UpdateFailed as err:
            _LOGGER.error("Failed to fetch %s data from API: %s", self.price_type, err)
            raise

        except Exception as err:
            _LOGGER.exception("Unexpected error fetching %s data: %s", self.price_type, err)
            raise UpdateFailed(f"Error: {err}") from err

    async def async_fetch_once(self) -> str:
        """One API attempt without retries: "ok", "budget", "stopped" or "failed". On failure the current data stays."""
        try:
            data = await self._fetch_prices(max_retries=1)
        except ClientClosed:
            return "stopped"
        except BudgetExhausted as err:
            _LOGGER.info("One-shot %s price fetch not sent: %s", self.price_type, err)
            return "budget"
        except Exception as err:
            _LOGGER.warning("One-shot %s price fetch failed, keeping current data: %s", self.price_type, err)
            return "failed"
        self.async_set_updated_data(data)
        return "ok"

    def schedule_hourly_update(self):
        if self._unsub_hourly:
            self._unsub_hourly()
            self._unsub_hourly = None

        now = dt_util.now()
        next_run = (now.replace(minute=0, second=0, microsecond=0)
                    + timedelta(hours=1, seconds=5))

        _LOGGER.debug("Scheduling next hourly update for %s at %s",
                     self.price_type, next_run.strftime("%Y-%m-%d %H:%M:%S"))

        self._unsub_hourly = async_track_point_in_time(
            self.hass, self._handle_hourly_update, dt_util.as_utc(next_run)
        )

    async def _handle_hourly_update(self, _):
        now = dt_util.now()
        if now.hour == 0 and now.minute < 2:
            today = now.strftime("%Y-%m-%d")
            has_today = self.data and any(
                p.get("start", "").startswith(today)
                for p in self.data.get("prices", [])
            )
            if has_today:
                _LOGGER.debug("Midnight tick for %s - new day prices already in 48h data, API fetch follows at 00:01", self.price_type)
                self.async_update_listeners()
                self.schedule_hourly_update()
                return

        _LOGGER.debug("Hourly update for %s - loading from cache", self.price_type)

        cached_data = self.data or await self._load_own_cache()

        if cached_data:
            last_updated = cached_data.get("last_updated", "")
            if last_updated:
                try:
                    cache_date = last_updated.split("T")[0]
                    today_date = dt_util.now().strftime("%Y-%m-%d")

                    # A fetch from yesterday afternoon already holds today's prices.
                    if cache_date != today_date and not self.today_usable(cached_data):
                        _LOGGER.error("Cache for %s is from %s (today is %s) - OLD DATA! Marking as invalid.",
                                     self.price_type, cache_date, today_date)

                        try:
                            invalid_cache = {
                                "invalid": True,
                                "reason": "old_data_detected",
                                "failed_at": dt_util.now().isoformat(),
                                "cache_date": cache_date,
                                "expected_date": today_date
                            }
                            await self._save_cache(invalid_cache)
                            _LOGGER.warning("Marked old cache as INVALID for %s", self.price_type)
                        except Exception as cache_err:
                            _LOGGER.error("Failed to mark cache as invalid: %s", cache_err)

                        try:
                            await self.async_request_refresh()
                        except Exception as fetch_err:
                            _LOGGER.error("Failed to fetch fresh data for %s: %s - sensors UNAVAILABLE",
                                        self.price_type, fetch_err)
                            self.data = None
                            self.async_update_listeners()

                        self.schedule_hourly_update()
                        return
                except Exception as err:
                    _LOGGER.debug("Could not parse cache date: %s", err)

            cached_data["is_cached"] = True
            self.data = self._for_today(cached_data)
            self.last_update_success = True
            self._has_tomorrow = self._check_has_valid_tomorrow(self.data)
            self.async_update_listeners()
            _LOGGER.debug("Loaded %s data from cache (has_tomorrow=%s)",
                         self.price_type, self._has_tomorrow)
        else:
            _LOGGER.warning("No cache found for %s, fetching from API as fallback",
                          self.price_type)
            try:
                await self.async_request_refresh()
            except Exception as err:
                _LOGGER.error("Failed to fetch data for %s: %s - sensors UNAVAILABLE",
                            self.price_type, err)
                self.data = None
                self.async_update_listeners()

        self.schedule_hourly_update()

    def schedule_midnight_update(self):
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None

        now = dt_util.now()
        next_mid = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)

        _LOGGER.debug("Scheduling next midnight update for %s at %s",
                     self.price_type, next_mid.strftime("%Y-%m-%d %H:%M:%S"))

        self._unsub_midnight = async_track_point_in_time(
            self.hass, self._handle_midnight_update, dt_util.as_utc(next_mid)
        )

    async def _handle_midnight_update(self, _):
        _LOGGER.info("Midnight update for %s - fetching fresh data from API", self.price_type)
        self._has_tomorrow = False

        try:
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Midnight fetch failed for %s: %s - marking cache invalid and setting sensors unavailable",
                         self.price_type, err)

            try:
                invalid_cache = {
                    "invalid": True,
                    "reason": "midnight_fetch_failed",
                    "failed_at": dt_util.now().isoformat(),
                    "error": str(err)
                }
                await self._save_cache(invalid_cache)
                _LOGGER.warning("Marked cache as INVALID for %s", self.price_type)
            except Exception as cache_err:
                _LOGGER.error("Failed to mark cache as invalid: %s", cache_err)

            self.data = None
            self.async_update_listeners()
            _LOGGER.warning("Sensors for %s are now UNAVAILABLE due to midnight fetch failure", self.price_type)

        self.schedule_midnight_update()
