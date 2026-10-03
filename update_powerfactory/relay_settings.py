"""
Relay settings configuration for PowerFactory.

This module is the main entry point for configuring relay devices in
PowerFactory using settings from the IPS database. It orchestrates:
- Device function determination (SWER/switch/sectionaliser)
- Mapping file lookup
- Relay type validation and update
- Phase determination for single-phase relays
- Setting application
- CT/VT updates

Sub-modules handle specialized functionality:
- relay_reclosing: Reclosing logic configuration
- relay_logic_elements: Dip switch configuration
- setting_utils: Shared utility functions

Performance optimizations:
- Uses RelayTypeIndex for O(1) relay type lookups.
- Mapping file results are cached in mapping_file.py

Usage:
    from update_powerfactory import relay_settings as rs

    result, updates = rs.relay_settings(app, device_object, relay_index, updates)
"""

import logging
import math
from typing import Dict, List, Tuple, Optional, Any, Union

from update_powerfactory import mapping_file as mf
from update_powerfactory import ct_settings as cs
from update_powerfactory import vt_settings as vs
from update_powerfactory.type_index import RelayTypeIndex
from update_powerfactory.setting_utils import (
    build_setting_key,
    determine_on_off,
    convert_binary,
    setting_adjustment,
)
from update_powerfactory.relay_reclosing import update_reclosing_logic
from update_powerfactory.relay_logic_elements import update_logic_elements
from core import UpdateResult
from config.relay_patterns import SINGLE_PHASE_RELAYS, MULTI_PHASE_RELAYS

logger = logging.getLogger(__name__)


# =============================================================================
# Phase Determination Constants
# =============================================================================

# Phase mapping based on name suffix patterns
PHASE_SUFFIX_MAP: Dict[str, int] = {
    "_A": 0, "A-A": 0, "-A": 0, "-R": 0,  # Phase A
    "_B": 1, "B-B": 1, "-B": 1, "-W": 1,  # Phase B
    "C-C": 2, "-C": 2, "_C": 2,           # Phase C
}

# Patterns indicating earth fault relays
EARTH_FAULT_PATTERNS: Tuple[str, ...] = (
    "N-E", "N", "EF-E", "E-E", "DEF", "-EF"
)

# Pickup attributes. An IPS value of OFF/Disabled on one of these means the
# protection element is disabled, which PowerFactory represents with the
# element's outserv flag. Writing a placeholder pickup (the old 9999
# fallback) left the element in service as far as studies were concerned.
PICKUP_ATTRIBUTES = frozenset({"Ipset", "Ipsetr"})

# Limit attributes where an IPS value of OFF means "no limit", not "element
# disabled". The value is what is written to PowerFactory for no limit
# (9999 is what the conversion fallback has always written for these).
OFF_MEANS_NO_LIMIT: Dict[str, float] = {"udeftmax": 180}

_OFF_TOKENS = frozenset({
    "off", "disabled", "disable",
    # An infinite pickup is IPS's way of writing "stage disabled" on
    # electromechanical relays (Ergon_RI, MCGG22, RXIDF instantaneous
    # elements). PowerFactory rejects inf, so it is treated as OFF.
    "inf", "+inf", "infinity", "+infinity", "\u221e",
})


# add_relay_skeletons.DATA_SOURCE_STRING (not imported: avoids pulling the
# ips_data import chain into update_powerfactory). Relays created by the
# skeleton pass carry this in dat_src and are created out of service.
SKELETON_DATA_SOURCE = "PRS"


class SettingRejectedError(RuntimeError):
    """PowerFactory refused a converted setting value (usually out of range)."""


def is_off_value(value: Any) -> bool:
    """True when an IPS setting value means 'element disabled'."""
    if isinstance(value, float):
        return math.isinf(value) and value > 0
    return isinstance(value, str) and value.strip().lower() in _OFF_TOKENS


_FLAG_ON = frozenset({"on", "enabled", "enable", "yes", "true"})
_FLAG_OFF = frozenset({"off", "disabled", "disable", "no", "false"})


def _flag_value(value: Any, existing: Any) -> Optional[int]:
    """1/0 for on/off text when the PF attribute is an integer, else None."""
    if not isinstance(existing, int) or isinstance(existing, bool):
        return None
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    if token in _FLAG_ON:
        return 1
    if token in _FLAG_OFF:
        return 0
    return None


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


# =============================================================================
# Main Entry Point
# =============================================================================

