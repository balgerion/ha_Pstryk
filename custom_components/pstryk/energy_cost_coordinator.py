"""Pstryk energy cost data coordinator."""
import logging
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_point_in_time
import homeassistant.util.dt as dt_util
from .const import (
    DOMAIN,
    API_URL,
    UNIFIED_METRICS_ENDPOINT,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY
)
from .api_client import PstrykAPIClient

_LOGGER = logging.getLogger(__name__)


class PstrykCostDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Pstryk energy cost data."""

    def __init__(self, hass: HomeAssistant, api_client: PstrykAPIClient, retry_attempts=None, retry_delay=None):
        """Initialize."""
        self.api_client = api_client
        self._unsub_hourly = None
        self._unsub_midnight = None

        # Get retry configuration
        if retry_attempts is None:
            retry_attempts = DEFAULT_RETRY_ATTEMPTS
        if retry_delay is None:
            retry_delay = DEFAULT_RETRY_DELAY

        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay

        # We use custom scheduled updates (midnight, hourly)
        # No automatic update_interval needed
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_cost",
        )

    async def _async_update_data(self, fetch_all: bool = True):
        """Fetch energy cost data from API.

        Args:
            fetch_all: If True, fetch all resolutions (daily, monthly, yearly).
                      If False, fetch only daily data (for hourly updates).
        """
        _LOGGER.debug("Starting energy cost and usage data fetch (fetch_all=%s)", fetch_all)

        try:
            now = dt_util.utcnow()

            # For daily data: fetch yesterday, today, and tomorrow to ensure we have complete data
            # This handles the case where live data might be from yesterday
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday_start = today_start - timedelta(days=1)
            day_after_tomorrow = today_start + timedelta(days=2)

            # For monthly data: always fetch current month only
            # The API handles month boundaries internally, so we don't need to worry about it
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            # Get first day of next month
            if now.month == 12:
                next_month_start = month_start.replace(year=now.year + 1, month=1)
            else:
                next_month_start = month_start.replace(month=now.month + 1)

            # For yearly data: fetch current year using month resolution
            year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            next_year_start = year_start.replace(year=now.year + 1)

            format_time = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            data = {}

            # Fetch daily data
            daily_url = API_URL + UNIFIED_METRICS_ENDPOINT.format(
                resolution="day",
                start=format_time(yesterday_start),
                end=format_time(day_after_tomorrow),
            )

            _LOGGER.debug(f"Fetching daily data from {yesterday_start} to {day_after_tomorrow}")

            try:
                daily_data = await self.api_client.fetch(daily_url)
                daily_data = await self.api_client.fetch(
                    daily_url,
                    max_retries=self.retry_attempts,
                    base_delay=self.retry_delay
                )

                if daily_data:
                    data["daily"] = self._process_daily_data_simple(daily_data)
            except UpdateFailed as e:
                _LOGGER.warning(f"Failed to fetch daily data: {e}. Continuing with other resolutions.")

            # Fetch monthly and yearly data only when fetch_all=True (midnight update)
            if fetch_all:
                # Fetch monthly data
                # IMPORTANT: For monthly data at month boundary, only request current month
                # to avoid API 500 errors when crossing month boundaries
                monthly_url = API_URL + UNIFIED_METRICS_ENDPOINT.format(
                    resolution="month",
                    start=format_time(month_start),
                    end=format_time(next_month_start),
                )

                _LOGGER.debug(f"Fetching monthly data for {month_start.strftime('%B %Y')}")

                try:
                    monthly_data = await self.api_client.fetch(
                        monthly_url,
                        max_retries=self.retry_attempts,
                        base_delay=self.retry_delay
                    )

                    if monthly_data:
                        data["monthly"] = self._process_monthly_data_simple(monthly_data)
                except UpdateFailed as e:
                    _LOGGER.warning(f"Failed to fetch monthly data: {e}. Continuing with other resolutions.")

                # Fetch yearly data using month resolution
                yearly_url = API_URL + UNIFIED_METRICS_ENDPOINT.format(
                    resolution="month",
                    start=format_time(year_start),
                    end=format_time(next_year_start),
                )

                _LOGGER.debug(f"Fetching yearly data for {year_start.year}")

                try:
                    yearly_data = await self.api_client.fetch(
                        yearly_url,
                        max_retries=self.retry_attempts,
                        base_delay=self.retry_delay
                    )

                    if yearly_data:
                        data["yearly"] = self._process_yearly_data_simple(yearly_data)
                except UpdateFailed as e:
                    _LOGGER.warning(f"Failed to fetch yearly data: {e}.")
            else:
                _LOGGER.debug("Skipping monthly and yearly data fetch (hourly update - using cached data)")

            # If we have at least one resolution, consider it a success
            if data:
                _LOGGER.debug(f"Successfully fetched energy cost and usage data for resolutions: {list(data.keys())}")
                return data
            else:
                raise UpdateFailed("Failed to fetch energy cost data for any resolution")

        except Exception as err:
            _LOGGER.error("Error fetching energy cost data: %s", err, exc_info=True)
            raise UpdateFailed(f"Error fetching energy cost data: {err}")

    def _normalize_frame(self, frame):
        """Normalize unified metrics frames to the integration's internal format."""
        metrics = frame.get("metrics", {})
        meter_values = metrics.get("meter_values") or metrics.get("meterValues") or {}
        cost = metrics.get("cost") or {}

        return {
            "start": frame.get("start"),
            "end": frame.get("end"),
            "is_live": frame.get("is_live", False),
            "fae_usage": frame.get("fae_usage", meter_values.get("energy_active_import_register", 0) or 0),
            "rae": frame.get("rae", meter_values.get("energy_active_export_register", 0) or 0),
            "energy_balance": frame.get("energy_balance", meter_values.get("energy_balance", 0) or 0),
            "fae_cost": frame.get(
                "fae_cost",
                cost.get("energy_import_cost", cost.get("energy_active_import_register_cost", 0) or 0),
            ),
            "var_dist_cost_net": frame.get("var_dist_cost_net", cost.get("distribution_cost", 0) or 0),
            "fix_dist_cost_net": frame.get("fix_dist_cost_net", 0),
            "energy_cost_net": frame.get(
                "energy_cost_net",
                cost.get("energy_cost_net", cost.get("energy_import_cost", 0) or 0),
            ),
            "service_cost_net": frame.get("service_cost_net", 0),
            "excise": frame.get("excise", cost.get("excise", 0) or 0),
            "vat": frame.get("vat", cost.get("vat", 0) or 0),
            "energy_sold_value": frame.get(
                "energy_sold_value",
                cost.get("energy_sold_value", cost.get("energy_active_export_register_value", 0) or 0),
            ),
            "energy_balance_value": frame.get(
                "energy_balance_value",
                cost.get("energy_balance_value", 0) or 0,
            ),
        }

    def _normalized_frames(self, response):
        """Return frames normalized to the integration's internal shape."""
        return [self._normalize_frame(frame) for frame in response.get("frames", [])]

    def _process_monthly_data_simple(self, response):
        """Take the first month frame from normalized unified metrics data."""
        _LOGGER.info("Processing monthly data - simple version")

        result = {
            "frame": {},
            "total_balance": 0,
            "total_sold": 0,
            "total_cost": 0,
            "fae_usage": 0,
            "rae_usage": 0
        }

        frames = self._normalized_frames(response)

        if frames:
            frame = frames[0]
            result["frame"] = frame
            result["total_balance"] = frame.get("energy_balance_value", 0)
            result["total_sold"] = frame.get("energy_sold_value", 0)
            result["total_cost"] = abs(frame.get("fae_cost", 0))
            _LOGGER.info(f"Monthly cost data: balance={result['total_balance']}, "
                        f"sold={result['total_sold']}, cost={result['total_cost']}")

        if frames:
            frame = frames[0]
            result["fae_usage"] = frame.get("fae_usage", 0)
            result["rae_usage"] = frame.get("rae", 0)
            _LOGGER.info(f"Monthly usage data: fae={result['fae_usage']}, rae={result['rae_usage']}")

        return result

    def _process_daily_data_simple(self, response):
        """Use the live day frame from normalized unified metrics data."""
        _LOGGER.info("=== SIMPLE DAILY DATA PROCESSOR ===")

        result = {
            "frame": {},
            "total_balance": 0,
            "total_sold": 0,
            "total_cost": 0,
            "fae_usage": 0,
            "rae_usage": 0
        }

        live_date = None

        frames = self._normalized_frames(response)

        if frames:
            _LOGGER.info(f"Processing {len(frames)} unified metric frames")

            for i, frame in enumerate(frames):
                _LOGGER.info(f"Frame {i}: start={frame.get('start')}, "
                           f"is_live={frame.get('is_live', False)}, "
                           f"fae_usage={frame.get('fae_usage')}, "
                           f"rae={frame.get('rae')}")

                if frame.get("is_live", False):
                    result["fae_usage"] = frame.get("fae_usage", 0)
                    result["rae_usage"] = frame.get("rae", 0)
                    _LOGGER.info(f"*** FOUND LIVE FRAME: fae_usage={result['fae_usage']}, rae={result['rae_usage']} ***")

                    live_start = frame.get("start")
                    if live_start:
                        live_date = live_start.split("T")[0]
                        _LOGGER.info(f"Live frame date: {live_date}")
                    break

        if live_date:
            _LOGGER.info(f"Looking for matching frame date: {live_date}")

            for frame in frames:
                frame_start = frame.get("start", "")
                frame_date = frame_start.split("T")[0] if frame_start else ""

                _LOGGER.info(f"Checking cost frame: start={frame_start}, date={frame_date}, "
                           f"balance={frame.get('energy_balance_value', 0)}, "
                           f"cost={frame.get('fae_cost', 0)}")

                if frame_date == live_date:
                    result["frame"] = frame
                    result["total_balance"] = frame.get("energy_balance_value", 0)
                    result["total_sold"] = frame.get("energy_sold_value", 0)
                    result["total_cost"] = abs(frame.get("fae_cost", 0))
                    _LOGGER.info(f"*** MATCHED cost frame for date {live_date}: balance={result['total_balance']}, "
                               f"cost={result['total_cost']}, sold={result['total_sold']} ***")
                    break
            else:
                _LOGGER.warning(f"No unified metrics frame found matching live date {live_date}")
        elif not live_date:
            _LOGGER.warning("No live frame found in unified metrics data, cannot match daily frame")

        _LOGGER.info(f"=== FINAL RESULT: fae_usage={result['fae_usage']}, "
                    f"rae_usage={result['rae_usage']}, "
                    f"balance={result['total_balance']}, "
                    f"cost={result['total_cost']}, "
                    f"sold={result['total_sold']} ===")
        return result

    def _process_yearly_data_simple(self, response):
        """Sum all month frames from normalized unified metrics data."""
        _LOGGER.info("Processing yearly data - simple version")

        total_balance = 0
        total_sold = 0
        total_cost = 0
        fae_usage = 0
        rae_usage = 0

        frames = self._normalized_frames(response)

        if frames:
            for frame in frames:
                total_balance += frame.get("energy_balance_value", 0)
                total_sold += frame.get("energy_sold_value", 0)
                total_cost += abs(frame.get("fae_cost", 0))
            _LOGGER.info(f"Yearly cost totals: balance={total_balance}, "
                        f"sold={total_sold}, cost={total_cost}")

        if frames:
            for frame in frames:
                fae_usage += frame.get("fae_usage", 0)
                rae_usage += frame.get("rae", 0)
            _LOGGER.info(f"Yearly usage totals: fae={fae_usage}, rae={rae_usage}")

        return {
            "frame": {},
            "total_balance": total_balance,
            "total_sold": total_sold,
            "total_cost": total_cost,
            "fae_usage": fae_usage,
            "rae_usage": rae_usage
        }

    def schedule_midnight_update(self):
        """Schedule midnight updates for daily reset."""
        if hasattr(self, '_unsub_midnight'):
            if self._unsub_midnight:
                self._unsub_midnight()
                self._unsub_midnight = None
        else:
            self._unsub_midnight = None

        now = dt_util.now()
        next_mid = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)

        _LOGGER.debug("Scheduling next midnight cost update at %s",
                     next_mid.strftime("%Y-%m-%d %H:%M:%S"))

        self._unsub_midnight = async_track_point_in_time(
            self.hass, self._handle_midnight_update, dt_util.as_utc(next_mid)
        )

    async def _handle_midnight_update(self, _):
        """Handle midnight update - fetch all data (daily, monthly, yearly)."""
        _LOGGER.debug("Running scheduled midnight cost update (all resolutions)")
        try:
            # Fetch all resolutions at midnight
            data = await self._async_update_data(fetch_all=True)
            self.data = data
            self.last_update_success = True
            self.async_update_listeners()
        except Exception as err:
            _LOGGER.error("Midnight cost update failed: %s - will retry next hour", err)
            self.last_update_success = False
        finally:
            # Always reschedule, even if update failed
            self.schedule_midnight_update()

    def schedule_hourly_update(self):
        """Schedule hourly updates."""
        if self._unsub_hourly:
            self._unsub_hourly()
            self._unsub_hourly = None

        now = dt_util.now()
        next_run = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, minutes=1))

        _LOGGER.debug("Scheduling next hourly cost update at %s",
                     next_run.strftime("%Y-%m-%d %H:%M:%S"))

        self._unsub_hourly = async_track_point_in_time(
            self.hass, self._handle_hourly_update, dt_util.as_utc(next_run)
        )

    async def _handle_hourly_update(self, now):
        """Handle the hourly update - fetch all resolutions (daily, monthly, yearly)."""
        _LOGGER.debug("Triggering hourly cost update (all resolutions)")
        try:
            # Fetch all resolutions during hourly updates
            data = await self._async_update_data(fetch_all=True)
            self.data = data
            self.last_update_success = True
            self.async_update_listeners()
        except Exception as err:
            _LOGGER.error("Hourly cost update failed: %s - will retry next hour", err)
            self.last_update_success = False
        finally:
            # Always reschedule, even if update failed
            self.schedule_hourly_update()
