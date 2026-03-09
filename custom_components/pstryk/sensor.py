"""Sensor platform for Pstryk Energy integration."""
import logging
import asyncio
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util
from .update_coordinator import PstrykDataUpdateCoordinator
from .energy_cost_coordinator import PstrykCostDataUpdateCoordinator
from .api_client import PstrykAPIClient
from .const import (
    DOMAIN,
    CONF_MQTT_48H_MODE,
    CONF_RETRY_ATTEMPTS,
    CONF_RETRY_DELAY,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY
)
from homeassistant.helpers.translation import async_get_translations

_LOGGER = logging.getLogger(__name__)

# Store translations globally to avoid reloading for each sensor
_TRANSLATIONS_CACHE = {}

# Cache for manifest version
_VERSION_CACHE = None


def get_integration_version(hass: HomeAssistant) -> str:
    """Get integration version from manifest.json."""
    global _VERSION_CACHE
    if _VERSION_CACHE is not None:
        return _VERSION_CACHE

    try:
        import json
        import os
        manifest_path = os.path.join(os.path.dirname(__file__), "manifest.json")
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
            _VERSION_CACHE = manifest.get("version", "unknown")
            return _VERSION_CACHE
    except Exception as ex:
        _LOGGER.warning("Failed to read version from manifest.json: %s", ex)
        return "unknown"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up the Pstryk sensors via the coordinator."""
    api_key = hass.data[DOMAIN][entry.entry_id]["api_key"]
    buy_top = entry.options.get("buy_top", entry.data.get("buy_top", 5))
    sell_top = entry.options.get("sell_top", entry.data.get("sell_top", 5))
    buy_worst = entry.options.get("buy_worst", entry.data.get("buy_worst", 5))
    sell_worst = entry.options.get("sell_worst", entry.data.get("sell_worst", 5))
    mqtt_48h_mode = entry.options.get(CONF_MQTT_48H_MODE, False)
    retry_attempts = entry.options.get(CONF_RETRY_ATTEMPTS, DEFAULT_RETRY_ATTEMPTS)
    retry_delay = entry.options.get(CONF_RETRY_DELAY, DEFAULT_RETRY_DELAY)

    _LOGGER.debug("Setting up Pstryk sensors with buy_top=%d, sell_top=%d, buy_worst=%d, sell_worst=%d, mqtt_48h_mode=%s, retry_attempts=%d, retry_delay=%ds", 
                 buy_top, sell_top, buy_worst, sell_worst, mqtt_48h_mode, retry_attempts, retry_delay)

    # Load translations once for all sensors
    global _TRANSLATIONS_CACHE
    if not _TRANSLATIONS_CACHE:
        try:
            _TRANSLATIONS_CACHE = await async_get_translations(
                hass, hass.config.language, DOMAIN, ["entity", "debug"]
            )
        except Exception as ex:
            _LOGGER.warning("Failed to load translations: %s", ex)
            _TRANSLATIONS_CACHE = {}

    # Cleanup old coordinators if they exist
    for price_type in ("buy", "sell"):
        key = f"{entry.entry_id}_{price_type}"
        coordinator = hass.data[DOMAIN].get(key)
        if coordinator:
            _LOGGER.debug("Cleaning up existing %s coordinator", price_type)
            # Cancel scheduled updates
            if hasattr(coordinator, '_unsub_hourly') and coordinator._unsub_hourly:
                coordinator._unsub_hourly()
            if hasattr(coordinator, '_unsub_midnight') and coordinator._unsub_midnight:
                coordinator._unsub_midnight()
            if hasattr(coordinator, '_unsub_afternoon') and coordinator._unsub_afternoon:
                coordinator._unsub_afternoon()
            # Remove from hass data
            hass.data[DOMAIN].pop(key, None)

    # Cleanup old cost coordinator if exists
    cost_key = f"{entry.entry_id}_cost"
    cost_coordinator = hass.data[DOMAIN].get(cost_key)
    if cost_coordinator:
        _LOGGER.debug("Cleaning up existing cost coordinator")
        if hasattr(cost_coordinator, '_unsub_hourly') and cost_coordinator._unsub_hourly:
            cost_coordinator._unsub_hourly()
        if hasattr(cost_coordinator, '_unsub_midnight') and cost_coordinator._unsub_midnight:
            cost_coordinator._unsub_midnight()
        hass.data[DOMAIN].pop(cost_key, None)

    # Create shared API client (or reuse existing one)
    api_client_key = f"{entry.entry_id}_api_client"
    if api_client_key not in hass.data[DOMAIN]:
        api_client = PstrykAPIClient(hass, api_key)
        hass.data[DOMAIN][api_client_key] = api_client
    else:
        api_client = hass.data[DOMAIN][api_client_key]

    entities = []
    coordinators = []

    # Create price coordinators first
    for price_type in ("buy", "sell"):
        key = f"{entry.entry_id}_{price_type}"
        coordinator = PstrykDataUpdateCoordinator(
            hass,
            api_client,
            price_type,
            mqtt_48h_mode,
            retry_attempts,
            retry_delay
        )
        coordinators.append((coordinator, price_type, key))

    # Create cost coordinator (will be initialized as unavailable for lazy loading)
    cost_coordinator = PstrykCostDataUpdateCoordinator(
        hass,
        api_client,
        retry_attempts,
        retry_delay
    )
    cost_coordinator.last_update_success = False
    coordinators.append((cost_coordinator, "cost", cost_key))

    # Initialize ONLY price coordinators immediately (fast startup)
    # Cost coordinator will be loaded lazily in background
    _LOGGER.info("Starting quick initialization - loading price coordinators only")

    async def safe_initial_fetch(coord, coord_type):
        """Safely fetch initial data for coordinator."""
        try:
            data = await coord._async_update_data()
            coord.data = data
            coord.last_update_success = True
            _LOGGER.debug("Successfully initialized %s coordinator", coord_type)
            return True
        except Exception as err:
            _LOGGER.error("Failed initial fetch for %s coordinator: %s", coord_type, err)
            coord.last_update_success = False
            return err

    # Load only price coordinators immediately for fast startup
    price_coordinators = [(c, t, k) for c, t, k in coordinators if t in ("buy", "sell")]

    initial_refresh_tasks = [
        safe_initial_fetch(coordinator, coordinator_type)
        for coordinator, coordinator_type, _ in price_coordinators
    ]

    refresh_results = await asyncio.gather(*initial_refresh_tasks, return_exceptions=True)

    # Check results for price coordinators
    for i, (coordinator, coordinator_type, key) in enumerate(price_coordinators):
        if isinstance(refresh_results[i], Exception):
            _LOGGER.error("Failed to initialize %s coordinator: %s",
                         coordinator_type, str(refresh_results[i]))
    
    # Store all coordinators and set up scheduling
    buy_coord = None
    sell_coord = None

    for coordinator, coordinator_type, key in coordinators:
        # Store coordinator
        hass.data[DOMAIN][key] = coordinator

        # Schedule updates for price coordinators
        if coordinator_type in ("buy", "sell"):
            coordinator.schedule_hourly_update()
            coordinator.schedule_midnight_update()

            # Schedule afternoon update if 48h mode is enabled
            if mqtt_48h_mode:
                coordinator.schedule_afternoon_update()

            # Create ONLY current price sensors (fast, immediate)
            top = buy_top if coordinator_type == "buy" else sell_top
            worst = buy_worst if coordinator_type == "buy" else sell_worst
            entities.append(PstrykPriceSensor(coordinator, coordinator_type, top, worst, entry.entry_id))

            # Store coordinator references for later use
            if coordinator_type == "buy":
                buy_coord = coordinator
            elif coordinator_type == "sell":
                sell_coord = coordinator

        # Schedule updates for cost coordinator
        elif coordinator_type == "cost":
            coordinator.schedule_hourly_update()
            coordinator.schedule_midnight_update()

    # Create remaining sensors (average price + financial balance) - they will show as unavailable initially
    remaining_entities = []

    # Create average price sensors for buy
    if buy_coord:
        for period in ("daily", "monthly", "yearly"):
            remaining_entities.append(PstrykAveragePriceSensor(
                cost_coordinator, buy_coord, period, entry.entry_id
            ))

    # Create average price sensors for sell
    if sell_coord:
        for period in ("daily", "monthly", "yearly"):
            remaining_entities.append(PstrykAveragePriceSensor(
                cost_coordinator, sell_coord, period, entry.entry_id
            ))

    # Create financial balance sensors
    for period in ("daily", "monthly", "yearly"):
        remaining_entities.append(PstrykFinancialBalanceSensor(
            cost_coordinator, period, entry.entry_id
        ))

    # Register ALL sensors immediately:
    # - Current price sensors (2) with data
    # - Remaining sensors (15) as unavailable until cost coordinator loads
    _LOGGER.info("Registering %d current price sensors with data and %d additional sensors as unavailable",
                 len(entities), len(remaining_entities))
    async_add_entities(entities + remaining_entities)

    # Load cost coordinator data in background - sensors will automatically update when data arrives
    async def lazy_load_cost_data():
        """Load cost coordinator data in background - sensors update automatically via coordinator."""
        _LOGGER.info("Waiting 15 seconds before loading cost coordinator data")
        await asyncio.sleep(15)

        _LOGGER.info("Loading cost coordinator data in background")
        try:
            # Load cost coordinator with all resolutions
            data = await cost_coordinator._async_update_data(fetch_all=True)
            cost_coordinator.data = data
            cost_coordinator.last_update_success = True
            # Notify all listening sensors that data is available
            cost_coordinator.async_update_listeners()
            _LOGGER.info("Cost coordinator loaded successfully - %d sensors updated",
                        len(remaining_entities))
        except Exception as err:
            _LOGGER.warning("Failed to load cost coordinator: %s. %d sensors remain unavailable.",
                          err, len(remaining_entities))
            cost_coordinator.last_update_success = False
            cost_coordinator.data = None

    # Start background data loading
    hass.async_create_task(lazy_load_cost_data())


class PstrykPriceSensor(CoordinatorEntity, SensorEntity):
    """Combined price sensor with table data attributes."""
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PstrykDataUpdateCoordinator, price_type: str, top_count: int, worst_count: int, entry_id: str):
        super().__init__(coordinator)
        self.price_type = price_type
        self.top_count = top_count
        self.worst_count = worst_count
        self.entry_id = entry_id
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._cached_sorted_prices = None
        self._last_data_hash = None
        
    async def async_added_to_hass(self):
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()

    @property
    def name(self) -> str:
        return f"Pstryk Current {self.price_type.title()} Price"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.price_type}_price"
    
    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, "pstryk_energy")},
            "name": "Pstryk Energy",
            "manufacturer": "Pstryk",
            "model": "Energy Price Monitor",
            "sw_version": get_integration_version(self.hass),
        }

    def _get_current_price(self):
        """Get current price based on current time."""
        if not self.coordinator.data or not self.coordinator.data.get("prices"):
            return None
            
        now_utc = dt_util.utcnow()
        for price_entry in self.coordinator.data.get("prices", []):
            try:
                if "start" not in price_entry:
                    continue
                    
                price_datetime = dt_util.parse_datetime(price_entry["start"])
                if not price_datetime:
                    continue
                    
                # Konwersja do UTC dla porównania
                price_datetime_utc = dt_util.as_utc(price_datetime)
                price_end_utc = price_datetime_utc + timedelta(hours=1)
                
                if price_datetime_utc <= now_utc < price_end_utc:
                    return price_entry.get("price")
            except Exception as e:
                _LOGGER.error("Error determining current price: %s", str(e))
                
        return None
    
    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None

        # Get current price based on actual time - NO FALLBACK to old data!
        current_price = self._get_current_price()

        # If we can't find current price, sensor should be unavailable (return None)
        # This prevents automations from running with wrong/old prices
        return current_price

    @property
    def native_unit_of_measurement(self) -> str:
        return "PLN/kWh"
    
    def _get_next_hour_price(self) -> dict:
        """Get price data for the next hour."""
        if not self.coordinator.data:
            return None
            
        now = dt_util.as_local(dt_util.utcnow())
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        
        # Use translations for debug messages
        debug_msg = _TRANSLATIONS_CACHE.get(
            "debug.looking_for_next_hour", 
            "Looking for price for next hour: {next_hour}"
        ).format(next_hour=next_hour.strftime("%Y-%m-%d %H:%M:%S"))
        _LOGGER.debug(debug_msg)
        
        # Check if we're looking for the next day's hour (midnight)
        is_looking_for_next_day = next_hour.day != now.day
        
        # First check in prices_today
        price_found = None
        if self.coordinator.data.get("prices_today"):
            for price_data in self.coordinator.data.get("prices_today", []):
                if "start" not in price_data:
                    continue
                    
                try:
                    price_datetime = dt_util.parse_datetime(price_data["start"])
                    if not price_datetime:
                        continue
                        
                    price_datetime = dt_util.as_local(price_datetime)
                    
                    if price_datetime.hour == next_hour.hour and price_datetime.day == next_hour.day:
                        price_found = price_data.get("price")
                        _LOGGER.debug("Found price for %s in today's list: %s", next_hour.strftime("%Y-%m-%d %H:%M:%S"), price_found)
                        return price_found
                except Exception as e:
                    error_msg = _TRANSLATIONS_CACHE.get(
                        "debug.error_processing_date", 
                        "Error processing date: {error}"
                    ).format(error=str(e))
                    _LOGGER.error(error_msg)
        
        # Always check the full list as a fallback, regardless of day
        if self.coordinator.data.get("prices"):
            _LOGGER.debug("Looking for price in full 48h list as fallback")
            
            for price_data in self.coordinator.data.get("prices", []):
                if "start" not in price_data:
                    continue
                    
                try:
                    price_datetime = dt_util.parse_datetime(price_data["start"])
                    if not price_datetime:
                        continue
                        
                    price_datetime = dt_util.as_local(price_datetime)
                    
                    # Check if this matches the hour and day we're looking for
                    if price_datetime.hour == next_hour.hour and price_datetime.day == next_hour.day:
                        price_found = price_data.get("price")
                        _LOGGER.debug("Found price for %s in full 48h list: %s", next_hour.strftime("%Y-%m-%d %H:%M:%S"), price_found)
                        return price_found
                except Exception as e:
                    full_list_error_msg = _TRANSLATIONS_CACHE.get(
                        "debug.error_processing_full_list", 
                        "Error processing date for full list: {error}"
                    ).format(error=str(e))
                    _LOGGER.error(full_list_error_msg)
        
        # If no price found for next hour
        if is_looking_for_next_day:
            midnight_msg = _TRANSLATIONS_CACHE.get(
                "debug.no_price_midnight", 
                "No price found for next day midnight. Data probably not loaded yet."
            )
            _LOGGER.info(midnight_msg)
        else:
            no_price_msg = _TRANSLATIONS_CACHE.get(
                "debug.no_price_next_hour", 
                "No price found for next hour: {next_hour}"
            ).format(next_hour=next_hour.strftime("%Y-%m-%d %H:%M:%S"))
            _LOGGER.warning(no_price_msg)
                
        return None
    
    def _get_cached_sorted_prices(self, today):
        """Get cached sorted prices or compute if data changed."""
        # Create a simple hash of the data to detect changes
        data_hash = hash(tuple((p["start"], p["price"]) for p in today))
        
        if self._last_data_hash != data_hash or self._cached_sorted_prices is None:
            _LOGGER.debug("Price data changed, recalculating sorted prices")
            
            # Sortowanie dla najlepszych cen
            sorted_best = sorted(
                today,
                key=lambda x: x["price"],
                reverse=(self.price_type == "sell"),
            )
            
            # Sortowanie dla najgorszych cen (odwrotna kolejność sortowania)
            sorted_worst = sorted(
                today,
                key=lambda x: x["price"],
                reverse=(self.price_type != "sell"),
            )
            
            self._cached_sorted_prices = {
                "best": sorted_best[: self.top_count],
                "worst": sorted_worst[: self.worst_count]
            }
            self._last_data_hash = data_hash
        
        return self._cached_sorted_prices
    
    def _is_likely_placeholder_data(self, prices_for_day):
        """Check if prices for a day are likely placeholders.
        
        Returns True if:
        - There are no prices
        - ALL prices have exactly the same value (suggesting API returned default values)
        - There are too many consecutive hours with the same value (e.g., 10+ hours)
        """
        if not prices_for_day:
            return True
            
        # Get all price values
        price_values = [p.get("price") for p in prices_for_day if p.get("price") is not None]
        
        if not price_values:
            return True
        
        # If we have less than 20 prices for a day, it's incomplete data
        if len(price_values) < 20:
            _LOGGER.debug(f"Only {len(price_values)} prices for the day, likely incomplete data")
            return True
            
        # Check if ALL values are identical
        unique_values = set(price_values)
        if len(unique_values) == 1:
            _LOGGER.debug(f"All {len(price_values)} prices have the same value ({price_values[0]}), likely placeholders")
            return True
        
        # Additional check: if more than 90% of values are the same, it's suspicious
        most_common_value = max(set(price_values), key=price_values.count)
        count_most_common = price_values.count(most_common_value)
        if count_most_common / len(price_values) > 0.9:
            _LOGGER.debug(f"{count_most_common}/{len(price_values)} prices have value {most_common_value}, likely placeholders")
            return True
            
        return False
    
    def _count_consecutive_same_values(self, prices):
        """Count maximum consecutive hours with the same price."""
        if not prices:
            return 0
            
        # Sort by time to ensure consecutive checking
        sorted_prices = sorted(prices, key=lambda x: x.get("start", ""))
        
        max_consecutive = 1
        current_consecutive = 1
        last_value = None
        
        for price in sorted_prices:
            value = price.get("price")
            if value is not None:
                if value == last_value:
                    current_consecutive += 1
                    max_consecutive = max(max_consecutive, current_consecutive)
                else:
                    current_consecutive = 1
                last_value = value
                    
        return max_consecutive
    
    def _get_mqtt_price_count(self):
        """Get the actual count of prices that would be published to MQTT."""
        if not self.coordinator.data:
            return 0
            
        if not self.coordinator.mqtt_48h_mode:
            # If not in 48h mode, we only publish today's prices
            prices_today = self.coordinator.data.get("prices_today", [])
            return len(prices_today)
        else:
            # In 48h mode, we need to count valid prices
            all_prices = self.coordinator.data.get("prices", [])
            
            # Just count today's prices as they're always valid
            now = dt_util.as_local(dt_util.utcnow())
            today_str = now.strftime("%Y-%m-%d")
            today_prices = [p for p in all_prices if p.get("start", "").startswith(today_str)]
            
            # For tomorrow, check if data looks valid
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            tomorrow_prices = [p for p in all_prices if p.get("start", "").startswith(tomorrow_str)]
            
            # Count today's prices always
            valid_count = len(today_prices)
            
            # Add tomorrow's prices only if they look like real data
            if tomorrow_prices and not self._is_likely_placeholder_data(tomorrow_prices):
                valid_count += len(tomorrow_prices)
            
            return valid_count
    
    def _get_sunrise_sunset_average(self, today_prices):
        """Calculate average price between sunrise and sunset."""
        if not today_prices:
            return None
            
        # Get sun entity
        sun_entity = self.hass.states.get("sun.sun")
        if not sun_entity:
            _LOGGER.debug("Sun entity not available")
            return None
            
        # Get sunrise and sunset times from attributes
        sunrise_attr = sun_entity.attributes.get("next_rising")
        sunset_attr = sun_entity.attributes.get("next_setting")
        
        if not sunrise_attr or not sunset_attr:
            _LOGGER.debug("Sunrise/sunset times not available")
            return None
            
        # Parse sunrise and sunset times
        try:
            sunrise = dt_util.parse_datetime(sunrise_attr)
            sunset = dt_util.parse_datetime(sunset_attr)
            
            if not sunrise or not sunset:
                return None
                
            # Convert to local time
            sunrise_local = dt_util.as_local(sunrise)
            sunset_local = dt_util.as_local(sunset)
            
            # If sunrise is tomorrow, use today's sunrise from calculation
            now = dt_util.now()
            if sunrise_local.date() > now.date():
                # Calculate approximate sunrise for today (subtract 24h)
                sunrise_local = sunrise_local - timedelta(days=1)
                
            # If sunset is tomorrow, we're after sunset today
            if sunset_local.date() > now.date():
                # Use previous sunset
                sunset_local = sunset_local - timedelta(days=1)
                
            _LOGGER.debug(f"Calculating s/s average between {sunrise_local.strftime('%H:%M')} and {sunset_local.strftime('%H:%M')}")
            
            # Get prices between sunrise and sunset
            sunrise_sunset_prices = []
            
            for price_entry in today_prices:
                if "start" not in price_entry or "price" not in price_entry:
                    continue
                    
                price_time = dt_util.parse_datetime(price_entry["start"])
                if not price_time:
                    continue
                    
                price_time_local = dt_util.as_local(price_time)
                
                # Check if price hour is between sunrise and sunset
                # We check the start of the hour
                if sunrise_local <= price_time_local < sunset_local:
                    price_value = price_entry.get("price")
                    if price_value is not None:
                        sunrise_sunset_prices.append(price_value)
                        
            # Calculate average
            if sunrise_sunset_prices:
                avg = round(sum(sunrise_sunset_prices) / len(sunrise_sunset_prices), 2)
                _LOGGER.debug(f"S/S average calculated from {len(sunrise_sunset_prices)} hours: {avg}")
                return avg
            else:
                _LOGGER.debug("No prices found between sunrise and sunset")
                return None
                
        except Exception as e:
            _LOGGER.error(f"Error calculating sunrise/sunset average: {e}")
            return None
        
    @property
    def extra_state_attributes(self) -> dict:
        """Include the price table attributes in the current price sensor."""
        now = dt_util.as_local(dt_util.utcnow())
        
        # Get translated attribute names from cache
        next_hour_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.next_hour", 
            "Next hour"
        )
        
        using_cached_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.using_cached_data", 
            "Using cached data"
        )
        
        all_prices_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.all_prices", 
            "All prices"
        )
        
        best_prices_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.best_prices", 
            "Best prices"
        )
        
        worst_prices_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.worst_prices", 
            "Worst prices"
        )
        
        best_count_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.best_count", 
            "Best count"
        )
        
        worst_count_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.worst_count", 
            "Worst count"
        )
        
        price_count_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.price_count", 
            "Price count"
        )
        
        last_updated_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.last_updated", 
            "Last updated"
        )
        
        avg_price_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.avg_price", 
            "Average price today"
        )
        
        avg_price_remaining_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.avg_price_remaining",
            "Average price remaining"
        )
        
        avg_price_full_day_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.avg_price_full_day",
            "Average price full day"
        )
        
        tomorrow_available_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.tomorrow_available", 
            "Tomorrow prices available"
        )
        
        mqtt_price_count_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.mqtt_price_count", 
            "MQTT price count"
        )
        
        # Add sunrise/sunset average key
        avg_price_sunrise_sunset_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.avg_price_sunrise_sunset",
            "Average price today s/s"
        )
        
        if self.coordinator.data is None:
            return {
                f"{avg_price_key} /0": None,
                f"{avg_price_key} /24": None,
                avg_price_sunrise_sunset_key: None,
                next_hour_key: None,
                all_prices_key: [],
                best_prices_key: [],
                worst_prices_key: [],
                best_count_key: self.top_count,
                worst_count_key: self.worst_count,
                price_count_key: 0,
                using_cached_key: False,
                tomorrow_available_key: False,
                mqtt_price_count_key: 0
            }
            
        next_hour_data = self._get_next_hour_price()
        today = self.coordinator.data.get("prices_today", [])
        is_cached = self.coordinator.data.get("is_cached", False)
        
        # Calculate average price for remaining hours today (from current hour)
        avg_price_remaining = None
        remaining_hours_count = 0
        avg_price_full_day = None
        
        if today:
            # Full day average (all 24 hours)
            total_price_full = sum(p.get("price", 0) for p in today if p.get("price") is not None)
            valid_prices_count_full = sum(1 for p in today if p.get("price") is not None)
            if valid_prices_count_full > 0:
                avg_price_full_day = round(total_price_full / valid_prices_count_full, 2)
            
            # Remaining hours average (from current hour onwards)
            current_hour = now.strftime("%Y-%m-%dT%H:")
            remaining_prices = []
            
            for p in today:
                if p.get("price") is not None and p.get("start", "") >= current_hour:
                    remaining_prices.append(p.get("price"))
            
            remaining_hours_count = len(remaining_prices)
            if remaining_hours_count > 0:
                avg_price_remaining = round(sum(remaining_prices) / remaining_hours_count, 2)
        
        # Calculate sunrise to sunset average
        avg_price_sunrise_sunset = self._get_sunrise_sunset_average(today)
        
        # Create keys with hour count in user's preferred format
        avg_price_remaining_with_hours = f"{avg_price_key} /{remaining_hours_count}"
        avg_price_full_day_with_hours = f"{avg_price_key} /24"
        
        # Check if tomorrow's prices are available (more robust check)
        all_prices = self.coordinator.data.get("prices", [])
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow_prices = []
        
        # Only check for tomorrow prices if we have a reasonable amount of data
        if len(all_prices) > 0:
            tomorrow_prices = [p for p in all_prices if p.get("start", "").startswith(tomorrow)]
        
        # Log what we found for debugging
        if tomorrow_prices:
            unique_values = set(p.get("price") for p in tomorrow_prices if p.get("price") is not None)
            consecutive = self._count_consecutive_same_values(tomorrow_prices)
            _LOGGER.debug(
                f"Tomorrow has {len(tomorrow_prices)} prices, "
                f"{len(unique_values)} unique values, "
                f"max {consecutive} consecutive same values"
            )
        
        # Tomorrow is available only if:
        # 1. We have at least 20 hours of data for tomorrow
        # 2. The data doesn't look like placeholders
        tomorrow_available = (
            len(tomorrow_prices) >= 20 and 
            not self._is_likely_placeholder_data(tomorrow_prices)
        )
        
        # Get cached sorted prices
        sorted_prices = self._get_cached_sorted_prices(today) if today else {"best": [], "worst": []}
        
        # Get actual MQTT price count
        mqtt_price_count = self._get_mqtt_price_count()
        
        return {
            avg_price_remaining_with_hours: avg_price_remaining,
            avg_price_full_day_with_hours: avg_price_full_day,
            avg_price_sunrise_sunset_key: avg_price_sunrise_sunset,
            next_hour_key: next_hour_data,
            all_prices_key: today,
            best_prices_key: sorted_prices["best"],
            worst_prices_key: sorted_prices["worst"],
            best_count_key: self.top_count,
            worst_count_key: self.worst_count,
            price_count_key: len(today),
            last_updated_key: now.strftime("%Y-%m-%d %H:%M:%S"),
            using_cached_key: is_cached,
            tomorrow_available_key: tomorrow_available,
            mqtt_price_count_key: mqtt_price_count,
            "mqtt_48h_mode": self.coordinator.mqtt_48h_mode
        }
        
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self.coordinator.data is not None


class PstrykAveragePriceSensor(RestoreEntity, SensorEntity):
    """Average price sensor using weighted averages from API data."""
    _attr_state_class = SensorStateClass.MEASUREMENT
    
    def __init__(self, cost_coordinator: PstrykCostDataUpdateCoordinator, 
                 price_coordinator: PstrykDataUpdateCoordinator,
                 period: str, entry_id: str):
        """Initialize the average price sensor."""
        self.cost_coordinator = cost_coordinator
        self.price_coordinator = price_coordinator
        self.price_type = price_coordinator.price_type
        self.period = period  # 'daily', 'monthly' or 'yearly'
        self.entry_id = entry_id
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._state = None
        self._energy_bought = 0.0
        self._energy_sold = 0.0
        self._total_cost = 0.0
        self._total_revenue = 0.0
        
    async def async_added_to_hass(self):
        """Restore state when entity is added."""
        await super().async_added_to_hass()
        
        # Subscribe to cost coordinator updates
        self.async_on_remove(
            self.cost_coordinator.async_add_listener(self._handle_cost_update)
        )
        
        # Restore previous state
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._state = float(last_state.state)
                
                # Restore attributes
                if last_state.attributes:
                    self._energy_bought = float(last_state.attributes.get("energy_bought", 0))
                    self._energy_sold = float(last_state.attributes.get("energy_sold", 0))
                    self._total_cost = float(last_state.attributes.get("total_cost", 0))
                    self._total_revenue = float(last_state.attributes.get("total_revenue", 0))
                        
                _LOGGER.debug("Restored weighted average for %s %s: %s", 
                            self.price_type, self.period, self._state)
            except (ValueError, TypeError):
                _LOGGER.warning("Could not restore state for %s", self.name)
        
    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        period_name = _TRANSLATIONS_CACHE.get(
            f"entity.sensor.period_{self.period}",
            self.period.title()
        )
        return f"Pstryk {self.price_type.title()} {period_name} Average"
        
    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{DOMAIN}_{self.price_type}_{self.period}_average"
        
    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, "pstryk_energy")},
            "name": "Pstryk Energy",
            "manufacturer": "Pstryk",
            "model": "Energy Price Monitor",
            "sw_version": get_integration_version(self.hass),
        }
        
    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._state
        
    @property
    def native_unit_of_measurement(self) -> str:
        """Return the unit of measurement."""
        return "PLN/kWh"
        
    @property
    def extra_state_attributes(self) -> dict:
        """Return extra state attributes."""
        period_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.period",
            "Period"
        )
        calculation_method_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.calculation_method",
            "Calculation method"
        )
        energy_bought_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.energy_bought",
            "Energy bought"
        )
        energy_sold_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.energy_sold",
            "Energy sold"
        )
        total_cost_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.total_cost",
            "Total cost"
        )
        total_revenue_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.total_revenue",
            "Total revenue"
        )
        attrs = {
            period_key: self.period,
            calculation_method_key: "Weighted average",
        }
        
        # Add energy and cost data if available
        if self.price_type == "buy" and self._energy_bought > 0:
            attrs[energy_bought_key] = round(self._energy_bought, 2)
            attrs[total_cost_key] = round(self._total_cost, 2)
        elif self.price_type == "sell" and self._energy_sold > 0:
            attrs[energy_sold_key] = round(self._energy_sold, 2)
            attrs[total_revenue_key] = round(self._total_revenue, 2)
            
        # Add last updated at the bottom
        last_updated_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.last_updated", 
            "Last updated"
        )
        now = dt_util.now()
        attrs[last_updated_key] = now.strftime("%Y-%m-%d %H:%M:%S")
        
        return attrs
        
    @callback
    def _handle_cost_update(self) -> None:
        """Handle updated data from the cost coordinator."""
        if not self.cost_coordinator or not self.cost_coordinator.data:
            return
            
        period_data = self.cost_coordinator.data.get(self.period)
        if not period_data:
            return
            
        # Calculate weighted average based on actual costs and usage
        if self.price_type == "buy":
            # For buy price: total cost / total energy bought
            total_cost = abs(period_data.get("total_cost", 0))  # Already calculated in coordinator
            energy_bought = period_data.get("fae_usage", 0)  # kWh from usage API
            
            if energy_bought > 0:
                self._state = round(total_cost / energy_bought, 4)
                self._energy_bought = energy_bought
                self._total_cost = total_cost
            else:
                self._state = None
                
        elif self.price_type == "sell":
            # For sell price: total revenue / total energy sold
            total_revenue = period_data.get("total_sold", 0)
            energy_sold = period_data.get("rae_usage", 0)  # kWh from usage API
            
            if energy_sold > 0:
                self._state = round(total_revenue / energy_sold, 4)
                self._energy_sold = energy_sold
                self._total_revenue = total_revenue
            else:
                self._state = None
                
        self.async_write_ha_state()
        
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (self.cost_coordinator is not None and 
                self.cost_coordinator.last_update_success and
                self.cost_coordinator.data is not None)


class PstrykFinancialBalanceSensor(CoordinatorEntity, SensorEntity):
    """Financial balance sensor that gets data directly from API."""
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY
    
    def __init__(self, coordinator: PstrykCostDataUpdateCoordinator, 
                 period: str, entry_id: str):
        """Initialize the financial balance sensor."""
        super().__init__(coordinator)
        self.period = period  # 'daily', 'monthly', or 'yearly'
        self.entry_id = entry_id
        
    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        period_name = _TRANSLATIONS_CACHE.get(
            f"entity.sensor.period_{self.period}",
            self.period.title()
        )
        balance_text = _TRANSLATIONS_CACHE.get(
            "entity.sensor.financial_balance",
            "Financial Balance"
        )
        return f"Pstryk {period_name} {balance_text}"
        
    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{DOMAIN}_financial_balance_{self.period}"
        
    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, "pstryk_energy")},
            "name": "Pstryk Energy",
            "manufacturer": "Pstryk",
            "model": "Energy Price Monitor",
            "sw_version": get_integration_version(self.hass),
        }
        
    @property
    def native_value(self):
        """Return the state of the sensor from API data."""
        if not self.coordinator.data:
            return None
            
        period_data = self.coordinator.data.get(self.period)
        if not period_data or "total_balance" not in period_data:
            return None
            
        # Get the balance value from API
        balance = period_data.get("total_balance")
        
        # Return exact value from API without rounding
        return balance
        
    @property
    def native_unit_of_measurement(self) -> str:
        """Return the unit of measurement."""
        return "PLN"
        
    @property
    def icon(self) -> str:
        """Return the icon based on balance."""
        if self.native_value is None:
            return "mdi:currency-usd-off"
        elif self.native_value < 0:
            return "mdi:cash-minus"  # We're paying
        elif self.native_value > 0:
            return "mdi:cash-plus"   # We're earning
        else:
            return "mdi:cash"
            
    @property
    def extra_state_attributes(self) -> dict:
        """Return extra state attributes from API data."""
        if not self.coordinator.data or not self.coordinator.data.get(self.period):
            return {}
            
        period_data = self.coordinator.data.get(self.period)
        frame = period_data.get("frame", {})
        
        # Get translated attribute names
        buy_cost_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.buy_cost",
            "Buy cost"
        )
        sell_revenue_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.sell_revenue",
            "Sell revenue"
        )
        period_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.period",
            "Period"
        )
        net_balance_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.balance",
            "Balance"
        )
        distribution_cost_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.distribution_cost",
            "Distribution cost"
        )
        excise_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.excise",
            "Excise"
        )
        vat_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.vat",
            "VAT"
        )
        service_cost_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.service_cost",
            "Service cost"
        )
        energy_bought_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.energy_bought",
            "Energy bought"
        )
        energy_sold_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.energy_sold", 
            "Energy sold"
        )
        attrs = {
            period_key: self.period,
            net_balance_key: period_data.get("total_balance", 0),
            buy_cost_key: period_data.get("total_cost", 0),
            sell_revenue_key: period_data.get("total_sold", 0),
            energy_bought_key: period_data.get("fae_usage", 0),
            energy_sold_key: period_data.get("rae_usage", 0),
        }
        
        # Add detailed cost breakdown if available
        if frame:
            # Konwertuj daty UTC na lokalne
            start_utc = frame.get("start")
            end_utc = frame.get("end")
            
            start_local = dt_util.as_local(dt_util.parse_datetime(start_utc)) if start_utc else None
            end_local = dt_util.as_local(dt_util.parse_datetime(end_utc)) if end_utc else None

            attrs.update({
                distribution_cost_key: frame.get("var_dist_cost_net", 0) + frame.get("fix_dist_cost_net", 0),
                excise_key: frame.get("excise", 0),
                vat_key: frame.get("vat", 0),
                service_cost_key: frame.get("service_cost_net", 0),
                "start": start_local.strftime("%Y-%m-%d") if start_local else None,
                "end": end_local.strftime("%Y-%m-%d") if end_local else None,
            })
            
        # Add last updated at the bottom
        last_updated_key = _TRANSLATIONS_CACHE.get(
            "entity.sensor.last_updated", 
            "Last updated"
        )
        now = dt_util.now()
        attrs[last_updated_key] = now.strftime("%Y-%m-%d %H:%M:%S")
        
        return attrs
        
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self.coordinator.data is not None
