"""
Pickup-coverage check for every relay pattern (pure: no PowerFactory calls).

A relay whose mapping file has pickup rows (Ipset / Ipsetr read from IPS)
but none of them found in its IPS settings gets no pickup from IPS: its
elements keep whatever the model held, and SPA grades those values.
Two different causes:

* Rule A - no protection settings in IPS. The IPS record holds detection,
  communications or nothing protection-related, so the device does not
  protect. Recognised when either
    - the IPS device ID marks a switch or sectionaliser (SW..., SE...), or
    - the pattern's mapping file is proven to read pickups on other relays
      (NO_PICKUP_MEANS_NO_PROTECTION), so finding none means IPS has none.
  The relay is placed out of service.

* Rule B - the mapping file does not read this IPS record (WSOS4_Energex,
  CAPM2, ADVC2 WSOS5 V5.13.26, SEL351 ...: no relay on these patterns gets
  a pickup). The relay is a real protection device; it stays in service
  and is reported so the mapping file can be fixed.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from update_powerfactory.setting_utils import build_setting_key

# Result strings (UpdateResult.result).
NO_PROTECTION_SETTINGS = "No protection settings in IPS - placed out of service"
PICKUPS_NOT_IN_IPS = "No pickups found in IPS - model settings kept"

# PF attributes that hold a pickup. Mirrors relay_settings.PICKUP_ATTRIBUTES.
PICKUP_ATTRIBUTES = frozenset({"Ipset", "Ipsetr"})

# Column D values that make a mapping row a constant, not an IPS read.
_CONSTANT_ROWS = frozenset({"ON", "On", "OFF", "Off", "None"})

# Patterns whose mapping file reads pickups on other relays in the fleet.
# For these, an IPS record with no pickup has no protection settings.
# Add a pattern only once its mapping file is shown to read pickups.
NO_PICKUP_MEANS_NO_PROTECTION = frozenset({
    "EQL_ADVC3_ADVC2_5.16",
    "ADVC PTCC",
    "WSOS5_Energex",
    "WSOS4_Energex",
    "CAPM2",
    "CAPM2_Energex",
    "ADVC2 WSOS5 V5.13.26",
    "ARGUS1 Compatibility_Energex",
    "ARGUS1 ReyDisp Evolution v6.4t.3.4_Energex",
    "P123 V13 V14 HEX",
    "P123_Energex",
    "P142 430J 43?J 440J 460J 46?J 468J 469J 500J_obsolete",
    "P142 090B 100B 150A 150C 170C 210G 300J",
    "RED615 2.6 - 2.12",
    "EQL_RC10_RC20",
    "SEL351-6 SEL5010_Energex",
    "SEL351A - SEL-5030_Energex",
})

# Unit protection (pilot wire / line differential relays). Their
# differential function does not take part in overcurrent grading; one with
# no overcurrent element left in service after its IPS settings are applied
# protects nothing SPA can assess and is placed out of service. One with an
# overcurrent element in service stays in.
UNIT_PROTECTION_PATTERNS = frozenset({
    "SOLKOR-RF_Energex",
    "7PG21 (SOLKOR-RF)",
    "SOLKOR-N_Energex",
    "7SG18 (SOLKOR-N)",
})

UNIT_PROTECTION_ONLY = "Unit protection only - placed out of service"

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
    are not counted. A row reading an IPS parameter that has no block path
    has 'None' in column D (ADVC2 WSOS5 V5.13.26: IPS holds no block paths
    for that pattern); the 'use_setting' marker makes it an IPS read.
    """
    found = missing = 0
    for line in mapping_file:
        if len(line) < 4 or line[2] not in PICKUP_ATTRIBUTES:
            continue
        if line[3] in _CONSTANT_ROWS and "use_setting" not in line[4:]:
            continue
        if build_setting_key(line) in setting_dict:
            found += 1
        else:
            missing += 1
    return found, missing


def is_unit_protection(pattern: Optional[str]) -> bool:
    """True for a pilot-wire / line-differential pattern."""
    return base_pattern(pattern) in UNIT_PROTECTION_PATTERNS


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
