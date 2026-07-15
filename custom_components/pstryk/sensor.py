import logging
import asyncio
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util
from .update_coordinator import PstrykDataUpdateCoordinator, is_likely_placeholder_data
from .energy_cost_coordinator import PstrykCostDataUpdateCoordinator
from .api_client import PstrykAPIClient
from .const import (
    DOMAIN,
    CONF_MQTT_48H_MODE,
    CONF_JSON_SENSOR,
    CONF_RETRY_ATTEMPTS,
    CONF_RETRY_DELAY,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY
)
from homeassistant.loader import async_get_integration

_LOGGER = logging.getLogger(__name__)

_VERSION_CACHE = None


def get_integration_version(hass: HomeAssistant) -> str:
    return _VERSION_CACHE or "unknown"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    global _VERSION_CACHE
    if _VERSION_CACHE is None:
        try:
            integration = await async_get_integration(hass, DOMAIN)
            _VERSION_CACHE = str(integration.version)
        except Exception as ex:
            _LOGGER.warning("Failed to read integration version: %s", ex)

    api_key = hass.data[DOMAIN][entry.entry_id]["api_key"]
    buy_top = entry.options.get("buy_top", entry.data.get("buy_top", 5))
    sell_top = entry.options.get("sell_top", entry.data.get("sell_top", 5))
    buy_worst = entry.options.get("buy_worst", entry.data.get("buy_worst", 5))
    sell_worst = entry.options.get("sell_worst", entry.data.get("sell_worst", 5))
    mqtt_48h_mode = entry.options.get(CONF_MQTT_48H_MODE, False)
    json_sensor_enabled = entry.options.get(CONF_JSON_SENSOR, False)
    retry_attempts = entry.options.get(CONF_RETRY_ATTEMPTS, DEFAULT_RETRY_ATTEMPTS)
    retry_delay = entry.options.get(CONF_RETRY_DELAY, DEFAULT_RETRY_DELAY)

    _LOGGER.debug("Setting up Pstryk sensors with buy_top=%d, sell_top=%d, buy_worst=%d, sell_worst=%d, mqtt_48h_mode=%s, retry_attempts=%d, retry_delay=%ds", 
                 buy_top, sell_top, buy_worst, sell_worst, mqtt_48h_mode, retry_attempts, retry_delay)

    cost_key = f"{entry.entry_id}_cost"

    api_client_key = f"{entry.entry_id}_api_client"
    if api_client_key not in hass.data[DOMAIN]:
        api_client = PstrykAPIClient(hass, api_key)
        hass.data[DOMAIN][api_client_key] = api_client
    else:
        api_client = hass.data[DOMAIN][api_client_key]

    entities = []
    coordinators = []

    for price_type in ("buy", "sell"):
        key = f"{entry.entry_id}_{price_type}"
        coordinator = PstrykDataUpdateCoordinator(
            hass,
            api_client,
            price_type,
            mqtt_48h_mode,
            retry_attempts,
            retry_delay,
            entry.entry_id
        )
        coordinators.append((coordinator, price_type, key))

    cost_coordinator = PstrykCostDataUpdateCoordinator(
        hass,
        api_client,
        retry_attempts,
        retry_delay
    )
    cost_coordinator.last_update_success = False
    coordinators.append((cost_coordinator, "cost", cost_key))

    _LOGGER.info("Starting quick initialization - loading price coordinators only")

    async def safe_initial_fetch(coord, coord_type):
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

    price_coordinators = [(c, t, k) for c, t, k in coordinators if t in ("buy", "sell")]

    initial_refresh_tasks = [
        safe_initial_fetch(coordinator, coordinator_type)
        for coordinator, coordinator_type, _ in price_coordinators
    ]

    refresh_results = await asyncio.gather(*initial_refresh_tasks, return_exceptions=True)

    for i, (coordinator, coordinator_type, key) in enumerate(price_coordinators):
        if isinstance(refresh_results[i], Exception):
            _LOGGER.error("Failed to initialize %s coordinator: %s",
                         coordinator_type, str(refresh_results[i]))
    
    buy_coord = None
    sell_coord = None

    for coordinator, coordinator_type, key in coordinators:
        hass.data[DOMAIN][key] = coordinator

        if coordinator_type in ("buy", "sell"):
            coordinator.schedule_hourly_update()
            coordinator.schedule_midnight_update()

            if mqtt_48h_mode:
                coordinator.schedule_afternoon_update()

            top = buy_top if coordinator_type == "buy" else sell_top
            worst = buy_worst if coordinator_type == "buy" else sell_worst
            entities.append(PstrykPriceSensor(coordinator, coordinator_type, top, worst, entry.entry_id))

            if json_sensor_enabled:
                entities.append(PstrykJsonPriceSensor(coordinator, coordinator_type, entry.entry_id))

            if coordinator_type == "buy":
                buy_coord = coordinator
            elif coordinator_type == "sell":
                sell_coord = coordinator

        elif coordinator_type == "cost":
            coordinator.schedule_hourly_update()
            coordinator.schedule_midnight_update()

    remaining_entities = []

    if buy_coord:
        for period in ("daily", "monthly", "yearly"):
            remaining_entities.append(PstrykAveragePriceSensor(
                cost_coordinator, buy_coord, period, entry.entry_id
            ))

    if sell_coord:
        for period in ("daily", "monthly", "yearly"):
            remaining_entities.append(PstrykAveragePriceSensor(
                cost_coordinator, sell_coord, period, entry.entry_id
            ))

    for period in ("daily", "monthly", "yearly"):
        remaining_entities.append(PstrykFinancialBalanceSensor(
            cost_coordinator, period, entry.entry_id
        ))

    _LOGGER.info("Registering %d current price sensors with data and %d additional sensors as unavailable",
                 len(entities), len(remaining_entities))
    async_add_entities(entities + remaining_entities)

    async def lazy_load_cost_data():
        _LOGGER.info("Waiting 15 seconds before loading cost coordinator data")
        await asyncio.sleep(15)

        _LOGGER.info("Loading cost coordinator data in background")
        try:
            data = await cost_coordinator._async_update_data(fetch_all=True)
            cost_coordinator.data = data
            cost_coordinator.last_update_success = True
            cost_coordinator.async_update_listeners()
            _LOGGER.info("Cost coordinator loaded successfully - %d sensors updated",
                        len(remaining_entities))
        except Exception as err:
            _LOGGER.warning("Failed to load cost coordinator: %s. %d sensors remain unavailable.",
                          err, len(remaining_entities))
            cost_coordinator.last_update_success = False
            cost_coordinator.data = None

    entry.async_create_background_task(hass, lazy_load_cost_data(), f"{DOMAIN}_lazy_cost_load")


