"""
Settings for matching PowerFactory switches to IPS records (Energex path).

The matcher itself is mapping/switch_matcher.py; ips_data/ex_switch_matching.py
runs it inside IPStoPF; switch_match_audit.py runs it offline over the whole
fleet.

MODE
    "legacy" - the original lookup (switch name up to the first "_") drives
               the model. The new matcher does not run.
    "shadow" - the original lookup still drives the model. The new matcher
               runs alongside it, logs every switch where it would give a
               different result, and writes the match log. Nothing in the
               model changes because of it.
    "new"    - the new matcher drives the model.

Start on "shadow", compare a run's log lines and match logs with what you
expect, then switch to "new".
"""

from pathlib import Path

from config.paths import (
    CB_ALT_NAMES_DIR,
    OUTPUT_LOCAL_DIR,
    get_protection_batch_output_dir,
)

MODE = "new"

# Rule names from mapping/switch_matcher.py to add to, or remove from, its
# DEFAULT_RULES. For example ENABLE_RULES = ("transformer_cb",) also matches
# zone-substation transformer LV CBs (KVG1X12 -> CB1X12), which brings
# transformer protection into the distribution models.
ENABLE_RULES = ()
DISABLE_RULES = ()

# One CSV per project describing every switch and how it matched. The
# recurring audit (switch_match_audit.py --switches <folder>) reads these.
WRITE_MATCH_LOG = True
MATCH_LOG_FOLDER = "switch_match"


def get_overrides_file() -> Path:
    """switch_match_overrides.csv, next to CB_ALT_NAME.csv."""
    return CB_ALT_NAMES_DIR / "switch_match_overrides.csv"


def get_match_log_dir(batch: bool) -> Path:
    """Folder for the per-project match logs, created if missing."""
    base = get_protection_batch_output_dir() if batch else Path(OUTPUT_LOCAL_DIR)
    folder = Path(base) / MATCH_LOG_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    return folder
