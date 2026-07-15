"""Pstryk Energy integration."""
import logging
import asyncio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components import persistent_notification
from homeassistant.helpers.translation import async_get_translations

from .mqtt_publisher import PstrykMqttPublisher
from .mqtt_common import setup_periodic_mqtt_publish
from .services import async_setup_services, async_unload_services
from .const import (
    DOMAIN,
    CONF_MQTT_ENABLED,
    CONF_MQTT_TOPIC_BUY,
    CONF_MQTT_TOPIC_SELL,
    CONF_MQTT_48H_MODE,
    DEFAULT_MQTT_TOPIC_BUY,
    DEFAULT_MQTT_TOPIC_SELL
)

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up hass.data structure and services."""
    hass.data.setdefault(DOMAIN, {})
    
    await async_setup_services(hass)
    
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Store API key and forward to sensor platform."""
    hass.data[DOMAIN].setdefault(entry.entry_id, {})["api_key"] = entry.data.get("api_key")
    
    if not entry.update_listeners:
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    _LOGGER.debug("Pstryk entry setup: %s", entry.entry_id)
    
    mqtt_enabled = entry.options.get(CONF_MQTT_ENABLED, False)
    mqtt_topic_buy = entry.options.get(CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY)
    mqtt_topic_sell = entry.options.get(CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL)
    mqtt_48h_mode = entry.options.get(CONF_MQTT_48H_MODE, False)
    
    hass.data[DOMAIN][f"{entry.entry_id}_mqtt_48h_mode"] = mqtt_48h_mode
    
    if mqtt_enabled:
        if not hass.services.has_service("mqtt", "publish"):
            _LOGGER.error("MQTT integration is not enabled. Cannot setup EVCC bridge.")
            try:
                translations = await async_get_translations(
                    hass, hass.config.language, DOMAIN, ["mqtt"]
                )
            except Exception as ex:
                _LOGGER.warning("Failed to load translations for MQTT notification: %s", ex)
                translations = {}
            persistent_notification.async_create(
                hass,
                translations.get(
                    f"component.{DOMAIN}.mqtt.mqtt_not_configured_message",
                    "MQTT integration is not enabled. EVCC MQTT Bridge for Pstryk Energy "
                    "cannot function. Please configure MQTT integration in Home Assistant."
                ),
                title=translations.get(
                    f"component.{DOMAIN}.mqtt.mqtt_not_configured_title",
                    "Pstryk Energy MQTT Error"
                ),
                notification_id=f"{DOMAIN}_mqtt_error_{entry.entry_id}"
            )
            return True
            
        mqtt_publisher = PstrykMqttPublisher(
            hass, 
            entry.entry_id, 
            mqtt_topic_buy,
            mqtt_topic_sell
        )
        hass.data[DOMAIN][f"{entry.entry_id}_mqtt"] = mqtt_publisher
        
        async def start_mqtt_publisher():
            """Start the MQTT publisher after a short delay to ensure coordinators are ready."""
            await mqtt_publisher.async_initialize()
            
            max_wait = 60
            wait_interval = 2
            waited = 0
            
            while waited < max_wait:
                buy_coordinator = hass.data[DOMAIN].get(f"{entry.entry_id}_buy")
                sell_coordinator = hass.data[DOMAIN].get(f"{entry.entry_id}_sell")
                
                if buy_coordinator and sell_coordinator:
                    _LOGGER.debug("Coordinators ready after %d seconds, starting MQTT periodic publishing", waited)
                    break
                    
                await asyncio.sleep(wait_interval)
                waited += wait_interval
            else:
                _LOGGER.warning("Coordinators not ready after %d seconds, MQTT publishing may fail", max_wait)
            
            await setup_periodic_mqtt_publish(
                hass, 
                entry.entry_id, 
                mqtt_topic_buy, 
                mqtt_topic_sell,
                interval_minutes=60
            )
        
        async def delayed_start():
            await asyncio.sleep(5)
            await start_mqtt_publisher()
            
        hass.async_create_task(delayed_start())
        
        _LOGGER.info("EVCC MQTT Bridge enabled for Pstryk Energy (48h mode: %s), publishing to %s and %s", 
                    mqtt_48h_mode, mqtt_topic_buy, mqtt_topic_sell)
        
    return True

async def _cleanup_coordinators(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up coordinators and cancel scheduled tasks."""
    retain_key = f"{entry.entry_id}_auto_retain"
    if retain_key in hass.data[DOMAIN] and callable(hass.data[DOMAIN][retain_key]):
        hass.data[DOMAIN][retain_key]()
        hass.data[DOMAIN].pop(retain_key, None)
    
    mqtt_publisher = hass.data[DOMAIN].get(f"{entry.entry_id}_mqtt")
    if mqtt_publisher:
        _LOGGER.debug("Cleaning up MQTT publisher for entry %s", entry.entry_id)
        mqtt_publisher.unsubscribe()
        hass.data[DOMAIN].pop(f"{entry.entry_id}_mqtt", None)
    
    for price_type in ("buy", "sell"):
        key = f"{entry.entry_id}_{price_type}"
        coordinator = hass.data[DOMAIN].get(key)
        if coordinator:
            _LOGGER.debug("Cleaning up %s coordinator for entry %s", price_type, entry.entry_id)
            if hasattr(coordinator, '_unsub_hourly') and coordinator._unsub_hourly:
                coordinator._unsub_hourly()
                coordinator._unsub_hourly = None
            if hasattr(coordinator, '_unsub_midnight') and coordinator._unsub_midnight:
                coordinator._unsub_midnight()
                coordinator._unsub_midnight = None
            if hasattr(coordinator, '_unsub_afternoon') and coordinator._unsub_afternoon:
                coordinator._unsub_afternoon()
                coordinator._unsub_afternoon = None
            hass.data[DOMAIN].pop(key, None)
    
    cost_key = f"{entry.entry_id}_cost"
    cost_coordinator = hass.data[DOMAIN].get(cost_key)
    if cost_coordinator:
        _LOGGER.debug("Cleaning up cost coordinator for entry %s", entry.entry_id)
        if hasattr(cost_coordinator, '_unsub_hourly') and cost_coordinator._unsub_hourly:
            cost_coordinator._unsub_hourly()
            cost_coordinator._unsub_hourly = None
        if hasattr(cost_coordinator, '_unsub_midnight') and cost_coordinator._unsub_midnight:
            cost_coordinator._unsub_midnight()
            cost_coordinator._unsub_midnight = None
        hass.data[DOMAIN].pop(cost_key, None)
    
    hass.data[DOMAIN].pop(f"{entry.entry_id}_mqtt_48h_mode", None)

    api_client_key = f"{entry.entry_id}_api_client"
    if api_client_key in hass.data[DOMAIN]:
        _LOGGER.debug("Cleaning up API client for entry %s", entry.entry_id)
        hass.data[DOMAIN].pop(api_client_key, None)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload sensor platform and clear data."""
    await _cleanup_coordinators(hass, entry)
    
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, "sensor")
    
    if unload_ok:
        if entry.entry_id in hass.data[DOMAIN]:
            hass.data[DOMAIN].pop(entry.entry_id)
            
        for key in list(hass.data[DOMAIN].keys()):
            if key.startswith(f"{entry.entry_id}_"):
                hass.data[DOMAIN].pop(key, None)
                
        entries = hass.config_entries.async_entries(DOMAIN)
        if len(entries) <= 1:
            await async_unload_services(hass)
                
    return unload_ok

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
