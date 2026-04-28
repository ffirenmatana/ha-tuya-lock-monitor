"""Shared user-ID → name mapping, loaded from a single YAML file.

Every DL026HA-family entry in this integration consumes the same file so
fingerprint slot 2 always resolves to "Mum" whichever lock raised the event.

Example ``<config>/tuya_lock_users.yaml``:

    fingerprint_names:
      1: Pat
      2: Alex
      3: Guest
    password_names:
      1: Front door code
    card_names:
      1: Keyfob 1

Lookups are cached in ``hass.data`` under the DOMAIN namespace. Call
:func:`async_reload_users` (or reload the config entry) to pick up edits.
Missing files are a one-time warning; missing IDs simply pass through.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_USERS_YAML_PATH,
    DOMAIN,
    USERS_YAML_CANDIDATES,
    USERS_YAML_CARD_KEY,
    USERS_YAML_FINGERPRINT_KEY,
    USERS_YAML_PASSWORD_KEY,
)

_LOGGER = logging.getLogger(__name__)

# Key inside hass.data[DOMAIN] where the parsed maps live.
_USERS_CACHE_KEY = "_users_cache"

# Inner structure:
#   {
#       "path": "<resolved absolute path, or None>",
#       "maps": {
#           "fingerprint_names": {1: "Pat", ...},
#           "password_names":    {...},
#           "card_names":        {...},
#       },
#       "warned_missing": True|False,
#   }


def _candidate_paths(hass: HomeAssistant, override: str | None) -> list[str]:
    paths: list[str] = []
    if override:
        if os.path.isabs(override):
            paths.append(override)
        else:
            paths.append(hass.config.path(override))
    for rel in USERS_YAML_CANDIDATES:
        paths.append(hass.config.path(rel))
    return paths


def _parse_yaml(path: str) -> dict[str, dict[int, str]]:
    """Parse ``path`` into three str→str maps keyed by integer id.

    Falls back to an empty dict on any parse/IO error.
    """
    try:
        # HA ships PyYAML; import late so this module is cheap at import time.
        import yaml  # noqa: PLC0415
    except ImportError:
        _LOGGER.warning("[TuyaUsers] PyYAML unavailable — user names disabled")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except OSError as err:
        _LOGGER.debug("[TuyaUsers] Could not read %s: %s", path, err)
        return {}
    except yaml.YAMLError as err:
        _LOGGER.warning("[TuyaUsers] Invalid YAML in %s: %s", path, err)
        return {}

    if not isinstance(raw, dict):
        _LOGGER.warning(
            "[TuyaUsers] %s must be a mapping of sections, got %s", path, type(raw).__name__
        )
        return {}

    out: dict[str, dict[int, str]] = {}
    for key in (USERS_YAML_FINGERPRINT_KEY, USERS_YAML_PASSWORD_KEY, USERS_YAML_CARD_KEY):
        section = raw.get(key)
        if section is None:
            out[key] = {}
            continue
        if not isinstance(section, dict):
            _LOGGER.warning(
                "[TuyaUsers] %s: '%s' must be a mapping (id: name), got %s",
                path, key, type(section).__name__,
            )
            out[key] = {}
            continue
        normalised: dict[int, str] = {}
        for rid, name in section.items():
            try:
                normalised[int(rid)] = str(name) if name is not None else ""
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "[TuyaUsers] %s: ignoring non-integer id '%s' under '%s'",
                    path, rid, key,
                )
        out[key] = normalised
    return out


def _load_from_disk(hass: HomeAssistant, override: str | None) -> dict[str, Any]:
    for path in _candidate_paths(hass, override):
        if os.path.isfile(path):
            maps = _parse_yaml(path)
            if not maps:
                # _parse_yaml already logged the reason; use empty defaults.
                maps = {
                    USERS_YAML_FINGERPRINT_KEY: {},
                    USERS_YAML_PASSWORD_KEY: {},
                    USERS_YAML_CARD_KEY: {},
                }
            total = sum(len(v) for v in maps.values())
            _LOGGER.info(
                "[TuyaUsers] Loaded %d user names from %s", total, path
            )
            return {"path": path, "maps": maps, "warned_missing": True}
    return {
        "path": None,
        "maps": {
            USERS_YAML_FINGERPRINT_KEY: {},
            USERS_YAML_PASSWORD_KEY: {},
            USERS_YAML_CARD_KEY: {},
        },
        "warned_missing": False,
    }


def _ensure_cache(hass: HomeAssistant, override: str | None) -> dict[str, Any]:
    domain_bucket = hass.data.setdefault(DOMAIN, {})
    cache = domain_bucket.get(_USERS_CACHE_KEY)
    if cache is None:
        cache = _load_from_disk(hass, override)
        domain_bucket[_USERS_CACHE_KEY] = cache
    elif cache["path"] is None and not cache["warned_missing"]:
        # First time any entry actually tries to look up a name with no file present.
        _LOGGER.warning(
            "[TuyaUsers] No tuya_lock_users.yaml found in any of: %s — "
            "person_name attributes will be blank. Create one to map IDs to names.",
            ", ".join(_candidate_paths(hass, override)),
        )
        cache["warned_missing"] = True
    return cache


def async_reload_users(hass: HomeAssistant, override: str | None = None) -> None:
    """Drop the parsed cache and re-read on next lookup.

    Synchronous entry point — safe to call from executor threads. Also safe
    to call directly from the event loop for a tiny file.
    """
    domain_bucket = hass.data.setdefault(DOMAIN, {})
    domain_bucket.pop(_USERS_CACHE_KEY, None)
    _ensure_cache(hass, override)


async def async_reload_users_on_loop(
    hass: HomeAssistant, override: str | None = None
) -> None:
    """Async wrapper that runs the disk read in the default executor.

    Prefer this from integration setup so the event loop isn't blocked by
    the YAML read.
    """
    def _do_reload() -> dict[str, Any]:
        return _load_from_disk(hass, override)

    cache = await hass.async_add_executor_job(_do_reload)
    hass.data.setdefault(DOMAIN, {})[_USERS_CACHE_KEY] = cache


def resolve_name(
    hass: HomeAssistant,
    kind: str,
    raw_id: Any,
    override: str | None = None,
) -> str:
    """Return a human-readable name for ``raw_id`` in the named section.

    ``kind`` must be one of ``fingerprint_names``, ``password_names``,
    ``card_names``. Unknown kinds, non-integer IDs, or missing entries all
    fall back to ``str(raw_id)`` so the caller's attribute never ends up
    as ``None``.
    """
    cache = _ensure_cache(hass, override)
    maps = cache["maps"]
    section = maps.get(kind, {})

    try:
        lookup_id = int(raw_id)
    except (TypeError, ValueError):
        return str(raw_id) if raw_id is not None else ""

    name = section.get(lookup_id)
    if name:
        return name
    return str(lookup_id)


def resolve_fingerprint(hass: HomeAssistant, raw_id: Any, override: str | None = None) -> str:
    return resolve_name(hass, USERS_YAML_FINGERPRINT_KEY, raw_id, override)


def resolve_password(hass: HomeAssistant, raw_id: Any, override: str | None = None) -> str:
    return resolve_name(hass, USERS_YAML_PASSWORD_KEY, raw_id, override)


def resolve_card(hass: HomeAssistant, raw_id: Any, override: str | None = None) -> str:
    return resolve_name(hass, USERS_YAML_CARD_KEY, raw_id, override)
