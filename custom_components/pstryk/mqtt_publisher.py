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
from .update_coordinator import is_likely_placeholder_data

_LOGGER = logging.getLogger(__name__)

class PstrykMqttPublisher:

    def __init__(
        self, 
        hass: HomeAssistant, 
        entry_id: str, 
        mqtt_topic_buy: str = DEFAULT_MQTT_TOPIC_BUY,
        mqtt_topic_sell: str = DEFAULT_MQTT_TOPIC_SELL
    ):
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
        if self._initialized:
            return True
            
        try:
            self._translations = await async_get_translations(
                self.hass, self.hass.config.language, DOMAIN, ["mqtt"]
            )
        except Exception as ex:
            _LOGGER.warning("Failed to load translations for MQTT publisher: %s", ex)
            
        self._initialized = True
        return True
    
    def _format_prices_for_evcc(self, prices_data, price_type):
        if not prices_data or "prices" not in prices_data:
            return []
            
        formatted_prices = []
        
        mqtt_48h_mode = self.hass.data[DOMAIN].get(f"{self.entry_id}_mqtt_48h_mode", False)
        
        now_local = dt_util.as_local(dt_util.utcnow())
        today_date = now_local.date()
        tomorrow_date = (now_local + timedelta(days=1)).date()
        
        all_prices = prices_data.get("prices", [])
        
        prices_by_date = {}
        for price_entry in all_prices:
            try:
                if "start" not in price_entry or "price" not in price_entry:
                    continue
                    
                price_datetime = dt_util.parse_datetime(price_entry["start"])
                if not price_datetime:
                    continue
                    
                price_datetime_local = dt_util.as_local(price_datetime)
                price_date = price_datetime_local.date()
                
                if price_date not in prices_by_date:
                    prices_by_date[price_date] = []
                    
                prices_by_date[price_date].append(price_entry)
                
            except Exception as e:
                _LOGGER.error("Error processing price entry: %s", str(e))
        
        days_to_include = []
        
        if mqtt_48h_mode:
            if today_date in prices_by_date:
                days_to_include.append(today_date)
            
            if tomorrow_date in prices_by_date:
                tomorrow_prices = prices_by_date[tomorrow_date]
                if not is_likely_placeholder_data(tomorrow_prices):
                    days_to_include.append(tomorrow_date)
                    _LOGGER.info(f"Including {len(tomorrow_prices)} tomorrow prices in MQTT publish")
                else:
                    _LOGGER.info(f"Excluding tomorrow prices from MQTT - appear to be placeholders or incomplete")
        else:
            if today_date in prices_by_date:
                days_to_include.append(today_date)
        
        for date_to_include in sorted(days_to_include):
            day_prices = prices_by_date.get(date_to_include, [])
            
            day_prices.sort(key=lambda x: x["start"])
            
            for price_entry in day_prices:
                try:
                    try:
                        price_value = float(price_entry["price"])
                    except (TypeError, ValueError):
                        _LOGGER.warning("Invalid price value: %s", price_entry.get("price"))
                        continue
                        
                    local_dt = dt_util.parse_datetime(price_entry["start"])
                    if not local_dt:
                        continue
                        
                    local_dt = dt_util.as_local(local_dt)
                    utc_dt = dt_util.as_utc(local_dt)
                    
                    end_dt = utc_dt + timedelta(hours=1)
                    
                    start_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    
                    formatted_prices.append({
                        "start": start_str,
                        "end": end_str,
                        "value": price_value
                    })
                except Exception as e:
                    _LOGGER.error("Error formatting price for EVCC: %s", str(e))
        
        if formatted_prices:
            first_time = formatted_prices[0]["start"]
            last_time = formatted_prices[-1]["start"]
            _LOGGER.debug(f"Formatted {len(formatted_prices)} prices for MQTT from {first_time} to {last_time}")
            
            hours_by_date = {}
            for fp in formatted_prices:
                date_part = fp["start"][:10]
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
        from .mqtt_common import setup_periodic_mqtt_publish
        
        await setup_periodic_mqtt_publish(
            self.hass,
            self.entry_id,
            self.mqtt_topic_buy,
            self.mqtt_topic_sell,
            interval_minutes
        )
        
        return True

    def unsubscribe(self):
        _LOGGER.debug("MQTT publisher cleanup requested")
            
    @property
    def last_published(self):
        return self._last_published