class PstrykPriceSensor(CoordinatorEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"All prices", "Best prices", "Worst prices"})

    def __init__(self, coordinator: PstrykDataUpdateCoordinator, price_type: str, top_count: int, worst_count: int, entry_id: str):
        super().__init__(coordinator)
        self.price_type = price_type
        self.top_count = top_count
        self.worst_count = worst_count
        self.entry_id = entry_id
        self.entity_id = f"sensor.{DOMAIN}_current_{price_type}_price"
        self._cached_sorted_prices = None
        self._last_data_hash = None
        
    async def async_added_to_hass(self):
        await super().async_added_to_hass()

    @property
    def name(self) -> str:
        return f"Pstryk Current {self.price_type.title()} Price"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.price_type}_price"
    
    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "pstryk_energy")},
            "name": "Pstryk Energy",
            "manufacturer": "Pstryk",
            "model": "Energy Price Monitor",
            "sw_version": get_integration_version(self.hass),
        }

    def _get_current_price(self):
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

        current_price = self._get_current_price()

        return current_price

    @property
    def native_unit_of_measurement(self) -> str:
        return "PLN/kWh"
    
    def _get_next_hour_price(self) -> dict:
        if not self.coordinator.data:
            return None
            
        now = dt_util.as_local(dt_util.utcnow())
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        
        debug_msg = "Looking for price for next hour: {next_hour}".format(next_hour=next_hour.strftime("%Y-%m-%d %H:%M:%S"))
        _LOGGER.debug(debug_msg)
        
        is_looking_for_next_day = next_hour.day != now.day
        
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
                    error_msg = "Error processing date: {error}".format(error=str(e))
                    _LOGGER.error(error_msg)
        
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
                    
                    if price_datetime.hour == next_hour.hour and price_datetime.day == next_hour.day:
                        price_found = price_data.get("price")
                        _LOGGER.debug("Found price for %s in full 48h list: %s", next_hour.strftime("%Y-%m-%d %H:%M:%S"), price_found)
                        return price_found
                except Exception as e:
                    full_list_error_msg = "Error processing date for full list: {error}".format(error=str(e))
                    _LOGGER.error(full_list_error_msg)
        
        if is_looking_for_next_day:
            midnight_msg = "No price found for next day midnight. Data probably not loaded yet."
            _LOGGER.info(midnight_msg)
        else:
            no_price_msg = "No price found for next hour: {next_hour}".format(next_hour=next_hour.strftime("%Y-%m-%d %H:%M:%S"))
            _LOGGER.warning(no_price_msg)
                
        return None
    
    def _get_cached_sorted_prices(self, today):
        data_hash = hash(tuple((p["start"], p["price"]) for p in today))
        
        if self._last_data_hash != data_hash or self._cached_sorted_prices is None:
            _LOGGER.debug("Price data changed, recalculating sorted prices")
            
            sorted_best = sorted(
                today,
                key=lambda x: x["price"],
                reverse=(self.price_type == "sell"),
            )
            
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
    
    def _count_consecutive_same_values(self, prices):
        if not prices:
            return 0
            
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
        if not self.coordinator.data:
            return 0
            
        if not self.coordinator.mqtt_48h_mode:
            prices_today = self.coordinator.data.get("prices_today", [])
            return len(prices_today)
        else:
            all_prices = self.coordinator.data.get("prices", [])
            
            now = dt_util.as_local(dt_util.utcnow())
            today_str = now.strftime("%Y-%m-%d")
            today_prices = [p for p in all_prices if p.get("start", "").startswith(today_str)]
            
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            tomorrow_prices = [p for p in all_prices if p.get("start", "").startswith(tomorrow_str)]
            
            valid_count = len(today_prices)
            
            if tomorrow_prices and not is_likely_placeholder_data(tomorrow_prices):
                valid_count += len(tomorrow_prices)
            
            return valid_count
    
    def _get_sunrise_sunset_average(self, today_prices):
        if not today_prices:
            return None
            
        sun_entity = self.hass.states.get("sun.sun")
        if not sun_entity:
            _LOGGER.debug("Sun entity not available")
            return None
            
        sunrise_attr = sun_entity.attributes.get("next_rising")
        sunset_attr = sun_entity.attributes.get("next_setting")
        
        if not sunrise_attr or not sunset_attr:
            _LOGGER.debug("Sunrise/sunset times not available")
            return None
            
        try:
            sunrise = dt_util.parse_datetime(sunrise_attr)
            sunset = dt_util.parse_datetime(sunset_attr)
            
            if not sunrise or not sunset:
                return None
                
            sunrise_local = dt_util.as_local(sunrise)
            sunset_local = dt_util.as_local(sunset)
            
            now = dt_util.now()
            if sunrise_local.date() > now.date():
                sunrise_local = sunrise_local - timedelta(days=1)
                
            if sunset_local.date() > now.date():
                sunset_local = sunset_local - timedelta(days=1)
                
            _LOGGER.debug(f"Calculating s/s average between {sunrise_local.strftime('%H:%M')} and {sunset_local.strftime('%H:%M')}")
            
            sunrise_sunset_prices = []
            
            for price_entry in today_prices:
                if "start" not in price_entry or "price" not in price_entry:
                    continue
                    
                price_time = dt_util.parse_datetime(price_entry["start"])
                if not price_time:
                    continue
                    
                price_time_local = dt_util.as_local(price_time)
                
                if sunrise_local <= price_time_local < sunset_local:
                    price_value = price_entry.get("price")
                    if price_value is not None:
                        sunrise_sunset_prices.append(price_value)
                        
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
        now = dt_util.as_local(dt_util.utcnow())
        
        next_hour_key = "Next hour"
        
        using_cached_key = "Using cached data"
        
        all_prices_key = "All prices"
        
        best_prices_key = "Best prices"
        
        worst_prices_key = "Worst prices"
        
        best_count_key = "Best count"
        
        worst_count_key = "Worst count"
        
        price_count_key = "Price count"
        
        last_updated_key = "Last updated"
        
        avg_price_key = "Average price today"
        
        avg_price_remaining_key = "Average price remaining"
        
        avg_price_full_day_key = "Average price full day"
        
        tomorrow_available_key = "Tomorrow prices available"
        
        mqtt_price_count_key = "MQTT price count"
        
        avg_price_sunrise_sunset_key = "Average price today s/s"
        
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
        
        avg_price_remaining = None
        remaining_hours_count = 0
        avg_price_full_day = None
        
        if today:
            total_price_full = sum(p.get("price", 0) for p in today if p.get("price") is not None)
            valid_prices_count_full = sum(1 for p in today if p.get("price") is not None)
            if valid_prices_count_full > 0:
                avg_price_full_day = round(total_price_full / valid_prices_count_full, 2)
            
            current_hour = now.strftime("%Y-%m-%dT%H:")
            remaining_prices = []
            
            for p in today:
                if p.get("price") is not None and p.get("start", "") >= current_hour:
                    remaining_prices.append(p.get("price"))
            
            remaining_hours_count = len(remaining_prices)
            if remaining_hours_count > 0:
                avg_price_remaining = round(sum(remaining_prices) / remaining_hours_count, 2)
        
        avg_price_sunrise_sunset = self._get_sunrise_sunset_average(today)
        
        avg_price_remaining_with_hours = f"{avg_price_key} /{remaining_hours_count}"
        avg_price_full_day_with_hours = f"{avg_price_key} /24"
        
        all_prices = self.coordinator.data.get("prices", [])
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow_prices = []
        
        if len(all_prices) > 0:
            tomorrow_prices = [p for p in all_prices if p.get("start", "").startswith(tomorrow)]
        
        if tomorrow_prices:
            unique_values = set(p.get("price") for p in tomorrow_prices if p.get("price") is not None)
            consecutive = self._count_consecutive_same_values(tomorrow_prices)
            _LOGGER.debug(
                f"Tomorrow has {len(tomorrow_prices)} prices, "
                f"{len(unique_values)} unique values, "
                f"max {consecutive} consecutive same values"
            )
        
        tomorrow_available = (
            len(tomorrow_prices) >= 20 and 
            not is_likely_placeholder_data(tomorrow_prices)
        )
        
        sorted_prices = self._get_cached_sorted_prices(today) if today else {"best": [], "worst": []}
        
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
        return self.coordinator.last_update_success and self.coordinator.data is not None


