"""Data update coordinator for Pstryk Energy integration."""
import logging
from datetime import timedelta
import asyncio
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util
from homeassistant.helpers.translation import async_get_translations
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
    """Convert price string to float."""
    try:
        return round(float(str(value).replace(",", ".").strip()), 2)
    except (ValueError, TypeError) as e:
        _LOGGER.warning("Price conversion error: %s", e)
        return None


class PstrykDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch both current price and today's table."""

    def __init__(self, hass, api_client: PstrykAPIClient, price_type, mqtt_48h_mode=False, retry_attempts=None, retry_delay=None):
        """Initialize the coordinator."""
        self.hass = hass
        self.api_client = api_client
        self.price_type = price_type
        self.mqtt_48h_mode = mqtt_48h_mode
        self._unsub_hourly = None
        self._unsub_midnight = None
        self._unsub_afternoon = None
        self._translations = {}
        self._had_tomorrow_prices = False

        # Get retry configuration
        if retry_attempts is None:
            retry_attempts = DEFAULT_RETRY_ATTEMPTS
        if retry_delay is None:
            retry_delay = DEFAULT_RETRY_DELAY

        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay

        # Set update interval as fallback
        update_interval = timedelta(hours=1)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{price_type}",
            update_interval=update_interval,
        )

    def _extract_price_value(self, frame):
        """Return normalized gross price for buy or prosumer sell frames."""
        metrics = frame.get("metrics", {})
        pricing = metrics.get("pricing", {})
        price_key = "price_gross" if self.price_type == "buy" else "price_prosumer_gross"

        return convert_price(
            frame.get(price_key, pricing.get(price_key, frame.get("price_gross")))
        )

    def _is_likely_placeholder_data(self, prices_for_day):
        """Check if prices for a day are likely placeholders."""
        if not prices_for_day:
            return True

        price_values = [p.get("price") for p in prices_for_day if p.get("price") is not None]

        if not price_values:
            return True

        if len(price_values) < 20:
            _LOGGER.debug(f"Only {len(price_values)} prices for the day, likely incomplete data")
            return True

        unique_values = set(price_values)
        if len(unique_values) == 1:
            _LOGGER.debug(f"All {len(price_values)} prices have the same value ({price_values[0]}), likely placeholders")
            return True

        most_common_value = max(set(price_values), key=price_values.count)
        count_most_common = price_values.count(most_common_value)
        if count_most_common / len(price_values) > 0.9:
            _LOGGER.debug(f"{count_most_common}/{len(price_values)} prices have value {most_common_value}, likely placeholders")
            return True

        return False

    async def _check_and_publish_mqtt(self, new_data):
        """Check if we should publish to MQTT after update."""
        if not self.mqtt_48h_mode:
            return

        now = dt_util.now()
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        all_prices = new_data.get("prices", [])
        tomorrow_prices = [p for p in all_prices if p["start"].startswith(tomorrow)]

        has_valid_tomorrow_prices = (
            len(tomorrow_prices) >= 20 and
            not self._is_likely_placeholder_data(tomorrow_prices)
        )

        if not self._had_tomorrow_prices and has_valid_tomorrow_prices:
            _LOGGER.info("Valid tomorrow prices detected for %s, triggering immediate MQTT publish", self.price_type)

            # Find our config entry
            entry_id = None
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if self.api_client.api_key == entry.data.get("api_key"):
                    entry_id = entry.entry_id
                    break

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
        """Fetch 48h of frames and extract current + today's list."""
        _LOGGER.debug("Starting %s price update (48h mode: %s)", self.price_type, self.mqtt_48h_mode)

        previous_data = None
        if hasattr(self, 'data') and self.data:
            previous_data = self.data.copy() if self.data else None
            if previous_data:
                previous_data["is_cached"] = True

        today_local = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
        window_end_local = today_local + timedelta(days=2)

        start_utc = dt_util.as_utc(today_local)
        end_utc = dt_util.as_utc(window_end_local)

        start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        endpoint = PRICING_ENDPOINT.format(start=start_str, end=end_str)
        url = f"{API_URL}{endpoint}"

        _LOGGER.debug("Requesting %s data from %s", self.price_type, url)

        now_utc = dt_util.utcnow()

        try:
            # Load translations
            try:
                self._translations = await async_get_translations(
                    self.hass, self.hass.config.language, DOMAIN, ["debug"]
                )
            except Exception as ex:
                _LOGGER.warning("Failed to load translations for coordinator: %s", ex)

            # Use shared API client
            data = await self.api_client.fetch(
                url,
                max_retries=self.retry_attempts,
                base_delay=self.retry_delay
            )

            frames = data.get("frames", [])
            if not frames:
                _LOGGER.warning("No frames returned for %s prices", self.price_type)

            prices = []
            current_price = None

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
                prices.append({"start": local_start, "price": val})

                if start <= now_utc < end:
                    current_price = val

            today_local = dt_util.now().strftime("%Y-%m-%d")
            prices_today = [p for p in prices if p["start"].startswith(today_local)]

            _LOGGER.debug("Successfully fetched %s price data: current=%s, today_prices=%d, total_prices=%d",
                         self.price_type, current_price, len(prices_today), len(prices))

            new_data = {
                "prices_today": prices_today,
                "prices": prices,
                "current": current_price,
                "is_cached": False,
            }

            if self.mqtt_48h_mode:
                await self._check_and_publish_mqtt(new_data)

            return new_data

        except UpdateFailed:
            # UpdateFailed already has proper error message from API client
            if previous_data:
                _LOGGER.warning("Using cached data from previous update due to API failure")
                return previous_data
            raise

        except Exception as err:
            error_msg = self._translations.get(
                "debug.unexpected_error",
                "Unexpected error fetching {price_type} data: {error}"
            ).format(price_type=self.price_type, error=str(err))
            _LOGGER.exception(error_msg)

            if previous_data:
                _LOGGER.warning("Using cached data from previous update due to API failure")
                return previous_data

            raise UpdateFailed(self._translations.get(
                "debug.unexpected_error_user",
                "Error: {error}"
            ).format(error=err))

    def schedule_hourly_update(self):
        """Schedule next refresh 1 min after each full hour."""
        if self._unsub_hourly:
            self._unsub_hourly()
            self._unsub_hourly = None

        now = dt_util.now()
        next_run = (now.replace(minute=0, second=0, microsecond=0)
                    + timedelta(hours=1, minutes=1))

        _LOGGER.debug("Scheduling next hourly update for %s at %s",
                     self.price_type, next_run.strftime("%Y-%m-%d %H:%M:%S"))

        self._unsub_hourly = async_track_point_in_time(
            self.hass, self._handle_hourly_update, dt_util.as_utc(next_run)
        )

    async def _check_tomorrow_prices_after_15(self):
        """Check if tomorrow prices are available and publish to MQTT if needed."""
        if not self.mqtt_48h_mode:
            return

        now = dt_util.now()
        if now.hour < 15:
            return

        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        if self.data and self.data.get("prices"):
            tomorrow_prices = [p for p in self.data.get("prices", []) if p.get("start", "").startswith(tomorrow)]

            has_valid_tomorrow = (
                len(tomorrow_prices) >= 20 and
                not self._is_likely_placeholder_data(tomorrow_prices)
            )

            if has_valid_tomorrow:
                return
            else:
                _LOGGER.info("Missing or invalid tomorrow prices at %s, will refresh data", now.strftime("%H:%M"))

        await self.async_request_refresh()

    async def _handle_hourly_update(self, _):
        """Handle hourly update."""
        _LOGGER.debug("Running scheduled hourly update for %s", self.price_type)

        await self.async_request_refresh()
        await self._check_tomorrow_prices_after_15()
        self.schedule_hourly_update()

    def schedule_midnight_update(self):
        """Schedule next refresh 1 min after local midnight."""
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
        """Handle midnight update."""
        _LOGGER.debug("Running scheduled midnight update for %s", self.price_type)
        await self.async_request_refresh()
        self.schedule_midnight_update()

    def schedule_afternoon_update(self):
        """Schedule frequent updates between 14:00-15:00 for 48h mode."""
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
        """Handle afternoon update for 48h mode."""
        now = dt_util.now()
        _LOGGER.debug("Running scheduled afternoon update check for %s at %s",
                     self.price_type, now.strftime("%H:%M"))

        await self.async_request_refresh()

        if now.hour < 15:
            self.schedule_afternoon_update()
        else:
            _LOGGER.info("Finished afternoon update window for %s, next cycle tomorrow", self.price_type)
            self.schedule_afternoon_update()
