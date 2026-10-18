"""
Protection-function check for NOJA controllers (pure: no PowerFactory calls).

A NOJA controller in the model either protects (at least one protection
element trips) or it does not: a fault-indication-only controller, or one
whose IPS trip map disables every element. The mapping files drive each
element's outserv flag from the IPS trip-1 map entry (R reclose, L lockout,
D disabled), so a controller with no protection function ends with every
protection element out of service. The relay itself used to stay in
service, so SPA listed it with blank pickups and skipped it in coordination
(issues log #10: 9 Brendale reclosers on 3 and 4 Oct; X5489-I_RC01ES and
X425694_RC01ES on 15 Oct).

A trip map that is absent from IPS (none of the values the mapping file
reads were found) gives the same picture for a different reason: the
mapping file does not fit the IPS export. That case is reported separately
so it reads as a mapping problem, not as a fault indicator.

relay_settings does the PowerFactory part: it collects the in-service
elements and places the relay out of service when classify() says so.
Offline tests: update_powerfactory/test_noja_function.py
"""

from typing import Any, Dict, List, Optional, Tuple

from config.relay_patterns import NOJA_RECLOSERS
from update_powerfactory.setting_utils import build_setting_key

# Result strings (UpdateResult.result).
NO_PROTECTION = (
    "No protection function (fault indication only) - placed out of service"
)
TRIP_MAP_MISSING = "Trip map not in IPS settings - placed out of service"

# Element functions SPA reads pickups from. Mirrors SPA
# relays/elements.get_prot_elements: in-service RelToc with one of the IDMT
# functions, in-service RelIoc with one of the instantaneous functions AND a
# reclose target. The NOJA "Minimum Current Multiplier" elements and the
# "0 Current Detector" are RelIoc without a reclose target, so they do not
# count (the 0 Current Detector's outserv row is a constant ON).
IDMT_FUNCTIONS = frozenset({"I>t", "IE>t", "I2>t"})
INST_FUNCTIONS = frozenset({"I>>", "IE>>", "I2>>"})

# Column D values that make an outserv row a constant, not an IPS read.
_CONSTANT_ROWS = frozenset({"ON", "On", "OFF", "Off", "None"})
# Rows consumed by sub-handlers, never by apply_settings.
_SUB_HANDLED = ("_logic", "_dip", "_Trips")


def is_noja(pattern: Optional[str]) -> bool:
    """True for a NOJA pattern, including swer_/switch_ prefixed forms."""
    return any(noja in (pattern or "") for noja in NOJA_RECLOSERS)


def trip_map_coverage(
    mapping_file: List[List[str]],
    setting_dict: Dict[str, Any],
) -> Tuple[int, int]:
    """
    (found, missing) over the mapping rows that set outserv from IPS.

    A row is 'found' when create_setting_dictionary stored a value for its
    key, 'missing' otherwise. Constant rows (ON/OFF) are not counted.
    """
    found = missing = 0
    for line in mapping_file:
        if len(line) < 4 or line[2] != "outserv" or line[3] in _CONSTANT_ROWS:
            continue
        if any(tag in line[1] for tag in _SUB_HANDLED):
            continue
        if build_setting_key(line) in setting_dict:
            found += 1
        else:
            missing += 1
    return found, missing


def is_protection_element(
    class_name: str,
    outserv: Any,
    function: Any,
    reclose_target: Any = None,
) -> bool:
    """True for an in-service element SPA would read a pickup from."""
    if outserv == 1:
        return False
    if class_name == "RelToc":
        return function in IDMT_FUNCTIONS
    if class_name == "RelIoc":
        return function in INST_FUNCTIONS and bool(reclose_target)
    return False


def classify(live_elements: int, found: int, missing: int) -> Optional[str]:
    """
    None when the controller protects; otherwise the result to report.

    Args:
        live_elements: in-service protection elements after the settings
            were applied (is_protection_element over the relay's elements)
        found, missing: trip_map_coverage() for the relay
    """
    if live_elements:
        return None
    if missing and not found:
        return TRIP_MAP_MISSING
    return NO_PROTECTION
