"""
Pickup-coverage check for every relay pattern (pure: no PowerFactory calls).

A relay whose mapping file has pickup rows (Ipset / Ipsetr read from IPS)
but none of them found in its IPS settings gets no pickup from IPS: its
elements keep whatever the model held (200 A / 40 A on every in-service
switch controller in Burleigh Heads, 28 Oct), and SPA grades those values.
The SEQ fleet diagnostic of 31 Oct (diag_pickup_coverage.py, 37 projects)
found 411 such relays in service. Two different causes:

* Rule A - no protection settings in IPS. The IPS record holds detection,
  communications or nothing protection-related, so the device does not
  protect. Recognised when either
    - the IPS device ID marks a switch or sectionaliser (SW..., SE...), or
    - the pattern's mapping file is proven to read pickups on other relays
      (NO_PICKUP_MEANS_NO_PROTECTION), so finding none means IPS has none.
  The relay is placed out of service (214 relays on 31 Oct, e.g. the
  detection-only ADVC on X15402-C_SW780; 39-row stub records on
  X1418180_RE1119 and X1628-D_RE922).

* Rule B - the mapping file does not read this IPS record (WSOS4_Energex,
  CAPM2, ADVC2 WSOS5 V5.13.26, SEL351 ...: no relay on these patterns gets
  a pickup). The relay is a real protection device; it stays in service
  and is reported so the mapping file can be fixed (197 relays on 31 Oct).

relay_settings does the PowerFactory part.
Offline tests: update_powerfactory/test_pickup_coverage.py
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from update_powerfactory.setting_utils import build_setting_key

# Result strings (UpdateResult.result).
NO_PROTECTION_SETTINGS = (
    "No protection settings in IPS (detection only) - placed out of service"
)
PICKUPS_NOT_IN_IPS = "No pickups found in IPS - model settings kept"

# PF attributes that hold a pickup. Mirrors relay_settings.PICKUP_ATTRIBUTES.
PICKUP_ATTRIBUTES = frozenset({"Ipset", "Ipsetr"})

# Column D values that make a mapping row a constant, not an IPS read.
_CONSTANT_ROWS = frozenset({"ON", "On", "OFF", "Off", "None"})

# Patterns whose mapping file reads pickups on other relays in the fleet
# (31 Oct diagnostic: EQL_ADVC3 150 relays, ADVC PTCC 26, WSOS5_Energex 16).
# For these, an IPS record with no pickup has no protection settings.
# Add a pattern only once its mapping file is shown to read pickups.
NO_PICKUP_MEANS_NO_PROTECTION = frozenset({
    "EQL_ADVC3_ADVC2_5.16",
    "ADVC PTCC",
    "WSOS5_Energex",
})

# IPS device IDs of switches (SW780) and sectionalisers (SE2209).
_SWITCH_DEVICE_ID = re.compile(r"^(SW|SE)\d", re.IGNORECASE)


def base_pattern(pattern: Optional[str]) -> str:
    """IPS pattern without the swer_ prefix or surrounding spaces."""
    pattern = (pattern or "").strip()
    if pattern.startswith("swer_"):
        pattern = pattern[len("swer_"):]
    return pattern.strip()


def is_switch_device_id(device_id: Optional[str]) -> bool:
    """True for an IPS device ID of a switch or sectionaliser."""
    return bool(_SWITCH_DEVICE_ID.match((device_id or "").strip()))


def pickup_coverage(
    mapping_file: List[List[str]],
    setting_dict: Dict[str, Any],
) -> Tuple[int, int]:
    """
    (found, missing) over the mapping rows that set a pickup from IPS.

    A row is 'found' when create_setting_dictionary stored a value for its
    key (an IPS OFF counts as found), 'missing' otherwise. Constant rows
    are not counted.
    """
    found = missing = 0
    for line in mapping_file:
        if len(line) < 4 or line[2] not in PICKUP_ATTRIBUTES:
            continue
        if line[3] in _CONSTANT_ROWS:
            continue
        if build_setting_key(line) in setting_dict:
            found += 1
        else:
            missing += 1
    return found, missing


def classify(
    found: int,
    missing: int,
    pattern: Optional[str],
    device_id: Optional[str],
) -> Optional[str]:
    """
    None when IPS gives the relay at least one pickup (or the mapping reads
    none); otherwise NO_PROTECTION_SETTINGS (rule A) or PICKUPS_NOT_IN_IPS
    (rule B).
    """
    if found or not missing:
        return None
    if is_switch_device_id(device_id):
        return NO_PROTECTION_SETTINGS
    if base_pattern(pattern) in NO_PICKUP_MEANS_NO_PROTECTION:
        return NO_PROTECTION_SETTINGS
    return PICKUPS_NOT_IN_IPS
