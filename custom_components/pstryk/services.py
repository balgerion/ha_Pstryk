"""Services for Pstryk Energy integration."""
import logging
import voluptuous as vol
import json
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.components import mqtt
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.event import async_track_point_in_time
from datetime import timedelta
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, 
    DEFAULT_MQTT_TOPIC_BUY, 
    DEFAULT_MQTT_TOPIC_SELL,
    CONF_MQTT_TOPIC_BUY,
    CONF_MQTT_TOPIC_SELL
)

_LOGGER = logging.getLogger(__name__)

SERVICE_PUBLISH_MQTT = "publish_to_evcc"
SERVICE_FORCE_RETAIN = "force_retain"

PUBLISH_MQTT_SCHEMA = vol.Schema({
    vol.Optional("entry_id"): cv.string,
    vol.Optional("topic_buy"): cv.string,
    vol.Optional("topic_sell"): cv.string,
})

FORCE_RETAIN_SCHEMA = vol.Schema({
    vol.Optional("entry_id"): cv.string,
    vol.Optional("topic_buy"): cv.string,
    vol.Optional("topic_sell"): cv.string,
    vol.Optional("retain_hours", default=168): vol.All(vol.Coerce(int), vol.Range(min=1, max=720)),
})

async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for Pstryk integration."""
    
    async def async_publish_mqtt_service(service_call: ServiceCall) -> None:
        """Handle the service call to publish to MQTT."""
        entry_id = service_call.data.get("entry_id")
        topic_buy_override = service_call.data.get("topic_buy")
        topic_sell_override = service_call.data.get("topic_sell")
        
        # Get translations for logs
        try:
            translations = await async_get_translations(
                hass, hass.config.language, DOMAIN, ["mqtt"]
            )
        except Exception as e:
            _LOGGER.warning("Failed to load translations for services: %s", e)
            translations = {}
            
        # Check if MQTT is available
        if not hass.services.has_service("mqtt", "publish"):
            mqtt_disabled_msg = translations.get(
                "mqtt.mqtt_disabled", 
                "MQTT integration is not enabled"
            )
            _LOGGER.error(mqtt_disabled_msg)
            return
            
        # Find config entries for Pstryk
        config_entries = hass.config_entries.async_entries(DOMAIN)
        if not config_entries:
            _LOGGER.error("No Pstryk Energy config entries found")
            return
            
        # If entry_id specified, filter to that entry
        if entry_id:
            config_entries = [entry for entry in config_entries if entry.entry_id == entry_id]
            if not config_entries:
                _LOGGER.error("Specified entry_id %s not found", entry_id)
                return
        
        # Publish for all entries or the specified one
        for entry in config_entries:
            key = f"{entry.entry_id}_mqtt"
            mqtt_publisher = hass.data[DOMAIN].get(key)
            
            # If publisher not found, create a temporary one
            if not mqtt_publisher:
                # Check if we have buy coordinator
                buy_coordinator = hass.data[DOMAIN].get(f"{entry.entry_id}_buy")
                sell_coordinator = hass.data[DOMAIN].get(f"{entry.entry_id}_sell")
                if not buy_coordinator or not sell_coordinator:
                    _LOGGER.error("Price coordinators not found for entry %s", entry.entry_id)
                    continue
                    
                # Dynamically import the publisher class
                from .mqtt_publisher import PstrykMqttPublisher
                
                # Get topic from options or use override/default
                mqtt_topic_buy = topic_buy_override
                mqtt_topic_sell = topic_sell_override
                if not mqtt_topic_buy:
                    mqtt_topic_buy = entry.options.get(CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY)
                if not mqtt_topic_sell:
                    mqtt_topic_sell = entry.options.get(CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL)
                    
                # Create temporary publisher
                mqtt_publisher = PstrykMqttPublisher(
                    hass, 
                    entry.entry_id, 
                    mqtt_topic_buy,
                    mqtt_topic_sell
                )
                await mqtt_publisher.async_initialize()
            else:
                # Use the override topics if provided
                if topic_buy_override:
                    mqtt_publisher.mqtt_topic_buy = topic_buy_override
                if topic_sell_override:
                    mqtt_publisher.mqtt_topic_sell = topic_sell_override
                
            # Publish prices
            success = await mqtt_publisher.publish_prices()
            
            if success:
                _LOGGER.info("Manual MQTT publish to EVCC completed for entry %s", entry.entry_id)
            else:
                _LOGGER.error("Failed to publish to MQTT for entry %s", entry.entry_id)
    
    async def async_force_retain_service(service_call: ServiceCall) -> None:
        """Handle the service call to force retain MQTT messages permanently."""
        entry_id = service_call.data.get("entry_id")
        topic_buy_override = service_call.data.get("topic_buy")
        topic_sell_override = service_call.data.get("topic_sell")
        retain_hours = service_call.data.get("retain_hours", 168)  # Default 7 days
        
        # Find config entries for Pstryk
        config_entries = hass.config_entries.async_entries(DOMAIN)
        if not config_entries:
            _LOGGER.error("No Pstryk Energy config entries found")
            return
            
        # If entry_id specified, filter to that entry
        if entry_id:
            config_entries = [entry for entry in config_entries if entry.entry_id == entry_id]
            if not config_entries:
                _LOGGER.error("Specified entry_id %s not found", entry_id)
                return
                
        # Process all entries or just the specified one
        for entry in config_entries:
            # Get MQTT publisher
            mqtt_publisher = hass.data[DOMAIN].get(f"{entry.entry_id}_mqtt")
            
            # Get buy and sell coordinators
            buy_coordinator = hass.data[DOMAIN].get(f"{entry.entry_id}_buy")
            sell_coordinator = hass.data[DOMAIN].get(f"{entry.entry_id}_sell")
            
            if not buy_coordinator or not buy_coordinator.data or not sell_coordinator or not sell_coordinator.data:
                _LOGGER.error("No data available for entry %s", entry.entry_id)
                continue
            
            # If no publisher, create a temporary one
            if not mqtt_publisher:
                # Import publisher class
                from .mqtt_publisher import PstrykMqttPublisher
                
                # Get topics from options or use overrides
                mqtt_topic_buy = topic_buy_override or entry.options.get(CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY)
                mqtt_topic_sell = topic_sell_override or entry.options.get(CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL)
                
                # Create publisher
                mqtt_publisher = PstrykMqttPublisher(
                    hass, 
                    entry.entry_id, 
                    mqtt_topic_buy,
                    mqtt_topic_sell
                )
                await mqtt_publisher.async_initialize()
            else:
                # Update topics if overrides provided
                if topic_buy_override:
                    mqtt_publisher.mqtt_topic_buy = topic_buy_override
                if topic_sell_override:
                    mqtt_publisher.mqtt_topic_sell = topic_sell_override
            
            # Format prices
            buy_prices = mqtt_publisher._format_prices_for_evcc(buy_coordinator.data, "buy")
            sell_prices = mqtt_publisher._format_prices_for_evcc(sell_coordinator.data, "sell")
            
            if not buy_prices or not sell_prices:
                _LOGGER.error("No price data available to publish")
                continue
                
            # Sort prices
            buy_prices.sort(key=lambda x: x["start"])
            sell_prices.sort(key=lambda x: x["start"])
            
            # Create payloads
            buy_payload = json.dumps(buy_prices)
            sell_payload = json.dumps(sell_prices)
            
            # Get topics
            buy_topic = mqtt_publisher.mqtt_topic_buy
            sell_topic = mqtt_publisher.mqtt_topic_sell
            
            _LOGGER.info(
                "Setting up scheduled retain for buy topic %s and sell topic %s for %d hours", 
                buy_topic,
                sell_topic,
                retain_hours
            )
            
            # First immediate publish
            await hass.services.async_call(
                "mqtt", 
                "publish",
                {
                    "topic": buy_topic,
                    "payload": buy_payload,
                    "qos": 1,
                    "retain": True
                },
                blocking=True
            )
            
            await hass.services.async_call(
                "mqtt", 
                "publish",
                {
                    "topic": sell_topic,
                    "payload": sell_payload,
                    "qos": 1,
                    "retain": True
                },
                blocking=True
            )
            
            # Schedule periodic re-publishing to ensure message stays retained
            now = dt_util.now()
            end_time = now + timedelta(hours=retain_hours)
            
            # Store the unsub function in hass.data
            retain_key = f"{entry.entry_id}_retain_unsub"
            if retain_key in hass.data[DOMAIN]:
                # Cancel previous retain schedule
                hass.data[DOMAIN][retain_key]()
                hass.data[DOMAIN].pop(retain_key, None)
            
            # Create a function that will republish and reschedule itself
            async def republish_retain(now=None):
                # Publish with retain
                await hass.services.async_call(
                    "mqtt", 
                    "publish",
                    {
                        "topic": buy_topic,
                        "payload": buy_payload,
                        "qos": 1,
                        "retain": True
                    },
                    blocking=True
                )
                
                await hass.services.async_call(
                    "mqtt", 
                    "publish",
                    {
                        "topic": sell_topic,
                        "payload": sell_payload,
                        "qos": 1,
                        "retain": True
                    },
                    blocking=True
                )
                
                current_time = dt_util.now()
                _LOGGER.debug(
                    "Re-published retained messages (will continue until %s)",
                    end_time.isoformat()
                )
                
                # If we've reached the end time, stop
                if current_time >= end_time:
                    _LOGGER.info(
                        "Finished scheduled retain after %d hours",
                        retain_hours
                    )
                    if retain_key in hass.data[DOMAIN]:
                        hass.data[DOMAIN].pop(retain_key, None)
                    return
                
                # Otherwise, schedule the next run in 1 hour
                next_run = current_time + timedelta(hours=1)
                hass.data[DOMAIN][retain_key] = async_track_point_in_time(
                    hass, republish_retain, dt_util.as_utc(next_run)
                )
            
            # Schedule first run in 1 hour
            next_run = now + timedelta(hours=1)
            hass.data[DOMAIN][retain_key] = async_track_point_in_time(
                hass, republish_retain, dt_util.as_utc(next_run)
            )
            
            _LOGGER.info(
                "Forced retain activated with hourly re-publishing for %d hours",
                retain_hours
            )
    
    # Register the services
    hass.services.async_register(
        DOMAIN, 
        SERVICE_PUBLISH_MQTT, 
        async_publish_mqtt_service,
        schema=PUBLISH_MQTT_SCHEMA
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_RETAIN,
        async_force_retain_service,
        schema=FORCE_RETAIN_SCHEMA
    )

async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload Pstryk services."""
    if hass.services.has_service(DOMAIN, SERVICE_PUBLISH_MQTT):
        hass.services.async_remove(DOMAIN, SERVICE_PUBLISH_MQTT)
        
    if hass.services.has_service(DOMAIN, SERVICE_FORCE_RETAIN):
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_RETAIN)
