"""Constants for Tuya Lock Monitor v2.

V2 changes vs v1:
  * Domain renamed so v1 and v2 can coexist on the same Home Assistant instance.
  * Per-entry user-ID → name strings removed from config-entry options.
    User names are now loaded from a single shared YAML file
    (`<config>/tuya_lock_users.yaml` by default) which every DL026HA-family
    entry consumes. Missing IDs pass through as-is; missing file is a warning.
  * Passage mode is real, not emulated: writing `automatic_lock=false`
    disables the auto-lock timer and the door stays unlocked (verified
    against the Tuya app's passage-mode toggle). Writing true re-enables
    auto-lock and relocks immediately.
  * `automatic_lock` is therefore writable (was wrongly listed as read-only
    in v1; the "phantom unlock" v1 saw was the DP doing exactly its job).
  * New control surfaces:
      - select.beep_volume           (mute / normal)
      - number.auto_lock_time        (1 – 1800 seconds)
      - switch.do_not_disturb        (optional quiet-hours master)
      - binary_sensor.auto_lock_armed (reflects `automatic_lock` status)
"""

DOMAIN = "tuya_lock_monitor_v2"

# --- Config entry keys ------------------------------------------------------
CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_DEVICE_ID = "device_id"
CONF_ENDPOINT = "endpoint"
CONF_LOCAL_IP = "local_ip"
CONF_LOCAL_KEY = "local_key"
CONF_LOCAL_VERSION = "local_version"
CONF_MODE = "mode"

# Optional override for the shared users-YAML location. If unset we fall back
# to <config>/tuya_lock_users.yaml, then <config>/custom_components/tuya_lock_users.yaml.
CONF_USERS_YAML_PATH = "users_yaml_path"

# --- Mode selection ---------------------------------------------------------
MODE_CLOUD = "cloud"
MODE_LOCAL = "local"

# --- Tuya cloud endpoints ---------------------------------------------------
ENDPOINTS = {
    "EU": "https://openapi.tuyaeu.com",
    "US": "https://openapi.tuyaus.com",
    "CN": "https://openapi.tuyacn.com",
    "IN": "https://openapi.tuyain.com",
}
DEFAULT_ENDPOINT = "https://openapi.tuyaeu.com"

# --- Polling cadences -------------------------------------------------------
UPDATE_INTERVAL = 60        # cloud-only scheduled refresh (s)
LOCAL_POLL_INTERVAL = 15    # minimum gap between local tinytuya polls (s)
PING_INTERVAL = 1           # TCP ping cadence for local mode (s)
CLOUD_META_REFRESH = 300    # how often to refresh cloud metadata / local_key (s)

# State-watch cadence (used after smart-lock door-operate on DL026HA).
STATE_WATCH_DURATION = 20.0
STATE_WATCH_INTERVAL = 2.0

# --- Local (tinytuya) protocol ---------------------------------------------
LOCAL_VERSIONS = ["3.3", "3.4", "3.5"]
DEFAULT_LOCAL_VERSION = "3.4"

# --- DPS ↔ status code mapping ---------------------------------------------
# (Unchanged from v1 — seeded from diagnostics across DL026HA / DL031HA.)
DPS_TO_CODE: dict[str, str] = {
    "1": "unlock_fingerprint",
    "2": "unlock_password",
    "3": "unlock_temporary",
    "5": "unlock_card",
    "8": "alarm_lock",
    "9": "unlock_request",
    "12": "residual_electricity",
    "13": "reverse_lock",
    "15": "unlock_app",
    "16": "hijack",
    "19": "doorbell",
    "32": "unlock_offline_pd",
    "33": "unlock_offline_clear",
    "44": "unlock_double_kit",
    "49": "remote_no_pd_setkey",
    "50": "remote_no_dp_key",
    "58": "normal_open_switch",
}
CODE_TO_DPS: dict[str, int] = {v: int(k) for k, v in DPS_TO_CODE.items()}

