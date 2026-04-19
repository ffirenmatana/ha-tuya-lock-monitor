"""Constants for the Tuya Lock Monitor integration."""

DOMAIN = "tuya_lock_monitor"

CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_DEVICE_ID = "device_id"
CONF_ENDPOINT = "endpoint"
CONF_LOCAL_IP = "local_ip"
CONF_LOCAL_KEY = "local_key"
CONF_LOCAL_VERSION = "local_version"
CONF_MODE = "mode"

# User-defined code → name mappings (stored in entry options as "1=Dad, 2=Mum" strings)
CONF_FINGERPRINT_NAMES = "fingerprint_names"
CONF_PASSWORD_NAMES = "password_names"
CONF_CARD_NAMES = "card_names"

MODE_CLOUD = "cloud"
MODE_LOCAL = "local"

ENDPOINTS = {
    "EU": "https://openapi.tuyaeu.com",
    "US": "https://openapi.tuyaus.com",
    "CN": "https://openapi.tuyacn.com",
    "IN": "https://openapi.tuyain.com",
}

DEFAULT_ENDPOINT = "https://openapi.tuyaeu.com"
UPDATE_INTERVAL = 60        # seconds — cloud-only scheduled refresh
LOCAL_POLL_INTERVAL = 15    # seconds — minimum gap between local tinytuya polls
PING_INTERVAL = 1           # seconds — how often to TCP-ping the device
CLOUD_META_REFRESH = 300    # seconds — how often to refresh cloud metadata / local_key

LOCAL_VERSIONS = ["3.3", "3.4", "3.5"]
DEFAULT_LOCAL_VERSION = "3.4"

# State-watch cadence (used after smart-lock door-operate on DL026HA)
STATE_WATCH_DURATION = 20.0   # seconds — maximum burst-poll window
STATE_WATCH_INTERVAL = 2.0    # seconds — cadence inside that window

# DPS number → status code (from device local_strategy in diagnostics)
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

# status code → DPS number (for sending commands locally)
CODE_TO_DPS: dict[str, int] = {v: int(k) for k, v in DPS_TO_CODE.items()}

# Status codes from the device
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

# DL026HA (and similar BLE-gateway lock) status codes
STATUS_LOCK_MOTOR_STATE = "lock_motor_state"        # bool — reflects the "lock status" enum
STATUS_AUTOMATIC_LOCK = "automatic_lock"            # bool — auto-relatch on close
STATUS_UNLOCK_BLE = "unlock_ble"                    # int counter — BLE unlock events
STATUS_UNLOCK_PHONE_REMOTE = "unlock_phone_remote"  # int counter — remote app unlocks

# Smart Lock cloud API paths (confirmed against DL026HA firmware on openapi.tuyaeu.com)
SMART_LOCK_TICKET_PATH = "/v1.0/smart-lock/devices/{device_id}/password-ticket"
SMART_LOCK_DOOR_OPERATE_PATH = "/v1.0/smart-lock/devices/{device_id}/password-free/door-operate"