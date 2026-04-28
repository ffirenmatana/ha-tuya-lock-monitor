"""Services for Tuya Lock Monitor v2.

Two diagnostic services for investigating whether the firmware exposes a
real passage-mode DP (i.e. a way to keep the door unlocked without the
auto_lock_time refresh loop):

* ``tuya_lock_monitor_v2.dump_specifications``
    Calls ``GET /v1.0/devices/{device_id}/specifications`` and writes the
    full DP schema to a file in the HA config directory. The spec lists
    *every* DP the device firmware claims to support — including DPs that
    are hidden from the /status payload until they've been written to.
    No device state is changed.

* ``tuya_lock_monitor_v2.try_normal_open_switch``
    One-shot test: sends ``normal_open_switch = <bool>`` to the device.
    On many Tuya lock firmwares this DP is the actual passage-mode
    toggle; if the device accepts it, real passage mode with zero
    refresh cost is possible. Will log the API response (and any error
    code) so we can tell whether the DP was accepted.

Both services accept either ``entity_id`` (any entity belonging to the
target coordinator) or ``device_id`` (the Tuya device id).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_DEVICE_ID, CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN
from .coordinator import TuyaLockCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_DUMP_SPECIFICATIONS = "dump_specifications"
SERVICE_TRY_NORMAL_OPEN_SWITCH = "try_normal_open_switch"
SERVICE_TRY_DP_WRITE = "try_dp_write"

ATTR_VALUE = "value"
ATTR_CODE = "code"

# At least one of entity_id / device_id must be supplied.
_TARGET_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_DEVICE_ID): cv.string,
    }
)

DUMP_SCHEMA = _TARGET_SCHEMA.extend({})

TRY_NOS_SCHEMA = _TARGET_SCHEMA.extend(
    {
        vol.Required(ATTR_VALUE): cv.boolean,
    }
)

# Accepts bool, int, or string (so we can test Raw DPs with base64 payloads too).
TRY_DP_SCHEMA = _TARGET_SCHEMA.extend(
    {
        vol.Required(ATTR_CODE): cv.string,
        vol.Required(ATTR_VALUE): vol.Any(cv.boolean, vol.Coerce(int), cv.string),
    }
)


def _resolve_coordinator(
    hass: HomeAssistant, call: ServiceCall
) -> TuyaLockCoordinator | None:
    """Find the coordinator targeted by the service call."""
    entries: dict[str, TuyaLockCoordinator] = hass.data.get(DOMAIN, {})
    if not entries:
        _LOGGER.error("[TuyaSvc] No Tuya Lock Monitor v2 entries configured")
        return None

    entity_id = call.data.get(CONF_ENTITY_ID)
    device_id = call.data.get(CONF_DEVICE_ID)

    if entity_id:
        ent_reg = er.async_get(hass)
        entry = ent_reg.async_get(entity_id)
        if entry is not None and entry.config_entry_id in entries:
            return entries[entry.config_entry_id]

    if device_id:
        # Match by Tuya device_id (what the user actually sees in the IoT Platform).
        for coordinator in entries.values():
            if coordinator.device_id == device_id:
                return coordinator

    # Single-entry convenience — if only one lock is configured, target it.
    if len(entries) == 1:
        return next(iter(entries.values()))

    _LOGGER.error(
        "[TuyaSvc] Could not resolve target lock — pass entity_id or device_id "
        "(configured device_ids: %s)",
        ", ".join(c.device_id for c in entries.values()),
    )
    return None


async def _handle_dump_specifications(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    coordinator = _resolve_coordinator(hass, call)
    if coordinator is None:
        return

    try:
        spec = await coordinator.async_cloud_get_specifications()
    except UpdateFailed as err:
        _LOGGER.error("[TuyaSvc] dump_specifications failed: %s", err)
        return
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("[TuyaSvc] dump_specifications crashed: %s", err)
        return

    # Pull out the bits we care about for quick inspection in the log.
    functions = spec.get("functions") or []
    status_spec = spec.get("status") or []

    _LOGGER.warning(
        "[TuyaSvc] Specifications for device_id=%s category=%s — "
        "%d writable function DPs, %d status DPs",
        coordinator.device_id,
        spec.get("category"),
        len(functions),
        len(status_spec),
    )
    for fn in functions:
        _LOGGER.warning(
            "[TuyaSvc]   function  code=%-28s type=%-8s values=%s",
            fn.get("code"),
            fn.get("type"),
            fn.get("values"),
        )
    for st in status_spec:
        _LOGGER.warning(
            "[TuyaSvc]   status    code=%-28s type=%-8s values=%s",
            st.get("code"),
            st.get("type"),
            st.get("values"),
        )

    # Also persist the raw JSON so the user can send it back without
    # having to scrape the log.
    out_path = Path(hass.config.path(f"tuya_lock_v2_spec_{coordinator.device_id}.json"))

    def _write_file() -> None:
        out_path.write_text(json.dumps(spec, indent=2, sort_keys=True))

    await hass.async_add_executor_job(_write_file)
    _LOGGER.warning("[TuyaSvc] Full specifications written to %s", out_path)


async def _handle_try_normal_open_switch(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    coordinator = _resolve_coordinator(hass, call)
    if coordinator is None:
        return

    value: bool = bool(call.data[ATTR_VALUE])
    _LOGGER.warning(
        "[TuyaSvc] try_normal_open_switch: sending normal_open_switch=%s to %s "
        "(forced via cloud so we get a clean error code)",
        value, coordinator.device_id,
    )
    # Go via cloud directly — a local tinytuya write against an unsupported
    # DP often returns no error, which gives us a false 'accepted'. The
    # cloud /commands endpoint returns explicit error codes we can read.
    ok = await coordinator._cloud_send_command(  # noqa: SLF001
        [{"code": "normal_open_switch", "value": value}]
    )
    if ok:
        _LOGGER.warning(
            "[TuyaSvc] normal_open_switch=%s accepted — watch the lock; if it "
            "stays unlocked and auto_lock_time stops firing, this IS the "
            "real passage-mode DP.",
            value,
        )
    else:
        _LOGGER.error(
            "[TuyaSvc] normal_open_switch=%s was rejected. Check the log for "
            "the cloud error code; 501 ('don't support this command') means "
            "the firmware doesn't expose this DP as writable.",
            value,
        )


async def _handle_try_dp_write(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    coordinator = _resolve_coordinator(hass, call)
    if coordinator is None:
        return

    code: str = call.data[ATTR_CODE]
    value: Any = call.data[ATTR_VALUE]
    _LOGGER.warning(
        "[TuyaSvc] try_dp_write: sending %s=%r to %s via /commands "
        "(bypasses read-only DP filter so we can probe anything the spec claims is writable)",
        code, value, coordinator.device_id,
    )
    ok = await coordinator._cloud_send_command(  # noqa: SLF001
        [{"code": code, "value": value}]
    )
    if ok:
        _LOGGER.warning("[TuyaSvc] %s=%r accepted by /commands", code, value)
    else:
        _LOGGER.error(
            "[TuyaSvc] %s=%r rejected — see the preceding [TuyaCmd] log line "
            "for the Tuya error code.",
            code, value,
        )


async def async_register_services(hass: HomeAssistant) -> None:
    """Register diagnostic services. Safe to call more than once."""
    if hass.services.has_service(DOMAIN, SERVICE_DUMP_SPECIFICATIONS):
        return

    hass.services.async_register(
        DOMAIN,
        SERVICE_DUMP_SPECIFICATIONS,
        _handle_dump_specifications,
        schema=DUMP_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TRY_NORMAL_OPEN_SWITCH,
        _handle_try_normal_open_switch,
        schema=TRY_NOS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TRY_DP_WRITE,
        _handle_try_dp_write,
        schema=TRY_DP_SCHEMA,
    )


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Drop services when the last entry is unloaded."""
    # Only remove services if no entries remain.
    if hass.data.get(DOMAIN):
        return
    for svc in (
        SERVICE_DUMP_SPECIFICATIONS,
        SERVICE_TRY_NORMAL_OPEN_SWITCH,
        SERVICE_TRY_DP_WRITE,
    ):
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)
