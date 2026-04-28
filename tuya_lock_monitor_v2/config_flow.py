"""Config flow for Tuya Lock Monitor v2.

Changes vs v1:
  * Per-entry fingerprint/password/card name fields removed. User names
    now come from a shared YAML file (see users_yaml.py).
  * Options flow gains an optional override path for that YAML.
"""
from __future__ import annotations

import logging
import traceback
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_DEVICE_ID,
    CONF_ENDPOINT,
    CONF_LOCAL_IP,
    CONF_LOCAL_KEY,
    CONF_LOCAL_VERSION,
    CONF_MODE,
    CONF_USERS_YAML_PATH,
    DEFAULT_ENDPOINT,
    DEFAULT_LOCAL_VERSION,
    DOMAIN,
    ENDPOINTS,
    LOCAL_VERSIONS,
    MODE_CLOUD,
    MODE_LOCAL,
)
from .coordinator import TuyaLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def _validate_cloud(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
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
        _LOGGER.error("Tuya cloud validation failed: %s | %s", err, type(err).__name__)
        _LOGGER.debug("Traceback:\n%s", traceback.format_exc())
        msg = str(err).lower()
        if "network" in msg or "connection" in msg or "timeout" in msg:
            return {"base": "cannot_connect"}
        if any(x in msg for x in ("token", "2002", "2406", "invalid", "signature", "sign", "auth")):
            return {"base": "invalid_auth"}
        return {"base": "unknown"}
    return {}


async def _validate_local(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    coordinator = TuyaLockCoordinator(
        hass,
        access_id="",
        access_secret="",
        device_id=data[CONF_DEVICE_ID],
        endpoint="",
        local_ip=data[CONF_LOCAL_IP].strip(),
        local_version=data.get(CONF_LOCAL_VERSION, DEFAULT_LOCAL_VERSION),
        local_key_direct=data[CONF_LOCAL_KEY].strip(),
    )
    try:
        await coordinator._async_update_data()
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Tuya local validation failed: %s | %s", err, type(err).__name__)
        _LOGGER.debug("Traceback:\n%s", traceback.format_exc())
        msg = str(err).lower()
        if any(x in msg for x in ("timeout", "connection", "network", "refused", "host")):
            return {"base": "cannot_connect"}
        return {"base": "local_failed"}
    return {}


class TuyaLockMonitorV2ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Initial setup UI for v2."""

    VERSION = 1

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(
        entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return TuyaLockMonitorV2OptionsFlow(entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            if user_input[CONF_MODE] == MODE_LOCAL:
                return await self.async_step_local()
            return await self.async_step_cloud()

        schema = vol.Schema(
            {
                vol.Required(CONF_MODE, default=MODE_CLOUD): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {
                                "value": MODE_CLOUD,
                                "label": "Cloud (recommended — requires Tuya IoT Platform account)",
                            },
                            {
                                "value": MODE_LOCAL,
                                "label": "Local only — enter local key manually, no cloud account needed",
                            },
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get(CONF_LOCAL_IP):
                user_input[CONF_LOCAL_IP] = user_input[CONF_LOCAL_IP].strip()
            errors = await _validate_cloud(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()
                user_input[CONF_MODE] = MODE_CLOUD
                return self.async_create_entry(
                    title=f"Tuya Lock v2 ({user_input[CONF_DEVICE_ID]})",
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
                vol.Optional(
                    CONF_LOCAL_VERSION, default=DEFAULT_LOCAL_VERSION
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": v, "label": f"Protocol {v}"} for v in LOCAL_VERSIONS
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="cloud", data_schema=schema, errors=errors)

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            if user_input.get("method") == "scan":
                return await self.async_step_local_scan()
            return await self.async_step_local_manual()

        schema = vol.Schema(
            {
                vol.Required("method", default="scan"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {
                                "value": "scan",
                                "label": "Scan network — auto-discover IP and Device ID",
                            },
                            {
                                "value": "manual",
                                "label": "Enter all details manually",
                            },
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="local", data_schema=schema)

    async def async_step_local_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            selected = user_input.get("selected_device", "_manual")
            if selected and selected != "_manual":
                info = getattr(self, "_discovered", {}).get(selected, {})
                self._prefill: dict[str, str] = {
                    CONF_DEVICE_ID: selected,
                    CONF_LOCAL_IP: info.get("ip", ""),
                    CONF_LOCAL_VERSION: str(
                        info.get("version", DEFAULT_LOCAL_VERSION)
                    ),
                }
            return await self.async_step_local_manual()

        import tinytuya  # noqa: PLC0415

        def _scan() -> dict:
            try:
                return (
                    tinytuya.deviceScan(verbose=False, maxretry=6, color=False)
                    or {}
                )
            except Exception:  # noqa: BLE001
                return {}

        self._discovered: dict = await self.hass.async_add_executor_job(_scan)

        if not self._discovered:
            return self.async_show_form(
                step_id="local_scan",
                data_schema=vol.Schema({}),
                errors={"base": "no_devices_found"},
            )

        options = [
            {
                "value": dev_id,
                "label": (
                    f"{info.get('ip', 'Unknown IP')} — {dev_id}"
                    f" (protocol v{info.get('version', '?')})"
                ),
            }
            for dev_id, info in self._discovered.items()
        ]
        options.append(
            {"value": "_manual", "label": "Device not listed — enter manually"}
        )

        schema = vol.Schema(
            {
                vol.Required("selected_device"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                ),
            }
        )
        return self.async_show_form(step_id="local_scan", data_schema=schema)

    async def async_step_local_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        prefill: dict[str, str] = getattr(self, "_prefill", {})

        if user_input is not None:
            user_input[CONF_LOCAL_IP] = user_input[CONF_LOCAL_IP].strip()
            user_input[CONF_LOCAL_KEY] = user_input[CONF_LOCAL_KEY].strip()
            errors = await _validate_local(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()
                user_input[CONF_MODE] = MODE_LOCAL
                return self.async_create_entry(
                    title=f"Tuya Lock v2 ({user_input[CONF_DEVICE_ID]})",
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_ID, default=prefill.get(CONF_DEVICE_ID, "")
                ): str,
                vol.Required(CONF_LOCAL_KEY): str,
                vol.Required(
                    CONF_LOCAL_IP, default=prefill.get(CONF_LOCAL_IP, "")
                ): str,
                vol.Optional(
                    CONF_LOCAL_VERSION,
                    default=prefill.get(CONF_LOCAL_VERSION, DEFAULT_LOCAL_VERSION),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": v, "label": f"Protocol {v}"}
                            for v in LOCAL_VERSIONS
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="local_manual", data_schema=schema, errors=errors
        )


class TuyaLockMonitorV2OptionsFlow(config_entries.OptionsFlow):
    """Allow settings to be changed after setup."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        mode = self._entry.data.get(CONF_MODE, MODE_CLOUD)

        if user_input is not None:
            for key in (CONF_LOCAL_IP, CONF_LOCAL_KEY, CONF_USERS_YAML_PATH):
                if user_input.get(key):
                    user_input[key] = user_input[key].strip()
            return self.async_create_entry(title="", data=user_input)

        current_users_path = self._entry.options.get(
            CONF_USERS_YAML_PATH, self._entry.data.get(CONF_USERS_YAML_PATH, "")
        )

        if mode == MODE_LOCAL:
            current_key = self._entry.options.get(
                CONF_LOCAL_KEY, self._entry.data.get(CONF_LOCAL_KEY, "")
            )
            current_ip = self._entry.options.get(
                CONF_LOCAL_IP, self._entry.data.get(CONF_LOCAL_IP, "")
            )
            current_version = self._entry.options.get(
                CONF_LOCAL_VERSION,
                self._entry.data.get(CONF_LOCAL_VERSION, DEFAULT_LOCAL_VERSION),
            )
            schema = vol.Schema(
                {
                    vol.Required(CONF_LOCAL_KEY, default=current_key): str,
                    vol.Required(CONF_LOCAL_IP, default=current_ip): str,
                    vol.Optional(
                        CONF_LOCAL_VERSION, default=current_version
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": v, "label": f"Protocol {v}"} for v in LOCAL_VERSIONS
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional(
                        CONF_USERS_YAML_PATH, default=current_users_path
                    ): str,
                }
            )
        else:
            current_ip = self._entry.options.get(
                CONF_LOCAL_IP, self._entry.data.get(CONF_LOCAL_IP, "")
            )
            current_version = self._entry.options.get(
                CONF_LOCAL_VERSION,
                self._entry.data.get(CONF_LOCAL_VERSION, DEFAULT_LOCAL_VERSION),
            )
            schema = vol.Schema(
                {
                    vol.Optional(CONF_LOCAL_IP, default=current_ip): str,
                    vol.Optional(
                        CONF_LOCAL_VERSION, default=current_version
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": v, "label": f"Protocol {v}"} for v in LOCAL_VERSIONS
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional(
                        CONF_USERS_YAML_PATH, default=current_users_path
                    ): str,
                }
            )

        return self.async_show_form(step_id="init", data_schema=schema)