def relay_settings(
    app,
    device_object: Any,
    relay_index: Union[RelayTypeIndex, List],
    updates: bool,
    ct_library: Optional[Any] = None,
    vt_library: Optional[Any] = None,
) -> Tuple[UpdateResult, bool]:
    """
    Configure a relay device with settings from IPS.

    This is the main entry point for relay configuration. It handles:
    1. Device function determination (SWER/switch/sectionaliser)
    2. Mapping file lookup
    3. Relay type validation and update
    4. Phase determination for single-phase relays
    5. Setting application
    6. Reclosing logic configuration
    7. CT/VT updates

    Args:
        app: PowerFactory application object
        device_object: The ProtectionDevice to configure
        relay_index: RelayTypeIndex for O(1) lookups, or list for backward
            compatibility
        updates: Current updates flag

    Returns:
        Tuple of (UpdateResult, updated updates flag)
    """
    # Classify device as SWER/switch/sectionaliser if applicable
    update_device_function(device_object)

    # Create result object from device
    result = UpdateResult.from_device(device_object)
    result.relay_pattern = device_object.device
    result.used_pattern = device_object.device

    # Switch- and sectionaliser-classified devices are not modelled as
    # protection relays: no mapping lookup, no type assignment, no
    # settings or CT/VT updates. The PF element already exists; place
    # it out of service and report informationally (not as an error).
    if device_object.device.startswith(("switch_", "sect_")):
        kind = (
            "Sectionaliser" if device_object.device.startswith("sect_")
            else "Switch"
        )
        device_object.pf_obj.SetAttribute("outserv", 1)
        result.result = f"{kind} - placed out of service"
        logger.info(
            f"{device_object.name}: classified as {kind.lower()} "
            f"({device_object.device}); element out of service, "
            f"no settings applied"
        )
        return result, updates

    # Classify single-pole relays BEFORE the mapping lookup. determine_phase
    # appends "_Earth" to the pattern for earth-fault relays, and
    # type_mapping.csv has separate "<pattern>_Earth" rows (e.g.
    # MCGG22_Earth -> "MCGG-22 Earth"). Classifying after the lookup meant
    # every earth-fault single-pole relay was typed and set from the PHASE
    # row (Gladstone 2026-10-03: FILASS-...-EF-E, ICIZSS-...-EF-E,
    # MOURSS-...-N-E typed MCGG-2x_Phase). Pure name logic - no PF writes
    # move, the RelMeasure iphase write still follows check_relay_type.
    phase = determine_phase(app, device_object)

    # Load mapping file for this relay pattern. CT secondary selects the
    # correct PowerFactory model for patterns that are CT-dependent.
    mapping_file, mapping_type = mf.read_mapping_file(
        app, device_object.device, device_object.pf_obj,
        device_object.ct_secondary
    )

    # Validate and update relay type if needed
    result = check_relay_type(
        app, device_object, mapping_type, relay_index, result
    )

    # Configure phase for single-phase relays
    if phase is not None:
        meas_elems = device_object.pf_obj.GetContents("*.RelMeasure")
        if meas_elems:
            meas_elems[0].SetAttribute("e:iphase", phase)
        else:
            # No measurement element to phase-configure. This normally means
            # the relay type wasn't assigned (e.g. check_relay_type recorded
            # "Type not found" and set outserv=1), so the relay has no
            # RelMeasure children.
            logger.warning(
                f"No RelMeasure element on '{device_object.name}' "
                f"(pattern {device_object.device}); phase {phase} not applied"
            )
            if not result.result:
                result.result = "No measurement element for phase assignment"

    # Build setting dictionary and apply settings
    if mapping_file:
        setting_dict = create_setting_dictionary(
            app, device_object.settings, mapping_file, device_object.pf_obj
        )
        result.date_setting = device_object.date
        device_object.pf_obj.SetAttribute("e:sernum", str(device_object.date))
    else:
        result.result = "Mapping file not found"
        device_object.pf_obj.SetAttribute("outserv", 1)
        return result, updates

    # Apply settings from mapping file
    updates = apply_settings(app, device_object, mapping_file, setting_dict, updates)

    # Delegate specialized configuration to sub-modules
    reclose_status = update_reclosing_logic(
        app, device_object, mapping_file, setting_dict
    )
    if reclose_status and not result.result:
        result.result = reclose_status
    update_logic_elements(
        app, device_object.pf_obj, mapping_file, setting_dict,
        make_element_finder(device_object.pf_obj),
    )

    # Update CT and VT settings. The CT/VT library folders are resolved once
    # per run by the orchestrator and threaded through here; passing None falls
    # back to a per-call lookup (used by standalone callers and tests).
    result = cs.update_ct(app, device_object, result, ct_library)
    result = vs.update_vt(app, device_object, result, vt_library)

    _enable_configured_skeleton(device_object, result)

    return result, updates


