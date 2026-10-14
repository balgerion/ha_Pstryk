import logging

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .price_refresh import async_refresh_prices, budget_free_at
from .sensor import get_integration_version

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    async_add_entities([PstrykRefreshPricesButton(entry.entry_id)])


class PstrykRefreshPricesButton(ButtonEntity):
    """Fetch buy and sell prices once, if the hourly API budget has room."""

    _attr_icon = "mdi:refresh"

    def __init__(self, entry_id: str):
        self.entry_id = entry_id
        self.entity_id = f"button.{DOMAIN}_refresh_prices"

    @property
    def name(self) -> str:
        return "Pstryk Refresh Prices"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_refresh_prices"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "pstryk_energy")},
            "name": "Pstryk Energy",
            "manufacturer": "Pstryk",
            "model": "Energy Price Monitor",
            "sw_version": get_integration_version(self.hass),
        }

    async def async_press(self) -> None:
        # Coordinators are created by the sensor platform, so look them up at press time.
        result = await async_refresh_prices(self.hass, self.entry_id, "button")
        if result == "budget":
            free_at = budget_free_at(self.hass, self.entry_id)
            persistent_notification.async_create(
                self.hass,
                "Prices were not refreshed: Pstryk allows 3 API requests per hour, and this hour's "
                "are used or Pstryk asked us to wait. Next free slot: "
                f"{dt_util.as_local(free_at).strftime('%H:%M') if free_at else 'unknown'}.",
                title="Pstryk: refresh not sent",
                notification_id=f"{DOMAIN}_refresh_budget",
            )
            return
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_refresh_budget")
        if result == "failed":
            persistent_notification.async_create(
                self.hass,
                "Prices were not refreshed: the request to Pstryk failed. The current prices stay "
                "in use; the Home Assistant log has the reason.",
                title="Pstryk: refresh failed",
                notification_id=f"{DOMAIN}_refresh_failed",
            )
            return
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_refresh_failed")
        _LOGGER.info("Refresh prices button: %s", result)
