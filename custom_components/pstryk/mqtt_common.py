"""Common MQTT functionality for Pstryk Energy integration."""
import logging
import json
from datetime import timedelta
from homeassistant.util import dt as dt_util
from homeassistant.helpers.event import async_track_point_in_time

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def publish_mqtt_prices(hass, entry_id, buy_topic, sell_topic):
    """Publish prices to MQTT - common function used by all components."""
    # Get coordinators
    buy_coordinator = hass.data[DOMAIN].get(f"{entry_id}_buy")
    sell_coordinator = hass.data[DOMAIN].get(f"{entry_id}_sell")
    
    if not buy_coordinator or not sell_coordinator:
        _LOGGER.debug("Price coordinators not found for entry %s (this is normal during startup)", entry_id)
        return False
        
    if not buy_coordinator.data or not sell_coordinator.data:
        _LOGGER.debug("No price data available for entry %s (coordinators may still be loading)", entry_id)
        return False
    
    # Get or create MQTT publisher
    mqtt_publisher = hass.data[DOMAIN].get(f"{entry_id}_mqtt")
    if not mqtt_publisher:
        from .mqtt_publisher import PstrykMqttPublisher
        mqtt_publisher = PstrykMqttPublisher(hass, entry_id, buy_topic, sell_topic)
        await mqtt_publisher.async_initialize()
    
    # Format prices
    buy_prices = mqtt_publisher._format_prices_for_evcc(buy_coordinator.data, "buy")
    sell_prices = mqtt_publisher._format_prices_for_evcc(sell_coordinator.data, "sell")
    
    if not buy_prices or not sell_prices:
        _LOGGER.error("No valid prices to publish")
        return False
    
    # Sort prices
    buy_prices.sort(key=lambda x: x["start"])
    sell_prices.sort(key=lambda x: x["start"])
    
    # Create payloads
    buy_payload = json.dumps(buy_prices)
    sell_payload = json.dumps(sell_prices)
    
    # Publish with retain flag
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
    
    _LOGGER.info("Published prices to MQTT topics %s and %s", buy_topic, sell_topic)
    return True


async def setup_periodic_mqtt_publish(hass, entry_id, buy_topic, sell_topic, interval_minutes=60):
    """Set up periodic MQTT publishing with automatic republishing."""
    retain_key = f"{entry_id}_auto_retain"
    
    # Cancel any existing task
    if retain_key in hass.data[DOMAIN] and callable(hass.data[DOMAIN][retain_key]):
        hass.data[DOMAIN][retain_key]()
        hass.data[DOMAIN].pop(retain_key, None)
    
    # Function to republish data periodically
    async def republish_retain_periodic(now=None):
        """Republish MQTT message with retain flag periodically."""
        try:
            success = await publish_mqtt_prices(hass, entry_id, buy_topic, sell_topic)
            if success:
                _LOGGER.debug("Auto-republished retained messages")
            
            # Schedule next run
            next_run = dt_util.now() + timedelta(minutes=interval_minutes)
            hass.data[DOMAIN][retain_key] = async_track_point_in_time(
                hass, republish_retain_periodic, dt_util.as_utc(next_run)
            )
            
        except Exception as e:
            _LOGGER.error("Error in automatic MQTT retain process: %s", str(e))
            # Try to reschedule anyway
            next_run = dt_util.now() + timedelta(minutes=interval_minutes)
            hass.data[DOMAIN][retain_key] = async_track_point_in_time(
                hass, republish_retain_periodic, dt_util.as_utc(next_run)
            )
    
    # Publish immediately
    await publish_mqtt_prices(hass, entry_id, buy_topic, sell_topic)
    
    # Schedule first periodic run
    next_run = dt_util.now() + timedelta(minutes=interval_minutes)
    hass.data[DOMAIN][retain_key] = async_track_point_in_time(
        hass, republish_retain_periodic, dt_util.as_utc(next_run)
    )
    
    _LOGGER.info(
        "Automatic MQTT publishing activated (every %d minutes)", 
        interval_minutes
    )