def _enable_configured_skeleton(device_object: Any, result: Any) -> None:
    """
    Put a skeleton-created relay into service once it has been configured.

    Only relays the script itself took out of service (skeleton-tagged)
    are switched on, and only on a clean result: a relay with no type,
    or with any recorded problem, is left as it is. Relays that are out
    of service in the master model for other reasons are untouched.
    """
    pf_device = device_object.pf_obj
    try:
        if pf_device.GetAttribute("outserv") != 1:
            return
        if pf_device.GetAttribute("dat_src") != SKELETON_DATA_SOURCE:
            return
        if pf_device.typ_id is None:
            return
    except AttributeError:
        return
    if result.result:
        return

    pf_device.SetAttribute("outserv", 0)
    logger.info(
        f"{device_object.name}: skeleton relay configured from IPS; "
        f"placed in service"
    )

# =============================================================================
# Device Classification
# =============================================================================

def update_device_function(device_object: Any) -> None:
    """
    Determine whether the device is SWER, switch, or sectionaliser.

    Updates the device.device attribute with appropriate prefix:
    - "swer_" for single/two phase devices
    - "switch_" for devices without protection settings
    - "sect_" for sectionaliser devices

    Args:
        device_object: The ProtectionDevice to classify
    """
    # Determine whether the device is a SWER device
    try:
        num_of_phases = device_object.pf_obj.GetAttribute("r:fold_id:e:nphase")
    except AttributeError:
        num_of_phases = 3

    if num_of_phases < 3:
        device_object.device = f"swer_{device_object.device}"

    # Subtransmission relays are placed by element/cubicle and don't carry the
    # distribution SWER/switch/sectionaliser semantics. Classifying them here
    # would prefix the pattern (e.g. "switch_SOLKOR-RF_Energex"), which then
    # misses both type_mapping.csv (-> "Mapping file not found" -> relay OOS)
    # and the raw-name RELAYS_OOS check. Skip it for subtransmission devices.
    if getattr(device_object, "subtransmission", False):
        return

    # Determine whether the device is a switch or sectionaliser
    if not device_object.settings and device_object.device not in ["SOLKOR-RF_Energex"]:
        # No protection settings typically means it's a switch
        device_object.device = f"switch_{device_object.device}"
    else:
        # Check if the device is a sectionaliser
        for setting in device_object.settings:
            if setting[1] in ["Sectionaliser"]:
                if setting[2].lower() in ["on", "auto"]:
                    device_object.device = f"sect_{device_object.device}"
                break
            if setting[1] in ["Detection"]:
                if setting[2].lower() == "on":
                    device_object.device = f"switch_{device_object.device}"
                    break

# =============================================================================
# Relay Type Management
# =============================================================================

def check_relay_type(
    app,
    device_object: Any,
    mapping_type: Optional[str],
    relay_index: Union[RelayTypeIndex, List],
    result: UpdateResult
) -> UpdateResult:
    """
    Check that the relay type is correct and update if necessary.

    If the relay type doesn't match the mapping type, attempts to find
    and assign the correct type. If the type cannot be found, places
    the relay out of service.

    Args:
        app: PowerFactory application object
        device_object: The ProtectionDevice being configured
        mapping_type: Expected relay type from mapping file
        relay_index: RelayTypeIndex for O(1) lookups, or list
        result: UpdateResult to update

    Returns:
        Updated UpdateResult
    """
    if not mapping_type:
        return result

    pf_device = device_object.pf_obj

    try:
        current_type = pf_device.typ_id.loc_name
    except AttributeError:
        current_type = None

    if current_type == mapping_type:
        return result

    # Need to update the relay type
    new_type = _find_relay_type(relay_index, mapping_type)

    if new_type:
        pf_device.typ_id = new_type
        result.used_pattern = mapping_type
    else:
        # Cannot find the required type - put device out of service
        app.PrintWarn(
            f"Relay type '{mapping_type}' not found for {device_object.name}"
        )
        pf_device.SetAttribute("outserv", 1)
        result.result = f"Type not found: {mapping_type}"

    return result