# --- Status codes (v1 carried forward) -------------------------------------
STATUS_UNLOCK_FINGERPRINT = "unlock_fingerprint"
STATUS_UNLOCK_PASSWORD = "unlock_password"
STATUS_UNLOCK_TEMPORARY = "unlock_temporary"
STATUS_UNLOCK_CARD = "unlock_card"
STATUS_ALARM_LOCK = "alarm_lock"
STATUS_UNLOCK_REQUEST = "unlock_request"
STATUS_RESIDUAL_ELECTRICITY = "residual_electricity"
STATUS_REVERSE_LOCK = "reverse_lock"
STATUS_UNLOCK_APP = "unlock_app"
STATUS_HIJACK = "hijack"
STATUS_DOORBELL = "doorbell"
STATUS_NORMAL_OPEN_SWITCH = "normal_open_switch"

# DL026HA status codes (BLE sub-device of SG120HA gateway).
STATUS_LOCK_MOTOR_STATE = "lock_motor_state"        # bool — true=UNLOCKED, false=LOCKED (firmware reports motor-engaged state, inverted from name)
STATUS_AUTOMATIC_LOCK = "automatic_lock"            # bool — writable: true=auto-lock on, false=passage mode
STATUS_UNLOCK_BLE = "unlock_ble"                    # int counter — BLE unlock events
STATUS_UNLOCK_PHONE_REMOTE = "unlock_phone_remote"  # int counter — remote app unlocks

# --- v2 control-surface DPs ------------------------------------------------
STATUS_BEEP_VOLUME = "beep_volume"              # enum — "mute" | "normal"
STATUS_AUTO_LOCK_TIME = "auto_lock_time"        # int seconds — 1..1800
STATUS_DO_NOT_DISTURB = "do_not_disturb"        # bool — quiet-hours master

BEEP_VOLUME_OPTIONS: list[str] = ["mute", "normal"]
AUTO_LOCK_TIME_MIN = 1
AUTO_LOCK_TIME_MAX = 1800
AUTO_LOCK_TIME_STEP = 1
AUTO_LOCK_TIME_DEFAULT = 30

# --- Passage mode ----------------------------------------------------------
# Real passage mode IS reachable on DL026HA firmware: the `automatic_lock` DP
# is writable and behaves inversely to its name (true → stay unlocked,
# false → relock). When entering passage mode we also bump `auto_lock_time`
# to its maximum as a hardware-level safety backstop — if HA crashes before
# async_shutdown writes automatic_lock=false, the lock still re-engages
# after this timer fires (worst-case 30 minutes of unlocked exposure).
PASSAGE_MODE_MAX_AUTO_LOCK = 1800   # seconds — max value the DP accepts

# --- Home Assistant bus events --------------------------------------------
EVENT_UNLOCK = "tuya_lock_monitor_v2_unlock"
# Fired when a new fingerprint / password / card unlock ID is observed.
# Payload: {entry_id, device_id, device_name, kind, id, time}

# --- Smart Lock cloud API paths --------------------------------------------
# Confirmed against DL026HA firmware on openapi.tuyaeu.com during captures.
SMART_LOCK_TICKET_PATH = "/v1.0/smart-lock/devices/{device_id}/password-ticket"
SMART_LOCK_DOOR_OPERATE_PATH = "/v1.0/smart-lock/devices/{device_id}/password-free/door-operate"

# --- Users YAML ------------------------------------------------------------
# Looked up (first-match wins) relative to the HA config directory.
USERS_YAML_CANDIDATES: tuple[str, ...] = (
    "tuya_lock_users.yaml",
    "custom_components/tuya_lock_users.yaml",
    "custom_components/tuya_lock_monitor_v2/tuya_lock_users.yaml",
)

# Top-level keys inside the YAML.
USERS_YAML_FINGERPRINT_KEY = "fingerprint_names"
USERS_YAML_PASSWORD_KEY = "password_names"
USERS_YAML_CARD_KEY = "card_names"
