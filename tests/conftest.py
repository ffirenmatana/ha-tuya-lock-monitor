"""Make the integration importable both ways the tests need it.

* `tuya_lock_monitor_v2`                    — plain package (test_coordinator.py)
* `custom_components.tuya_lock_monitor_v2`  — how HA's loader finds it
  (test_setup.py), via the symlink under tests/_ha_root/.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE.parent, HERE, HERE / "_ha_root"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# HA's loader does `import custom_components` after putting its config dir at
# sys.path[0]; the test harness ships a regular package of that name there,
# which would shadow ours. Whichever is imported first wins — so import ours.
import custom_components  # noqa: E402,F401