def _find_relay_type(
    relay_index: Union[RelayTypeIndex, List],
    type_name: str
) -> Optional[Any]:
    """
    Find a relay type by name using indexed or linear lookup.

    Args:
        relay_index: RelayTypeIndex for O(1) lookups, or list for O(n)
        type_name: Name of the relay type to find

    Returns:
        The PowerFactory TypRelay object, or None if not found
    """
    # Use indexed lookup if available (O(1))
    if isinstance(relay_index, RelayTypeIndex):
        return relay_index.get(type_name)

    # Fall back to linear search (O(n))
    for relay_type in relay_index:
        if relay_type.loc_name == type_name:
            return relay_type

    return None


# =============================================================================
# Phase Determination
# =============================================================================

def determine_phase(app, device_object: Any) -> Optional[int]:
    """
    Determine the correct phase for single-phase relays.

    Single pole and phase specific relays need to be mapped correctly.
    This function analyzes the device name to determine the correct phase.

    Args:
        app: PowerFactory application object
        device_object: The ProtectionDevice to analyze

    Returns:
        Phase index (0=A, 1=B, 2=C), or None if not a single-phase relay
    """
    # Two phase relays have a unique type - don't assign phase
    if device_object.device in MULTI_PHASE_RELAYS:
        return None

    if device_object.device not in SINGLE_PHASE_RELAYS:
        return None

    # Energex devices carry the IPS asset name in seq_name; Ergon devices
    # have no seq_name and use the asset name in .name. Either may be None.
    name = getattr(device_object, "seq_name", None) or device_object.name or ""

    # Check for phase suffix in last 6 characters of name
    name_suffix = name[-6:]

    for suffix, phase in PHASE_SUFFIX_MAP.items():
        if suffix in name_suffix:
            return phase

    # Check if it is an Earth Fault relay
    is_earth = any(pattern in name_suffix for pattern in EARTH_FAULT_PATTERNS)
    # Check for trailing "E" indicating earth fault
    if not is_earth and name and name[-1] == "E":
        is_earth = True

    if is_earth:
        earth_pattern = f"{device_object.device}_Earth"
        if mf.get_type_mapping(earth_pattern) is None:
            # No "_Earth" row: keep the phase row rather than send the
            # relay to "Mapping file not found" (the pre-fix behaviour).
            logger.warning(
                f"{name}: earth-fault single-pole relay but type_mapping.csv "
                f"has no '{earth_pattern}' row; using the '{device_object.device}' "
                f"(phase) row"
            )
            return None
        device_object.device = earth_pattern
        return None

    # Default to Phase A if no match found
    logger.info(
        f"{name}: single-pole relay with no phase or earth marker in the "
        f"name; defaulting to phase A"
    )
    return 0


# =============================================================================
# Setting Dictionary Creation
# =============================================================================

