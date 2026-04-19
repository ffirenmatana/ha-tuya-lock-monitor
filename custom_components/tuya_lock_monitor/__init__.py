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
    CONF_LOCAL_IP,
    CONF_LOCAL_KEY,
    CONF_LOCAL_VERSION,
    DEFAULT_LOCAL_VERSION,
    DOMAIN,
)
from .coordinator import TuyaLockCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.LOCK, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tuya Lock Monitor from a config entry."""
    local_ip = entry.options.get(CONF_LOCAL_IP) or entry.data.get(CONF_LOCAL_IP) or None
    local_version = entry.options.get(CONF_LOCAL_VERSION) or entry.data.get(CONF_LOCAL_VERSION, DEFAULT_LOCAL_VERSION)
    local_key = entry.options.get(CONF_LOCAL_KEY) or entry.data.get(CONF_LOCAL_KEY) or None
    coordinator = TuyaLockCoordinator(
        hass,
        entry.data.get(CONF_ACCESS_ID, ""),
        entry.data.get(CONF_ACCESS_SECRET, ""),
        entry.data[CONF_DEVICE_ID],
        entry.data.get(CONF_ENDPOINT, ""),
        local_ip=local_ip,
        local_version=local_version,
        local_key_direct=local_key,
    )

    await coordinator.async_config_entry_first_refresh()

    # Start the 1-second ping loop if a local IP is configured.
    # The loop runs for the lifetime of the entry and is cancelled on unload.
    if local_ip:
        await coordinator.async_start_ping_loop()
        entry.async_on_unload(coordinator.async_stop_ping_loop)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