class PstrykJsonPriceSensor(CoordinatorEntity, SensorEntity):
    _attr_icon = "mdi:code-json"
    _unrecorded_attributes = frozenset({"prices_today", "prices_tomorrow", "prices"})

    def __init__(self, coordinator: PstrykDataUpdateCoordinator, price_type: str, entry_id: str):
        super().__init__(coordinator)
        self.price_type = price_type
        self.entry_id = entry_id
        self.entity_id = f"sensor.{DOMAIN}_json_{price_type}"

    @property
    def name(self) -> str:
        return f"Pstryk Json {self.price_type.title()}"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.price_type}_json"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "pstryk_energy")},
            "name": "Pstryk Energy",
            "manufacturer": "Pstryk",
            "model": "Energy Price Monitor",
            "sw_version": get_integration_version(self.hass),
        }

    @property
    def native_unit_of_measurement(self) -> str:
        return "PLN/kWh"

    def _price_entries(self):
        data = self.coordinator.data or {}
        entries = []
        for p in data.get("prices", []):
            if p.get("price") is None:
                continue
            t = dt_util.parse_datetime(p.get("start", ""))
            if t is None:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            entries.append({"time": t, "price": p["price"]})
        entries.sort(key=lambda e: e["time"])
        return entries

    @property
    def native_value(self):
        now = dt_util.now()
        for e in self._price_entries():
            if e["time"] <= now < e["time"] + timedelta(hours=1):
                return e["price"]
        return None

    @property
    def extra_state_attributes(self) -> dict:
        entries = self._price_entries()
        today = dt_util.now().date()
        tomorrow = today + timedelta(days=1)
        today_entries = [e for e in entries if e["time"].date() == today]
        tomorrow_entries = [e for e in entries if e["time"].date() == tomorrow]
        if is_likely_placeholder_data(tomorrow_entries):
            tomorrow_entries = []
        return {
            "prices_today": today_entries,
            "prices_tomorrow": tomorrow_entries,
            "prices": today_entries + tomorrow_entries,
        }

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data is not None


