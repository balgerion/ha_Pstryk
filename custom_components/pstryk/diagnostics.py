"""Diagnostics support for Pstryk Energy."""
from __future__ import annotations

import logging
from typing import Any
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, 
    CONF_MQTT_ENABLED, 
    CONF_MQTT_48H_MODE, 
    CONF_MQTT_TOPIC_BUY, 
    CONF_MQTT_TOPIC_SELL,
    CONF_RETRY_ATTEMPTS,
    CONF_RETRY_DELAY,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY
)

_LOGGER = logging.getLogger(__name__)

async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    diagnostics_data = {
        "entry": {
            "title": entry.title,
            "entry_id": entry.entry_id,
            "version": entry.version,
            "options": {
                "buy_top": entry.options.get("buy_top", entry.data.get("buy_top", 5)),
                "sell_top": entry.options.get("sell_top", entry.data.get("sell_top", 5)),
                "buy_worst": entry.options.get("buy_worst", entry.data.get("buy_worst", 5)),
                "sell_worst": entry.options.get("sell_worst", entry.data.get("sell_worst", 5)),
                "mqtt_enabled": entry.options.get(CONF_MQTT_ENABLED, False),
                "mqtt_48h_mode": entry.options.get(CONF_MQTT_48H_MODE, False),
                "mqtt_topic_buy": entry.options.get(CONF_MQTT_TOPIC_BUY, "Not set"),
                "mqtt_topic_sell": entry.options.get(CONF_MQTT_TOPIC_SELL, "Not set"),
                "retry_attempts": entry.options.get(CONF_RETRY_ATTEMPTS, DEFAULT_RETRY_ATTEMPTS),
                "retry_delay": entry.options.get(CONF_RETRY_DELAY, DEFAULT_RETRY_DELAY),
            },
        },
        "coordinators": {},
        "mqtt_status": {},
        "utility_meters": {},
    }

    # Check MQTT status
    mqtt_publisher = hass.data[DOMAIN].get(f"{entry.entry_id}_mqtt")
    if mqtt_publisher:
        diagnostics_data["mqtt_status"] = {
            "publisher_initialized": mqtt_publisher._initialized,
            "last_published": mqtt_publisher.last_published.strftime("%Y-%m-%d %H:%M:%S") if mqtt_publisher.last_published else None,
            "topic_buy": mqtt_publisher.mqtt_topic_buy,
            "topic_sell": mqtt_publisher.mqtt_topic_sell,
        }
    else:
        diagnostics_data["mqtt_status"]["enabled"] = False

    # Check coordinators
    now = dt_util.now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    
    for price_type in ("buy", "sell"):
        key = f"{entry.entry_id}_{price_type}"
        coordinator = hass.data[DOMAIN].get(key)
        if coordinator:
            coordinator_data = {
                "last_update_success": coordinator.last_update_success,
                "data_available": coordinator.data is not None,
                "mqtt_48h_mode": getattr(coordinator, 'mqtt_48h_mode', False),
            }
            
            # Check for retry configuration
            if hasattr(coordinator, 'retry_mechanism'):
                coordinator_data["retry_config"] = {
                    "max_retries": coordinator.retry_mechanism.max_retries,
                    "base_delay": coordinator.retry_mechanism.base_delay,
                }
            
            # Check for various update attributes
            if hasattr(coordinator, 'last_update') and coordinator.last_update:
                coordinator_data["last_update"] = dt_util.as_local(coordinator.last_update).strftime("%Y-%m-%d %H:%M:%S")
            elif hasattr(coordinator, 'last_updated') and coordinator.last_updated:
                coordinator_data["last_update"] = dt_util.as_local(coordinator.last_updated).strftime("%Y-%m-%d %H:%M:%S")
            else:
                coordinator_data["last_update"] = None
                
            # Add price count if data available
            if coordinator.data:
                coordinator_data["prices_count"] = len(coordinator.data.get("prices", []))
                coordinator_data["prices_today_count"] = len(coordinator.data.get("prices_today", []))
                coordinator_data["is_cached"] = coordinator.data.get("is_cached", False)
                
                # Check tomorrow prices availability
                all_prices = coordinator.data.get("prices", [])
                tomorrow_prices = [p for p in all_prices if p.get("start", "").startswith(tomorrow)]
                coordinator_data["tomorrow_prices_count"] = len(tomorrow_prices)
                coordinator_data["tomorrow_prices_available"] = len(tomorrow_prices) >= 20
                
                # MQTT price count - what would actually be published
                if coordinator.mqtt_48h_mode:
                    coordinator_data["mqtt_price_count"] = len(all_prices)
                else:
                    coordinator_data["mqtt_price_count"] = len(coordinator.data.get("prices_today", []))
                
            diagnostics_data["coordinators"][price_type] = coordinator_data

    # Check if MQTT integration is available
    diagnostics_data["mqtt_integration_available"] = hass.services.has_service("mqtt", "publish")
    
    # Check average price sensors
    for price_type in ("buy", "sell"):
        for period in ("monthly", "yearly"):
            entity_id = f"sensor.pstryk_{price_type}_{period}_average"
            state = hass.states.get(entity_id)
            
            if state:
                meter_data = {
                    "state": state.state,
                    "last_reset": state.attributes.get("Last reset", "Unknown"),
                    "price_count": state.attributes.get("Price count", 0),
                    "price_sum": state.attributes.get("Price sum", 0),
                    "available": state.state not in ["unknown", "unavailable"],
                }
                diagnostics_data["utility_meters"][f"{price_type}_{period}_average"] = meter_data
    
    # Check financial balance sensors
    for period in ("daily", "monthly", "yearly"):
        entity_id = f"sensor.pstryk_{period}_financial_balance"
        state = hass.states.get(entity_id)
        
        if state:
            balance_data = {
                "state": state.state,
                "buy_cost": state.attributes.get("Buy cost", 0),
                "sell_revenue": state.attributes.get("Sell revenue", 0),
                "balance": state.attributes.get("Balance", 0),
                "buy_cost": state.attributes.get("Buy cost", 0),
                "distribution_cost": state.attributes.get("Distribution cost", 0),
                "excise": state.attributes.get("Excise", 0),
                "vat": state.attributes.get("VAT", 0),
                "is_live": state.attributes.get("is_live", False),
                "available": state.state not in ["unknown", "unavailable"],
            }
            diagnostics_data["utility_meters"][f"financial_balance_{period}"] = balance_data
            
    # Check cost coordinator
    cost_coordinator = hass.data[DOMAIN].get(f"{entry.entry_id}_cost")
    if cost_coordinator and cost_coordinator.data:
        diagnostics_data["cost_coordinator"] = {
            "last_update_success": cost_coordinator.last_update_success,
            "data_available": cost_coordinator.data is not None,
            "daily_balance": cost_coordinator.data.get("daily", {}).get("total_balance"),
            "monthly_balance": cost_coordinator.data.get("monthly", {}).get("total_balance"),
            "yearly_balance": cost_coordinator.data.get("yearly", {}).get("total_balance"),
        }
        
        # Check for retry configuration
        if hasattr(cost_coordinator, 'retry_mechanism'):
            diagnostics_data["cost_coordinator"]["retry_config"] = {
                "max_retries": cost_coordinator.retry_mechanism.max_retries,
                "base_delay": cost_coordinator.retry_mechanism.base_delay,
            }

    return diagnostics_data
