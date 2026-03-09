"""Config flow for Pstryk Energy integration - Enhanced version."""
from homeassistant import config_entries
import voluptuous as vol
import asyncio
from datetime import timedelta
from homeassistant.util import dt as dt_util
from homeassistant.core import callback
from homeassistant.components import mqtt
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import (
    DOMAIN,
    API_URL,
    API_TIMEOUT,
    PRICING_ENDPOINT,
    DEFAULT_MQTT_TOPIC_BUY,
    DEFAULT_MQTT_TOPIC_SELL,
    CONF_MQTT_ENABLED,
    CONF_MQTT_TOPIC_BUY,
    CONF_MQTT_TOPIC_SELL,
    CONF_MQTT_48H_MODE,
    CONF_RETRY_ATTEMPTS,
    CONF_RETRY_DELAY,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_DELAY,
    MIN_RETRY_ATTEMPTS,
    MAX_RETRY_ATTEMPTS,
    MIN_RETRY_DELAY,
    MAX_RETRY_DELAY
)

class MQTTNotConfiguredError(HomeAssistantError):
    """Exception raised when MQTT is not configured."""
    pass

class PstrykConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pstryk Energy."""
    VERSION = 3

    def __init__(self):
        """Initialize the config flow."""
        self._data = {}
        self._options = {}

    async def async_step_user(self, user_input=None):
        """Handle the initial step - API configuration."""
        errors = {}
        if user_input is not None:
            # Validate API key
            api_key = user_input["api_key"]
            valid = await self._validate_api_key(api_key)
            
            if valid:
                self._data["api_key"] = api_key
                # Move to price settings step
                return await self.async_step_price_settings()
            else:
                errors["api_key"] = "invalid_api_key"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("api_key"): str,
            }),
            errors=errors
        )

    async def async_step_price_settings(self, user_input=None):
        """Handle price monitoring settings."""
        if user_input is not None:
            # Store price settings
            self._data.update({
                "buy_top": user_input["buy_top"],
                "sell_top": user_input["sell_top"],
                "buy_worst": user_input["buy_worst"],
                "sell_worst": user_input["sell_worst"],
            })
            
            # Check if MQTT is available
            mqtt_available = await self._check_mqtt_availability()
            if mqtt_available:
                return await self.async_step_mqtt_settings()
            else:
                # Skip MQTT and go to API retry settings
                return await self.async_step_api_retry()

        return self.async_show_form(
            step_id="price_settings",
            data_schema=vol.Schema({
                vol.Required("buy_top", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
                vol.Required("sell_top", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
                vol.Required("buy_worst", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
                vol.Required("sell_worst", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
            })
        )

    async def async_step_mqtt_settings(self, user_input=None):
        """Handle MQTT configuration."""
        errors = {}
        
        if user_input is not None:
            # Check MQTT configuration if enabled
            mqtt_enabled = user_input.get(CONF_MQTT_ENABLED, False)
            if mqtt_enabled:
                mqtt_configured = await self._check_mqtt_configuration()
                if not mqtt_configured:
                    errors["base"] = "mqtt_not_configured"
            
            if not errors:
                # Store MQTT settings
                self._options.update({
                    CONF_MQTT_ENABLED: user_input.get(CONF_MQTT_ENABLED, False),
                    CONF_MQTT_TOPIC_BUY: user_input.get(CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY),
                    CONF_MQTT_TOPIC_SELL: user_input.get(CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL),
                    CONF_MQTT_48H_MODE: user_input.get(CONF_MQTT_48H_MODE, False),
                })
                # Move to API retry settings
                return await self.async_step_api_retry()

        return self.async_show_form(
            step_id="mqtt_settings",
            data_schema=vol.Schema({
                vol.Required(CONF_MQTT_ENABLED, default=False): bool,
                vol.Optional(CONF_MQTT_TOPIC_BUY, default=DEFAULT_MQTT_TOPIC_BUY): str,
                vol.Optional(CONF_MQTT_TOPIC_SELL, default=DEFAULT_MQTT_TOPIC_SELL): str,
                vol.Optional(CONF_MQTT_48H_MODE, default=False): bool,
            }),
            errors=errors
        )

    async def async_step_api_retry(self, user_input=None):
        """Handle API retry configuration."""
        if user_input is not None:
            # Store retry settings
            self._options.update({
                CONF_RETRY_ATTEMPTS: user_input.get(CONF_RETRY_ATTEMPTS, DEFAULT_RETRY_ATTEMPTS),
                CONF_RETRY_DELAY: user_input.get(CONF_RETRY_DELAY, DEFAULT_RETRY_DELAY),
            })
            
            # Create the config entry
            return self.async_create_entry(
                title="Pstryk Energy", 
                data=self._data,
                options=self._options
            )

        return self.async_show_form(
            step_id="api_retry",
            data_schema=vol.Schema({
                vol.Optional(CONF_RETRY_ATTEMPTS, default=DEFAULT_RETRY_ATTEMPTS): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_RETRY_ATTEMPTS, max=MAX_RETRY_ATTEMPTS)
                ),
                vol.Optional(CONF_RETRY_DELAY, default=DEFAULT_RETRY_DELAY): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_RETRY_DELAY, max=MAX_RETRY_DELAY)
                ),
            })
        )
    
    async def _validate_api_key(self, api_key):
        """Validate API key by calling a simple API endpoint."""
        now = dt_util.utcnow()
        start_utc = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        endpoint = PRICING_ENDPOINT.format(start=start_utc, end=end_utc)
        url = f"{API_URL}{endpoint}"

        try:
            session = async_get_clientsession(self.hass)
            async with asyncio.timeout(API_TIMEOUT):
                resp = await session.get(
                    url,
                    headers={"Authorization": api_key, "Accept": "application/json"}
                )
                return resp.status == 200
        except (Exception, asyncio.TimeoutError):
            return False
    
    async def _check_mqtt_configuration(self):
        """Check if MQTT integration is properly configured."""
        try:
            return self.hass.services.has_service("mqtt", "publish")
        except Exception:
            return False
    
    async def _check_mqtt_availability(self):
        """Check if MQTT integration is available (not necessarily configured)."""
        try:
            return self.hass.services.has_service("mqtt", "publish")
        except Exception:
            return False
    
    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return PstrykOptionsFlowHandler()


class PstrykOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Pstryk Energy - single page view."""

    async def async_step_init(self, user_input=None):
        """Manage the options - single page for quick configuration."""
        errors = {}
        
        if user_input is not None:
            # Check MQTT configuration if enabled
            mqtt_enabled = user_input.get(CONF_MQTT_ENABLED, False)
            if mqtt_enabled:
                mqtt_configured = await self._check_mqtt_configuration()
                if not mqtt_configured:
                    errors["base"] = "mqtt_not_configured"
            
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        # Check if MQTT integration is loaded
        mqtt_enabled = False
        try:
            mqtt_enabled = self.hass.services.has_service("mqtt", "publish")
        except Exception:
            mqtt_enabled = False
        
        # Build schema with better organization
        # We use field naming to create visual grouping
        schema = {
            # Price Monitoring Settings - prefix with numbers for ordering
            vol.Required("buy_top", default=self.config_entry.options.get(
                "buy_top", self.config_entry.data.get("buy_top", 5))): 
                    vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
            vol.Required("sell_top", default=self.config_entry.options.get(
                "sell_top", self.config_entry.data.get("sell_top", 5))): 
                    vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
            vol.Required("buy_worst", default=self.config_entry.options.get(
                "buy_worst", self.config_entry.data.get("buy_worst", 5))): 
                    vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
            vol.Required("sell_worst", default=self.config_entry.options.get(
                "sell_worst", self.config_entry.data.get("sell_worst", 5))): 
                    vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
        }
        
        # MQTT Configuration (if available)
        if mqtt_enabled:
            schema.update({
                vol.Required(CONF_MQTT_ENABLED, default=self.config_entry.options.get(
                    CONF_MQTT_ENABLED, False)): bool,
                vol.Optional(CONF_MQTT_TOPIC_BUY, default=self.config_entry.options.get(
                    CONF_MQTT_TOPIC_BUY, DEFAULT_MQTT_TOPIC_BUY)): str,
                vol.Optional(CONF_MQTT_TOPIC_SELL, default=self.config_entry.options.get(
                    CONF_MQTT_TOPIC_SELL, DEFAULT_MQTT_TOPIC_SELL)): str,
                vol.Optional(CONF_MQTT_48H_MODE, default=self.config_entry.options.get(
                    CONF_MQTT_48H_MODE, False)): bool,
            })
        
        # API Configuration
        schema.update({
            vol.Optional(CONF_RETRY_ATTEMPTS, default=self.config_entry.options.get(
                CONF_RETRY_ATTEMPTS, DEFAULT_RETRY_ATTEMPTS)): 
                    vol.All(vol.Coerce(int), vol.Range(min=MIN_RETRY_ATTEMPTS, max=MAX_RETRY_ATTEMPTS)),
            vol.Optional(CONF_RETRY_DELAY, default=self.config_entry.options.get(
                CONF_RETRY_DELAY, DEFAULT_RETRY_DELAY)): 
                    vol.All(vol.Coerce(int), vol.Range(min=MIN_RETRY_DELAY, max=MAX_RETRY_DELAY)),
        })

        # Add description with section information
        description_text = "Configure your energy price monitoring settings"
        if mqtt_enabled:
            description_text += "\n\n**Note:** Settings are grouped by: Price Monitoring, MQTT Bridge, and API Configuration"

        return self.async_show_form(
            step_id="init", 
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "description": description_text
            }
        )

    async def _check_mqtt_configuration(self):
        """Check if MQTT is properly configured."""
        try:
            mqtt_config_entry = None
            for entry in self.hass.config_entries.async_entries("mqtt"):
                if entry.state == config_entries.ConfigEntryState.LOADED:
                    mqtt_config_entry = entry
                    break
            
            if not mqtt_config_entry:
                return False
            
            return self.hass.services.has_service("mqtt", "publish")
        except Exception:
            return False
