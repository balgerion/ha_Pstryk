import logging
import json
import os
from datetime import timedelta
import asyncio
from typing import Any
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
from .api_client import PstrykAPIClient

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
        self._unsub_afternoon = None
        self._had_tomorrow_prices = False

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

                await asyncio.sleep(5)

                from .mqtt_common import publish_mqtt_prices
                success = await publish_mqtt_prices(self.hass, entry_id, mqtt_topic_buy, mqtt_topic_sell)

                if success:
                    _LOGGER.info("Successfully published 48h prices to MQTT after detecting valid tomorrow prices")
                else:
                    _LOGGER.error("Failed to publish to MQTT after detecting tomorrow prices")

        self._had_tomorrow_prices = has_valid_tomorrow_prices

    async def _async_update_data(self):
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
                max_retries=self.retry_attempts,
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
            }

            self._has_tomorrow = self._check_has_valid_tomorrow(new_data)
            new_data["has_tomorrow"] = self._has_tomorrow

            await self._save_cache(new_data)

            if self.mqtt_48h_mode:
                await self._check_and_publish_mqtt(new_data)

            return new_data

        except UpdateFailed as err:
            _LOGGER.error("Failed to fetch %s data from API: %s", self.price_type, err)
            raise

        except Exception as err:
            _LOGGER.exception("Unexpected error fetching %s data: %s", self.price_type, err)
            raise UpdateFailed(f"Error: {err}") from err

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

        cached_data = self.data or await self._load_cache()

        if cached_data:
            last_updated = cached_data.get("last_updated", "")
            if last_updated:
                try:
                    cache_date = last_updated.split("T")[0]
                    today_date = dt_util.now().strftime("%Y-%m-%d")

                    if cache_date != today_date:
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
            self.data = cached_data
            self.last_update_success = True
            self._has_tomorrow = cached_data.get("has_tomorrow", False)
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

    def schedule_afternoon_update(self):
        if self._unsub_afternoon:
            self._unsub_afternoon()
            self._unsub_afternoon = None

        if not self.mqtt_48h_mode:
            _LOGGER.debug("Afternoon updates not scheduled for %s - 48h mode is disabled", self.price_type)
            return

        now = dt_util.now()

        if now.hour < 14:
            next_check = now.replace(hour=14, minute=0, second=0, microsecond=0)
        elif now.hour == 14:
            current_minutes = now.minute
            if current_minutes < 15:
                next_minutes = 15
            elif current_minutes < 30:
                next_minutes = 30
            elif current_minutes < 45:
                next_minutes = 45
            else:
                next_check = now.replace(hour=15, minute=0, second=0, microsecond=0)
                next_minutes = None

            if next_minutes is not None:
                next_check = now.replace(minute=next_minutes, second=0, microsecond=0)
        else:
            next_check = (now + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)

        if next_check <= now:
            next_check = next_check + timedelta(minutes=15)

        _LOGGER.info("Scheduling afternoon update check for %s at %s (48h mode, checking every 15min between 14:00-15:00)",
                     self.price_type, next_check.strftime("%Y-%m-%d %H:%M:%S"))

        self._unsub_afternoon = async_track_point_in_time(
            self.hass, self._handle_afternoon_update, dt_util.as_utc(next_check)
        )

    async def _handle_afternoon_update(self, _):
        now = dt_util.now()

        if not self.mqtt_48h_mode:
            _LOGGER.debug("Skipping afternoon update for %s (48h mode disabled)",
                         self.price_type)
            self.schedule_afternoon_update()
            return

        if self._has_tomorrow:
            _LOGGER.debug("Already have tomorrow for %s, skipping afternoon fetch",
                         self.price_type)
            self.schedule_afternoon_update()
            return

        _LOGGER.info("Afternoon check for %s at %s - looking for tomorrow prices",
                    self.price_type, now.strftime("%H:%M"))
        await self.async_request_refresh()

        if self._has_tomorrow:
            _LOGGER.info("✓ Found tomorrow prices for %s at %s!",
                        self.price_type, now.strftime("%H:%M"))

        self.schedule_afternoon_update()