def create_setting_dictionary(
    app,
    settings: List[List],
    mapping_file: List[List],
    pf_device: Any
) -> Dict[str, Any]:
    """
    Create a dictionary mapping PF attribute keys to IPS setting values.

    The key format is: "{folder}{element}{attribute}"

    This function processes the IPS settings through the mapping file
    to create a lookup dictionary for applying settings to PF elements.

    Args:
        app: PowerFactory application object
        settings: List of IPS setting rows
        mapping_file: List of mapping file rows
        pf_device: The PowerFactory device object

    Returns:
        Dictionary mapping attribute keys to setting values
    """
    setting_dictionary = {}
    collisions: Dict[str, List[str]] = {}
    sources: Dict[str, str] = {}

    for setting in settings:
        lines = mapping_file
        for i, value in enumerate(setting):
            prob_lines = []
            for line in lines:
                if line[3] in ["None", "ON", "On", "OFF", "Off"]:
                    # This setting is not required as part of this relay
                    if len(line) < 5:
                        continue

                # Determine the index of the associated setting reference
                # from the line in the mapping file
                index = i + 3  # Adjusted to start at Column D in mapping file

                if line[index] == "use_setting":
                    key = build_setting_key(line)
                    # Apply unit conversions into a local; never mutate the row.
                    # Non-numeric values (OFF, Disabled) pass through
                    # unconverted for set_attribute/apply_settings to handle.
                    unit = setting[-1]
                    converted = value
                    try:
                        if unit in ("mA", "ms"):
                            converted = float(value) / 1000
                        elif unit == "kA":
                            converted = float(value) * 1000
                    except (TypeError, ValueError):
                        converted = value
                    if key in setting_dictionary \
                            and setting_dictionary[key] != converted:
                        seen = collisions.setdefault(
                            key, [f"{setting_dictionary[key]!r} <- {sources[key]}"]
                        )
                        seen.append(f"{converted!r} <- {setting[0]}{setting[1]}")
                    setting_dictionary[key] = converted
                    sources[key] = f"{setting[0]}{setting[1]}"
                    continue
                elif (
                    str(line[index]) != str(value)
                    and "0{}".format(line[index]) != value
                ):
                    # Setting address might drop leading zero
                    continue
                else:
                    # Multiple lines may have similar values until full key determined
                    prob_lines.append(line)

            if not prob_lines:
                break
            lines = prob_lines

    if collisions:
        # Two different IPS settings resolved to the same PF attribute
        # (e.g. one mapping row per setting group); the last one read wins.
        # Values in the order read (first = value first stored, then each
        # overriding value with the IPS block path + parameter it came from),
        # so the mapping row at fault can be found without an IPS export.
        logger.warning(
            "%s: %d PF attribute(s) mapped from more than one IPS setting "
            "with different values; last value used: %s",
            getattr(pf_device, "loc_name", "?"), len(collisions),
            "; ".join(
                f"{key}: {values}" for key, values in
                sorted(collisions.items())[:10]
            ),
        )

    return setting_dictionary


# =============================================================================
# Setting Application
# =============================================================================

def apply_settings(
    app,
    device_object: Any,
    mapping_file: List[List],
    setting_dict: Dict[str, Any],
    updates: bool
) -> bool:
    """
    Apply settings from the mapping file to the relay.

    Iterates through the mapping file and applies each setting
    to the appropriate PowerFactory element.

    Args:
        app: PowerFactory application object
        device_object: The ProtectionDevice being configured
        mapping_file: List of mapping file rows
        setting_dict: Dictionary of setting values
        updates: Current updates flag

    Returns:
        Updated updates flag
    """
    pf_device = device_object.pf_obj

    # Elements whose in-service state is driven by a mapping 'outserv' row.
    # Their state is left entirely to that row when re-enabling.
    outserv_mapped = {
        (line[0], line[1]) for line in mapping_file if line[2] == "outserv"
    }
    # (folder, element) -> PF element, for pickups that IPS says are OFF
    # and pickups that carry a real value in this setting file.
    disabled_elements: Dict[Tuple[str, str], Any] = {}
    enabled_elements: Dict[Tuple[str, str], Any] = {}
    find = make_element_finder(pf_device)

    for mapped_set in mapping_file:
        # Skip logic elements (handled by sub-modules)
        if (
            "_logic" in mapped_set[1]
            or "_dip" in mapped_set[1]
            or "_Trips" in mapped_set[1]
        ):
            continue

        # Get the PowerFactory object for the setting
        element = find(app, pf_device, mapped_set)
        if not element:
            app.PrintError(f"Unable to find an element for {mapped_set}")
            continue

        key = build_setting_key(mapped_set)
        try:
            setting = setting_dict[key]
        except KeyError:
            # Handle outserv attributes
            if mapped_set[2] == "outserv":
                setting = None
            else:
                continue

        if mapped_set[2] in PICKUP_ATTRIBUTES:
            element_key = (mapped_set[0], mapped_set[1])
            if is_off_value(setting):
                # Element disabled in IPS: no pickup to write. Taken out
                # of service after the loop so a later outserv row in the
                # mapping cannot switch it back on.
                disabled_elements[element_key] = element
                continue
            if _is_number(setting):
                enabled_elements[element_key] = element

        attribute = f"e:{mapped_set[2]}"
        updates = set_attribute(
            app,
            mapped_set,
            setting,
            element,
            attribute,
            device_object,
            setting_dict,
            updates,
        )

    updates = _apply_pickup_enable_state(
        pf_device, disabled_elements, enabled_elements, outserv_mapped,
        updates,
    )

    return updates


