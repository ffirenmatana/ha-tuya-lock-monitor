"""Config flow for Tuya Lock Monitor."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode

from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_DEVICE_ID,
    CONF_ENDPOINT,
    CONF_LOCAL_IP,
    CONF_LOCAL_VERSION,
    DEFAULT_ENDPOINT,
    DEFAULT_LOCAL_VERSION,
    DOMAIN,
    ENDPOINTS,
    LOCAL_VERSIONS,
)
from .coordinator import TuyaLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def _validate_credentials(
    hass: HomeAssistant, data: dict[str, Any]
) -> dict[str, str]:
    """Test connectivity and return any errors."""
    coordinator = TuyaLockCoordinator(
        hass,
        data[CONF_ACCESS_ID],
        data[CONF_ACCESS_SECRET],
        data[CONF_DEVICE_ID],
        data[CONF_ENDPOINT],
        local_ip=data.get(CONF_LOCAL_IP) or None,
        local_version=data.get(CONF_LOCAL_VERSION, DEFAULT_LOCAL_VERSION),
    )
    try:
        await coordinator._async_update_data()
    except Exception as err:  # noqa: BLE001
        _LOGGER.error(
            "Tuya validation failed — full error: %s | type: %s",
            err, type(err).__name__,
        )
        import traceback
        _LOGGER.debug("Tuya validation traceback:\n%s", traceback.format_exc())
        msg = str(err).lower()
        if "network" in msg or "connection" in msg or "timeout" in msg:
            return {"base": "cannot_connect"}
        if any(x in msg for x in ("token", "2002", "2406", "invalid", "signature", "sign", "auth")):
            return {"base": "invalid_auth"}
        return {"base": "unknown"}
    return {}


class TuyaLockMonitorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # Strip whitespace from optional local IP
            if user_input.get(CONF_LOCAL_IP):
                user_input[CONF_LOCAL_IP] = user_input[CONF_LOCAL_IP].strip()
            errors = await _validate_credentials(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Tuya Lock ({user_input[CONF_DEVICE_ID]})",
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ACCESS_ID): str,
                vol.Required(CONF_ACCESS_SECRET): str,
                vol.Required(CONF_DEVICE_ID): str,
                vol.Required(CONF_ENDPOINT, default=DEFAULT_ENDPOINT): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": url, "label": f"{region} — {url}"}
                            for region, url in ENDPOINTS.items()
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(CONF_LOCAL_IP): str,
                vol.Optional(CONF_LOCAL_VERSION, default=DEFAULT_LOCAL_VERSION): SelectSelector(
                    SelectSelectorConfig(
                        options=[{"value": v, "label": f"Protocol {v}"} for v in LOCAL_VERSIONS],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )



async def _validate_credentials(
    hass: HomeAssistant, data: dict[str, Any]
) -> dict[str, str]:
    """Test connectivity and return any errors."""
    coordinator = TuyaLockCoordinator(
        hass,
        data[CONF_ACCESS_ID],
        data[CONF_ACCESS_SECRET],
        data[CONF_DEVICE_ID],
        data[CONF_ENDPOINT],
    )
    try:
        await coordinator._async_update_data()
    except Exception as err:  # noqa: BLE001
        _LOGGER.error(
            "Tuya validation failed — full error: %s | type: %s",
            err, type(err).__name__,
        )
        import traceback
        _LOGGER.debug("Tuya validation traceback:\n%s", traceback.format_exc())
        msg = str(err).lower()
        if "network" in msg or "connection" in msg or "timeout" in msg:
            return {"base": "cannot_connect"}
        if any(x in msg for x in ("token", "2002", "2406", "invalid", "signature", "sign", "auth")):
            return {"base": "invalid_auth"}
        return {"base": "unknown"}
    return {}


class TuyaLockMonitorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await _validate_credentials(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Tuya Lock ({user_input[CONF_DEVICE_ID]})",
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ACCESS_ID): str,
                vol.Required(CONF_ACCESS_SECRET): str,
                vol.Required(CONF_DEVICE_ID): str,
                vol.Required(CONF_ENDPOINT, default=DEFAULT_ENDPOINT): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": url, "label": f"{region} — {url}"}
                            for region, url in ENDPOINTS.items()
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
