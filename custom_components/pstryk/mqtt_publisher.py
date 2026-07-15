"""MQTT Publisher for Pstryk Energy integration."""
import logging
import json
from datetime import timedelta
import asyncio
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.components import mqtt
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    DEFAULT_MQTT_TOPIC_BUY,
    DEFAULT_MQTT_TOPIC_SELL
)

_LOGGER = logging.getLogger(__name__)

class PstrykMqttPublisher:
    """Class to handle publishing Pstryk energy prices to MQTT for EVCC."""

    def __init__(
        self, 
        hass: HomeAssistant, 
        entry_id: str, 
        mqtt_topic_buy: str = DEFAULT_MQTT_TOPIC_BUY,
        mqtt_topic_sell: str = DEFAULT_MQTT_TOPIC_SELL
    ):
        """Initialize the MQTT publisher."""
        self.hass = hass
        self.entry_id = entry_id
        self.mqtt_topic_buy = mqtt_topic_buy
        self.mqtt_topic_sell = mqtt_topic_sell
        self._publish_task = None
        self._initialized = False
        self._translations = {}
        self._unsub_timer = None
        self._last_published = None

    async def async_initialize(self):
        """Initialize the publisher and load translations."""
        if self._initialized:
            return True
            
        # Load translations
        try:
            self._translations = await async_get_translations(
                self.hass, self.hass.config.language, DOMAIN, ["mqtt"]
            )
        except Exception as ex:
            _LOGGER.warning("Failed to load translations for MQTT publisher: %s", ex)
            
        self._initialized = True
        return True
    
    def _is_day_data_valid(self, day_prices):
        """Check if prices for a specific day look like real data.
        
        Returns False if the data appears to be placeholders.
        """
        if not day_prices:
            return False
            
        price_values = [p.get("price") for p in day_prices if p.get("price") is not None]
        
        if not price_values:
            return False
            
        # Need at least 20 hours for a valid day
        if len(price_values) < 20:
            return False
            
        # If all values are identical, it's likely placeholder data
        unique_values = set(price_values)
        if len(unique_values) == 1:
            return False
            
        # If more than 90% of values are the same, probably placeholders
        most_common = max(set(price_values), key=price_values.count)
        if price_values.count(most_common) / len(price_values) > 0.9:
            return False
            
        return True

    def _format_prices_for_evcc(self, prices_data, price_type):
        """Format prices in EVCC expected format.
        
        EVCC expects a list of objects with the following structure:
        [
            {
                "start": "2024-05-07T00:00:00Z", // ISO timestamp in UTC
                "end": "2024-05-07T01:00:00Z",   // ISO timestamp in UTC (next hour)
                "value": 0.1234                  // Price in PLN/kWh
            }
        ]
        """
        if not prices_data or "prices" not in prices_data:
            return []
            
        formatted_prices = []
        
        # First, check if we're in 48h mode
        mqtt_48h_mode = self.hass.data[DOMAIN].get(f"{self.entry_id}_mqtt_48h_mode", False)
        
        # Get current date for comparison - in local time
        now_local = dt_util.as_local(dt_util.utcnow())
        today_date = now_local.date()
        tomorrow_date = (now_local + timedelta(days=1)).date()
        
        # Get all prices from coordinator
        all_prices = prices_data.get("prices", [])
        
        # Process prices and group by date
        prices_by_date = {}
        for price_entry in all_prices:
            try:
                if "start" not in price_entry or "price" not in price_entry:
                    continue
                    
                # Parse the datetime (it's already in local time from coordinator)
                price_datetime = dt_util.parse_datetime(price_entry["start"])
                if not price_datetime:
                    continue
                    
                # Ensure it's local time
                price_datetime_local = dt_util.as_local(price_datetime)
                price_date = price_datetime_local.date()
                
                if price_date not in prices_by_date:
                    prices_by_date[price_date] = []
                    
                prices_by_date[price_date].append(price_entry)
                
            except Exception as e:
                _LOGGER.error("Error processing price entry: %s", str(e))
        
        # Determine which days to include
        days_to_include = []
        
        if mqtt_48h_mode:
            # In 48h mode, include today and tomorrow
            if today_date in prices_by_date:
                days_to_include.append(today_date)
            
            if tomorrow_date in prices_by_date:
                # Check if tomorrow's data is valid
                tomorrow_prices = prices_by_date[tomorrow_date]
                if self._is_day_data_valid(tomorrow_prices):
                    days_to_include.append(tomorrow_date)
                    _LOGGER.info(f"Including {len(tomorrow_prices)} tomorrow prices in MQTT publish")
                else:
                    _LOGGER.info(f"Excluding tomorrow prices from MQTT - appear to be placeholders or incomplete")
        else:
            # In 24h mode, only include today
            if today_date in prices_by_date:
                days_to_include.append(today_date)
        
        # Process selected days
        for date_to_include in sorted(days_to_include):
            day_prices = prices_by_date.get(date_to_include, [])
            
            # Sort by time to ensure correct order
            day_prices.sort(key=lambda x: x["start"])
            
            for price_entry in day_prices:
                try:
                    # Validate price is a number
                    try:
                        price_value = float(price_entry["price"])
                    except (TypeError, ValueError):
                        _LOGGER.warning("Invalid price value: %s", price_entry.get("price"))
                        continue
                        
                    # Parse local time and convert to UTC ISO format
                    local_dt = dt_util.parse_datetime(price_entry["start"])
                    if not local_dt:
                        continue
                        
                    # Ensure it's local time and convert to UTC
                    local_dt = dt_util.as_local(local_dt)
                    utc_dt = dt_util.as_utc(local_dt)
                    
                    # Calculate end time (1 hour later)
                    end_dt = utc_dt + timedelta(hours=1)
                    
                    # Format times in ISO format with Z suffix for UTC
                    start_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    
                    formatted_prices.append({
                        "start": start_str,
                        "end": end_str,
                        "value": price_value
                    })
                except Exception as e:
                    _LOGGER.error("Error formatting price for EVCC: %s", str(e))
        
        # Log summary
        if formatted_prices:
            first_time = formatted_prices[0]["start"]
            last_time = formatted_prices[-1]["start"]
            _LOGGER.debug(f"Formatted {len(formatted_prices)} prices for MQTT from {first_time} to {last_time}")
            
            # Verify we have complete days
            hours_by_date = {}
            for fp in formatted_prices:
                date_part = fp["start"][:10]  # YYYY-MM-DD
                if date_part not in hours_by_date:
                    hours_by_date[date_part] = 0
                hours_by_date[date_part] += 1
                
            for date, hours in hours_by_date.items():
                if hours != 24:
                    _LOGGER.warning(f"Incomplete day {date}: only {hours} hours instead of 24")
        else:
            _LOGGER.warning("No prices formatted for MQTT")
                    
        return formatted_prices

    async def publish_prices(self):
        """Publish prices to MQTT using common function."""
        from .mqtt_common import publish_mqtt_prices
        
        success = await publish_mqtt_prices(
            self.hass, 
            self.entry_id, 
            self.mqtt_topic_buy, 
            self.mqtt_topic_sell
        )
        
        if success:
            self._last_published = dt_util.now()
            
        return success

    async def schedule_periodic_updates(self, interval_minutes=60):
        """Schedule periodic updates to MQTT (default: 60 minutes)."""
        from .mqtt_common import setup_periodic_mqtt_publish
        
        # Use common function for periodic publishing
        await setup_periodic_mqtt_publish(
            self.hass,
            self.entry_id,
            self.mqtt_topic_buy,
            self.mqtt_topic_sell,
            interval_minutes
        )
        
        return True

    def unsubscribe(self):
        """Unsubscribe from all events."""
        # Cleanup is handled by common function in mqtt_common.py
        _LOGGER.debug("MQTT publisher cleanup requested")
            
    @property
    def last_published(self):
        """Return the timestamp of the last successful publish."""
        return self._last_published