class PstrykAveragePriceSensor(RestoreEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    
    def __init__(self, cost_coordinator: PstrykCostDataUpdateCoordinator, 
                 price_coordinator: PstrykDataUpdateCoordinator,
                 period: str, entry_id: str):
        self.cost_coordinator = cost_coordinator
        self.price_coordinator = price_coordinator
        self.price_type = price_coordinator.price_type
        self.period = period
        self.entry_id = entry_id
        self.entity_id = f"sensor.{DOMAIN}_{self.price_type}_{period}_average"
        self._state = None
        self._energy_bought = 0.0
        self._energy_sold = 0.0
        self._total_cost = 0.0
        self._total_revenue = 0.0
        
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        
        self.async_on_remove(
            self.cost_coordinator.async_add_listener(self._handle_cost_update)
        )
        
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._state = float(last_state.state)
                
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
        period_name = self.period.title()
        return f"Pstryk {self.price_type.title()} {period_name} Average"
        
    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.price_type}_{self.period}_average"
        
    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "pstryk_energy")},
            "name": "Pstryk Energy",
            "manufacturer": "Pstryk",
            "model": "Energy Price Monitor",
            "sw_version": get_integration_version(self.hass),
        }
        
    @property
    def native_value(self):
        return self._state
        
    @property
    def native_unit_of_measurement(self) -> str:
        return "PLN/kWh"
        
    @property
    def extra_state_attributes(self) -> dict:
        period_key = "Period"
        calculation_method_key = "Calculation method"
        energy_bought_key = "Energy bought"
        energy_sold_key = "Energy sold"
        total_cost_key = "Total cost"
        total_revenue_key = "Total revenue"
        attrs = {
            period_key: self.period,
            calculation_method_key: "Weighted average",
        }
        
        if self.price_type == "buy" and self._energy_bought > 0:
            attrs[energy_bought_key] = round(self._energy_bought, 2)
            attrs[total_cost_key] = round(self._total_cost, 2)
        elif self.price_type == "sell" and self._energy_sold > 0:
            attrs[energy_sold_key] = round(self._energy_sold, 2)
            attrs[total_revenue_key] = round(self._total_revenue, 2)
            
        last_updated_key = "Last updated"
        now = dt_util.now()
        attrs[last_updated_key] = now.strftime("%Y-%m-%d %H:%M:%S")
        
        return attrs
        
    @callback
    def _handle_cost_update(self) -> None:
        if not self.cost_coordinator or not self.cost_coordinator.data:
            return
            
        period_data = self.cost_coordinator.data.get(self.period)
        if not period_data:
            return
            
        if self.price_type == "buy":
            total_cost = abs(period_data.get("total_cost", 0))
            energy_bought = period_data.get("fae_usage", 0)
            
            if energy_bought > 0:
                self._state = round(total_cost / energy_bought, 4)
                self._energy_bought = energy_bought
                self._total_cost = total_cost
            else:
                self._state = None
                
        elif self.price_type == "sell":
            total_revenue = period_data.get("total_sold", 0)
            energy_sold = period_data.get("rae_usage", 0)
            
            if energy_sold > 0:
                self._state = round(total_revenue / energy_sold, 4)
                self._energy_sold = energy_sold
                self._total_revenue = total_revenue
            else:
                self._state = None
                
        self.async_write_ha_state()
        
    @property
    def available(self) -> bool:
        return (self.cost_coordinator is not None and 
                self.cost_coordinator.last_update_success and
                self.cost_coordinator.data is not None)