def _apply_pickup_enable_state(
    pf_device: Any,
    disabled_elements: Dict[Tuple[str, str], Any],
    enabled_elements: Dict[Tuple[str, str], Any],
    outserv_mapped: set,
    updates: bool,
) -> bool:
    """
    Reflect IPS OFF/Disabled pickups in the elements' outserv flag.

    - Pickup OFF in IPS -> element out of service.
    - Pickup has a real value but the element is out of service and no
      mapping 'outserv' row owns its state -> logged only. The element may
      be disabled by a dip/logic row or deliberately in the master model,
      and IPS often keeps a numeric pickup on a disabled stage, so it is
      not switched back on automatically.
    """
    device_name = pf_device.loc_name

    newly_disabled = []
    for element_key, element in disabled_elements.items():
        if element.GetAttribute("outserv") != 1:
            element.SetAttribute("outserv", 1)
            updates = True
            newly_disabled.append("/".join(k for k in element_key if k))
    # One line per relay rather than one per element: multi-trip relays map
    # the same element in every trip folder, which repeated the same line
    # four times per recloser.
    if newly_disabled:
        logger.info(
            "%s: pickup OFF in IPS; set out of service: %s",
            device_name, ", ".join(newly_disabled),
        )
    if len(disabled_elements) > len(newly_disabled):
        logger.debug(
            "%s: %d element(s) with pickup OFF in IPS already out of service",
            device_name, len(disabled_elements) - len(newly_disabled),
        )

    for element_key, element in enabled_elements.items():
        if element_key in disabled_elements or element_key in outserv_mapped:
            continue
        if element.GetAttribute("outserv") == 1:
            logger.info(
                "%s: %s is out of service but has a pickup in IPS; "
                "left out of service (review)",
                device_name, element.loc_name,
            )

    return updates


def make_element_finder(pf_device: Any):
    """
    A find_element() for one relay, backed by a (folder, name) index.

    find_element() runs a recursive GetContents per mapping row - a few
    hundred per relay. The index is one recursive GetContents per relay;
    a miss (wildcard or case differences that GetContents tolerates) falls
    back to find_element(), so results are unchanged.
    """
    index: Dict[Tuple[str, str], Any] = {}
    try:
        for obj in pf_device.GetContents("*", True):
            try:
                key = (obj.fold_id.loc_name, obj.loc_name)
            except AttributeError:
                continue
            index.setdefault(key, obj)
    except Exception:  # PF raises bare errors on odd objects; fall back
        index = {}

    def find(app, pf_object: Any, line: List) -> Optional[Any]:
        if pf_object is pf_device:
            hit = index.get((line[0], line[1]))
            if hit is not None:
                return hit
        return find_element(app, pf_object, line)

    return find


def find_element(app, pf_object: Any, line: List) -> Optional[Any]:
    """
    Find the PowerFactory element for a setting.

    The line contains the folder name and element name that identify
    where the setting should be applied.

    Args:
        app: PowerFactory application object
        pf_object: The parent PowerFactory object to search
        line: Mapping file line [folder, element, attribute, ...]

    Returns:
        The PowerFactory element object, or None if not found
    """
    obj_contents = pf_object.GetContents(line[1], True)

    if not obj_contents:
        # GetContents may not work due to object naming
        # Fall back to manual search
        obj_contents = pf_object.GetContents()
        if not obj_contents:
            return None

        for obj in obj_contents:
            if obj.fold_id.loc_name == line[0] and obj.loc_name == line[1]:
                return obj

        # Recursive search
        for obj in obj_contents:
            found = find_element(app, obj, line)
            if found:
                return found

        return None
    else:
        for obj in obj_contents:
            if obj.fold_id.loc_name == line[0]:
                return obj
        return None


