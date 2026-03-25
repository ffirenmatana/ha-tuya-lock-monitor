"""Constants for the Tuya Lock Monitor integration."""

DOMAIN = "tuya_lock_monitor"

CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_DEVICE_ID = "device_id"
CONF_ENDPOINT = "endpoint"
CONF_LOCAL_IP = "local_ip"
CONF_LOCAL_VERSION = "local_version"

ENDPOINTS = {
    "EU": "https://openapi.tuyaeu.com",
    "US": "https://openapi.tuyaus.com",
    "CN": "https://openapi.tuyacn.com",
    "IN": "https://openapi.tuyain.com",
}

DEFAULT_ENDPOINT = "https://openapi.tuyaeu.com"
UPDATE_INTERVAL = 60           # seconds — cloud-only mode
LOCAL_UPDATE_INTERVAL = 15     # seconds — local mode
CLOUD_META_REFRESH = 300       # seconds — how often to refresh metadata from cloud in local mode
LOCAL_FAIL_THRESHOLD = 3       # consecutive local failures before switching to cloud fallback

LOCAL_VERSIONS = ["3.3", "3.4", "3.5"]
DEFAULT_LOCAL_VERSION = "3.4"

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
