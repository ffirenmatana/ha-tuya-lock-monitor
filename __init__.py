"""Tuya Lock Monitor integration setup."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_DEVICE_ID,
    CONF_ENDPOINT,
    DOMAIN,
)
from .coordinator import TuyaLockCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.LOCK]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tuya Lock Monitor from a config entry."""
    coordinator = TuyaLockCoordinator(
        hass,
        entry.data[CONF_ACCESS_ID],
        entry.data[CONF_ACCESS_SECRET],
        entry.data[CONF_DEVICE_ID],
        entry.data[CONF_ENDPOINT],
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