def set_attribute(
    app,
    line: List,
    setting_value: Any,
    element: Any,
    attribute: str,
    device_object: Any,
    setting_dictionary: Dict[str, Any],
    updates: bool
) -> bool:
    """
    Set a single attribute on a PowerFactory element.

    Handles special cases for curves, out-of-service flags, and
    various data type conversions.

    Args:
        app: PowerFactory application object
        line: Mapping file line
        setting_value: The value to set
        element: The PowerFactory element
        attribute: The attribute name (e.g., "e:Ipset")
        device_object: The ProtectionDevice
        setting_dictionary: Dictionary of all settings
        updates: Current updates flag

    Returns:
        Updated updates flag
    """
    if line[2] == "pcharac":
        # Curve setting requires a PF object
        if line[-1] == "binary":
            setting_value = convert_binary(app, setting_value, line)
        setting_value = mf.get_pf_curve(app, setting_value, element)
        existing_setting = element.GetAttribute(attribute)
        if setting_value != existing_setting:
            element.SetAttribute(attribute, setting_value)
        return True

    elif line[2] == "outserv":
        # Out of service setting requires special handling
        if line[-1] == "binary":
            setting_value = convert_binary(app, setting_value, line)
            if setting_value == "1":
                setting_value = "OFF"
                line[-1] = "OFF"
            else:
                setting_value = "ON"
                line[-1] = "NF"
        setting_value = determine_on_off(app, setting_value, line[-1])
        element.SetAttribute(attribute, setting_value)
        return updates

    if line[6] == "None":
        # Setting can be directly applied without adjustment
        existing_setting = element.GetAttribute(attribute)
        try:
            if setting_value != existing_setting:
                _set_or_reject(element, attribute, setting_value, device_object)
                return True
            return updates
        except TypeError:
            # The IPS value arrived as a string (or the wrong numeric type)
            # for a numeric attribute: convert and retry below.
            pass

        if line[2] in OFF_MEANS_NO_LIMIT and is_off_value(setting_value):
            # OFF on a limit attribute is a known value meaning "no limit":
            # write the no-limit value without logging a conversion failure.
            no_limit = OFF_MEANS_NO_LIMIT[line[2]]
            if existing_setting != no_limit:
                _set_or_reject(element, attribute, no_limit, device_object)
                return True
            return updates

        flag = _flag_value(setting_value, existing_setting)
        if flag is not None:
            # 'on'/'off' text for an integer (switch/enable) attribute.
            # This used to fall through to the 9999 fallback below, which
            # for an 'off' wrote a non-zero (i.e. ON) value: SMF3A+B_J50
            # e:ModFrame on NSPTOC1/PHLPTOC1, Brendale 2026-10-03.
            if flag != existing_setting:
                _set_or_reject(element, attribute, flag, device_object)
                return True
            return updates

        try:
            numeric = float(setting_value)
        except (TypeError, ValueError):
            # Not a number at all (e.g. OFF on a time attribute). Pickup
            # OFF values never get here; apply_settings routes them to the
            # element's outserv flag.
            device_name = device_object.pf_obj.loc_name
            logger.warning(
                "%s set_attribute: could not convert %r for %s on %s; "
                "writing fallback 9999",
                device_name,
                setting_value,
                attribute,
                getattr(element, "loc_name", "?"),
            )
            _set_or_reject(element, attribute, 9999, device_object)
            return updates

        try:
            if numeric != round(existing_setting, 3):
                _set_or_reject(element, attribute, numeric, device_object)
                return True
            return updates
        except TypeError:
            # Integer-typed attribute: PowerFactory rejects a float.
            as_int = int(numeric)
            if as_int != existing_setting:
                _set_or_reject(element, attribute, as_int, device_object)
                return True
            return updates
    else:
        # Setting needs adjustment based on mapping file
        setting_value = setting_adjustment(app, line, setting_dictionary, device_object)
        if setting_value is None:
            # 'is None', not 'not': an adjusted value of 0 is a real setting
            # (determine_on_off's 0, a 0 s delay, an 'x - n' that nets 0).
            # The falsy test dropped them and left the old value in place.
            return updates
        existing_setting = element.GetAttribute(attribute)
        try:
            if setting_value != existing_setting:
                _set_or_reject(element, attribute, setting_value, device_object)
                return True
        except TypeError:
            setting_value = int(setting_value)
            if setting_value != existing_setting:
                _set_or_reject(element, attribute, setting_value, device_object)
                return True

    return updates


def _set_or_reject(
    element: Any,
    attribute: str,
    value: Any,
    device_object: Any,
) -> None:
    """
    SetAttribute, turning PowerFactory's refusal into a readable error.

    PowerFactory raises AttributeError ("setting attribute 'e:Ipset' of
    'DataObject' object failed") when a value of the right type is outside
    the attribute's allowed range. That surfaced as a chained traceback with
    no value in it. TypeError (wrong type) is left to propagate: callers use
    it to retry with a converted value.
    """
    try:
        element.SetAttribute(attribute, value)
    except AttributeError:
        raise SettingRejectedError(
            f"{attribute}={value!r} rejected by PowerFactory on "
            f"{getattr(element, 'loc_name', '?')} (out of range for this "
            f"relay type?); CT {device_object.ct_primary}/"
            f"{device_object.ct_secondary}"
        ) from None