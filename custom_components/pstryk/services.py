import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.components import mqtt
from homeassistant.helpers.event import async_track_point_in_time
from datetime import timedelta
from homeassistant.util import dt as dt_util

from .mqtt_common import publish_mqtt_prices, setup_periodic_mqtt_publish
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
    
    async def async_publish_mqtt_service(service_call: ServiceCall) -> None:
        entry_id = service_call.data.get("entry_id")
        topic_buy_override = service_call.data.get("topic_buy")
        topic_sell_override = service_call.data.get("topic_sell")
        
        if not hass.services.has_service("mqtt", "publish"):
            _LOGGER.error("MQTT integration is not enabled")
            return
            
        config_entries = hass.config_entries.async_entries(DOMAIN)
        if not config_entries:
            _LOGGER.error("No Pstryk Energy config entries found")
            return
            
        if entry_id:
            config_entries = [entry for entry in config_entries if entry.entry_id == entry_id]
            if not config_entries:
                _LOGGER.error("Specified entry_id %s not found", entry_id)
                return
        
        for entry in config_entries:
            mqtt_topic_buy = topic_buy_override or entry.options.get(CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY)
            mqtt_topic_sell = topic_sell_override or entry.options.get(CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL)
            
            success = await publish_mqtt_prices(hass, entry.entry_id, mqtt_topic_buy, mqtt_topic_sell)
            
            if success:
                _LOGGER.info("Manual MQTT publish to EVCC completed for entry %s", entry.entry_id)
            else:
                _LOGGER.error("Failed to publish to MQTT for entry %s", entry.entry_id)
    
    async def async_force_retain_service(service_call: ServiceCall) -> None:
        entry_id = service_call.data.get("entry_id")
        topic_buy_override = service_call.data.get("topic_buy")
        topic_sell_override = service_call.data.get("topic_sell")
        retain_hours = service_call.data.get("retain_hours", 168)
        
        config_entries = hass.config_entries.async_entries(DOMAIN)
        if not config_entries:
            _LOGGER.error("No Pstryk Energy config entries found")
            return
            
        if entry_id:
            config_entries = [entry for entry in config_entries if entry.entry_id == entry_id]
            if not config_entries:
                _LOGGER.error("Specified entry_id %s not found", entry_id)
                return
                
        def make_republisher(entry_id, topic_buy, topic_sell, retain_key, end_time):
            async def republish_retain(now=None):
                success = await publish_mqtt_prices(hass, entry_id, topic_buy, topic_sell)

                if success:
                    current_time = dt_util.now()
                    _LOGGER.debug(
                        "Re-published retained messages (will continue until %s)",
                        end_time.strftime("%Y-%m-%d %H:%M:%S")
                    )

                    if current_time >= end_time:
                        _LOGGER.info(
                            "Finished scheduled retain after %d hours",
                            retain_hours
                        )
                        if retain_key in hass.data[DOMAIN]:
                            hass.data[DOMAIN].pop(retain_key, None)
                        return
                    
                    next_run = current_time + timedelta(hours=1)
                    hass.data[DOMAIN][retain_key] = async_track_point_in_time(
                        hass, republish_retain, dt_util.as_utc(next_run)
                    )

            return republish_retain

        for entry in config_entries:
            mqtt_topic_buy = topic_buy_override or entry.options.get(CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY)
            mqtt_topic_sell = topic_sell_override or entry.options.get(CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL)

            success = await publish_mqtt_prices(hass, entry.entry_id, mqtt_topic_buy, mqtt_topic_sell)

            if not success:
                _LOGGER.error("Failed to publish initial retained messages for entry %s", entry.entry_id)
                continue

            _LOGGER.info(
                "Setting up scheduled retain for topics %s and %s for %d hours",
                mqtt_topic_buy,
                mqtt_topic_sell,
                retain_hours
            )

            now = dt_util.now()
            end_time = now + timedelta(hours=retain_hours)

            retain_key = f"{entry.entry_id}_retain_unsub"
            if retain_key in hass.data[DOMAIN]:
                hass.data[DOMAIN][retain_key]()
                hass.data[DOMAIN].pop(retain_key, None)

            republish_retain = make_republisher(
                entry.entry_id, mqtt_topic_buy, mqtt_topic_sell, retain_key, end_time
            )

            next_run = now + timedelta(hours=1)
            hass.data[DOMAIN][retain_key] = async_track_point_in_time(
                hass, republish_retain, dt_util.as_utc(next_run)
            )

            _LOGGER.info(
                "Forced retain activated with hourly re-publishing for %d hours",
                retain_hours
            )
    
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
    if hass.services.has_service(DOMAIN, SERVICE_PUBLISH_MQTT):
        hass.services.async_remove(DOMAIN, SERVICE_PUBLISH_MQTT)
        
    if hass.services.has_service(DOMAIN, SERVICE_FORCE_RETAIN):
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_RETAIN)
