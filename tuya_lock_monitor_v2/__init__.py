"""Tuya Lock Monitor v2 — integration setup."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant

from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_DEVICE_ID,
    CONF_ENDPOINT,
    CONF_LOCAL_IP,
    CONF_LOCAL_KEY,
    CONF_LOCAL_VERSION,
    CONF_USERS_YAML_PATH,
    DEFAULT_LOCAL_VERSION,
    DOMAIN,
)
from .coordinator import TuyaLockCoordinator
from .services import async_register_services, async_unregister_services
from .users_yaml import async_reload_users_on_loop

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.LOCK,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Tuya Lock Monitor v2 entry."""
    # Pre-load the shared users YAML once — every entry consumes the same file.
    users_override = (
        entry.options.get(CONF_USERS_YAML_PATH)
        or entry.data.get(CONF_USERS_YAML_PATH)
        or None
    )
    await async_reload_users_on_loop(hass, users_override)

    local_ip = entry.options.get(CONF_LOCAL_IP) or entry.data.get(CONF_LOCAL_IP) or None
    local_version = (
        entry.options.get(CONF_LOCAL_VERSION)
        or entry.data.get(CONF_LOCAL_VERSION, DEFAULT_LOCAL_VERSION)
    )
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

    if local_ip:
        await coordinator.async_start_ping_loop()
        entry.async_on_unload(coordinator.async_stop_ping_loop)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_register_services(hass)

    # Safety hook: if passage mode is active and HA is stopping (clean
    # shutdown / restart), relock the door before HA goes away. The
    # 30-minute auto_lock_time backstop set by async_enter_passage_mode
    # covers the case where HA dies before this listener can fire.
    async def _on_ha_stop(_event: Event) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_ha_stop)
    )
    # Also relock on integration unload (e.g. when the user disables or
    # reconfigures the entry while passage mode is on).
    entry.async_on_unload(coordinator.async_shutdown)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await async_unregister_services(hass)
    return unload_ok