class PstrykFinancialBalanceSensor(CoordinatorEntity, SensorEntity):
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY
    
    def __init__(self, coordinator: PstrykCostDataUpdateCoordinator, 
                 period: str, entry_id: str):
        super().__init__(coordinator)
        self.period = period
        self.entry_id = entry_id
        self.entity_id = f"sensor.{DOMAIN}_{period}_financial_balance"
        
    @property
    def name(self) -> str:
        period_name = self.period.title()
        balance_text = "Financial Balance"
        return f"Pstryk {period_name} {balance_text}"
        
    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_financial_balance_{self.period}"
        
    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "pstryk_energy")},
            "name": "Pstryk Energy",
            "manufacturer": "Pstryk",
            "model": "Energy Price Monitor",
            "sw_version": get_integration_version(self.hass),
        }
        
    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
            
        period_data = self.coordinator.data.get(self.period)
        if not period_data or "total_balance" not in period_data:
            return None
            
        balance = period_data.get("total_balance")
        
        return balance
        
    @property
    def native_unit_of_measurement(self) -> str:
        return "PLN"
        
    @property
    def icon(self) -> str:
        if self.native_value is None:
            return "mdi:currency-usd-off"
        elif self.native_value < 0:
            return "mdi:cash-minus"
        elif self.native_value > 0:
            return "mdi:cash-plus"
        else:
            return "mdi:cash"
            
    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data or not self.coordinator.data.get(self.period):
            return {}
            
        period_data = self.coordinator.data.get(self.period)
        frame = period_data.get("frame", {})
        
        buy_cost_key = "Buy cost"
        sell_revenue_key = "Sell revenue"
        period_key = "Period"
        net_balance_key = "Balance"
        distribution_cost_key = "Distribution cost"
        excise_key = "Excise"
        vat_key = "VAT"
        service_cost_key = "Service cost"
        energy_bought_key = "Energy bought"
        energy_sold_key = "Energy sold"
        attrs = {
            period_key: self.period,
            net_balance_key: period_data.get("total_balance", 0),
            buy_cost_key: period_data.get("total_cost", 0),
            sell_revenue_key: period_data.get("total_sold", 0),
            energy_bought_key: period_data.get("fae_usage", 0),
            energy_sold_key: period_data.get("rae_usage", 0),
        }
        
        if frame:
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
            
        last_updated_key = "Last updated"
        now = dt_util.now()
        attrs[last_updated_key] = now.strftime("%Y-%m-%d %H:%M:%S")
        
        return attrs
        
    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data is not None
