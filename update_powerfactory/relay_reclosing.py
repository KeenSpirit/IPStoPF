"""
Reclosing logic configuration for relay devices.

This module handles the configuration of reclosing elements in PowerFactory
relays. Reclosing elements control automatic reclosure sequences after
fault trips.

The reclosing logic is configured through a logic table that specifies
the behavior for each trip number (1st trip, 2nd trip, etc.) and
protection element (OC1+, OC2+, etc.).

Logic table values:
- 0.0: No action (disabled)
- 1.0: Reclose (continue reclosing sequence)
- 2.0: Lockout (stop reclosing sequence)

This module was extracted from relay_settings.py to:
- Isolate complex reclosing logic
- Improve maintainability
- Enable independent testing

Usage:
    from update_powerfactory.relay_reclosing import update_reclosing_logic

    update_reclosing_logic(app, device_object, mapping_file, setting_dict)
"""

import re
import logging
from typing import Any, Dict, List, Optional

from update_powerfactory.setting_utils import build_setting_key, setting_adjustment
from config.relay_patterns import NOJA_RECLOSERS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NOJA per-trip reclose sequence
# ---------------------------------------------------------------------------

# IPS reclose codes -> PowerFactory ilogic values. The mapping is 1:1.
NOJA_TRIP_CODES: Dict[str, float] = {
    "r": 1.0,   # reclose
    "l": 2.0,   # trip to lockout
    "d": 0.0,   # element disabled for this trip
}

# RelRecl carries recltime1..recltime5 and hctrip1..hctrip5, so five trips is
# the ceiling regardless of what IPS supplies (currently four).
_MAX_RECLOSE_TRIPS = 5

_TRIP_SUFFIX = re.compile(r"^(.*?)(\d+)\s*$")


def _ips_setting_index(device_object: Any) -> Dict[tuple, str]:
    """{(blockpath, paramname): value} over the device's raw IPS settings."""
    index = {}
    for setting in getattr(device_object, "settings", None) or []:
        if len(setting) >= 3:
            index[(setting[0], setting[1])] = setting[2]
    return index


def _noja_trip_sequences(
    mapping_file: List[List],
    device_object: Any,
) -> Dict[str, List[float]]:
    """
    Read the full per-trip reclose sequence for each reclose block.

    Each surviving ``_logic`` row addresses trip 1 of one block: column D
    the IPS block path, column E the parameter name, e.g.
    ``AR OCEF map: OC1+, Trip 1``. The trailing index is incremented to
    walk the rest of the sequence, so one mapping row per block yields a
    whole row of the ilogic table.

    Returns {blockid: [float, ...]}, or {} when the mapping file does not
    use trip-indexed parameter names (the CMS files) -- the signal to fall
    back to the legacy single-value path.
    """
    index = _ips_setting_index(device_object)
    if not index:
        return {}

    device_name = device_object.pf_obj.loc_name
    sequences: Dict[str, List[float]] = {}

    for mapped_set in mapping_file:
        if "_logic" not in mapped_set[1] or len(mapped_set) < 5:
            continue

        blockid = mapped_set[2]
        blockpath = mapped_set[3]
        match = _TRIP_SUFFIX.match(str(mapped_set[4]))
        if not match:
            continue

        stem, first_trip = match.group(1), int(match.group(2))

        values = []
        for trip in range(first_trip, first_trip + _MAX_RECLOSE_TRIPS):
            raw = index.get((blockpath, "{}{}".format(stem, trip)))
            if raw is None:
                break
            code = NOJA_TRIP_CODES.get(str(raw).strip().lower())
            if code is None:
                logger.warning(
                    " %s reclose sequence: unrecognised value %r for %s "
                    "trip %s; treated as disabled",
                    device_name, raw, blockid, trip
                )
                code = 0.0
            values.append(code)

        if values:
            sequences[blockid] = values

    # A single resolved trip per block is what the legacy path already
    # handles; only claim this path when a real sequence exists.
    if not any(len(v) > 1 for v in sequences.values()):
        return {}

    return sequences


def _noja_sequence_length(sequences: Dict[str, List[float]]) -> int:
    """
    Trips-to-lockout implied by the sequences.

    A block locks out at the first trip carrying 2.0; the recloser's
    sequence runs as long as the longest-running block. Blocks that never
    trip (all 0.0) do not contribute. Floored at 1.
    """
    lengths = [1]

    for values in sequences.values():
        if not any(values):
            continue
        for i, value in enumerate(values):
            if value == 2.0:
                lengths.append(i + 1)
                break
        else:
            # Never locks out within the supplied trips; the sequence has
            # to end somewhere, so take its full length.
            lengths.append(len(values))

    return max(lengths)


def _noja_logic_rows(
    sequences: Dict[str, List[float]],
    op_to_lockout: int,
) -> Dict[str, List[float]]:
    """Truncate or pad each sequence to op_to_lockout columns."""
    row_dict = {}

    for blockid, values in sequences.items():
        row = (list(values) + [0.0] * op_to_lockout)[:op_to_lockout]
        # The table must not ask PF to reclose past the final trip.
        if any(row) and row[-1] == 1.0:
            row[-1] = 2.0
        row_dict[blockid] = row

    return row_dict


def update_reclosing_logic(
    app,
    device_object: Any,
    mapping_file: List[List],
    setting_dictionary: Dict[str, Any]
) -> None:
    pf_device = device_object.pf_obj
    device_type = device_object.device

    element = _find_reclosing_element(app, pf_device, mapping_file)
    if not element:
        logger.warning(
            " %s no RelRecl resolved from the mapping file; reclose logic "
            "table left at default (all disabled)", pf_device.loc_name
        )
        return

    trip_setting = get_trip_num(app, mapping_file, setting_dictionary)

    # Preferred NOJA path. The IPS reclose map carries an explicit R/L/D per
    # element per trip, which is precisely the PF ilogic table -- both the
    # sequence length and every cell come from it, so neither
    # _noja_trips_to_lockout nor get_trip_num is consulted. Falls through to
    # the legacy path for mapping files that do not carry a trip-indexed map.
    if _is_noja_recloser(device_type):
        sequences = _noja_trip_sequences(mapping_file, device_object)
        if sequences:
            op_to_lockout = _noja_sequence_length(sequences)
            row_dict = _noja_logic_rows(sequences, op_to_lockout)
            element.SetAttribute("e:oplockout", op_to_lockout)
            logger.info(
                " %s NOJA recloser: %s trips to lockout from the per-trip "
                "IPS reclose map (%s of %s blocks resolved)",
                pf_device.loc_name, op_to_lockout,
                len(sequences), len(sequences)
            )
            _apply_logic_to_element(
                element, row_dict, op_to_lockout, pf_device.loc_name
            )
            return

    # NOJA oplockout has two possible sources and neither is reliable alone:
    #   * _TripstoLockout rows (via get_trip_num) -- present in
    #     RC01ES_Energex_to_Noja Recloser.csv, absent from the CMS files.
    #   * numeric _logic rows (_noja_trips_to_lockout) -- the reverse.
    # Both floor at 1, so taking the larger picks up whichever the mapping
    # file actually carries. op_to_lockout sizes every row of the table, so
    # it must be settled before _build_logic_rows runs.
    if _is_noja_recloser(device_type):
        logic_trips = _noja_trips_to_lockout(mapping_file, setting_dictionary)
        op_to_lockout = max(logic_trips, trip_setting)
        element.SetAttribute("e:oplockout", op_to_lockout)
        logger.info(
            " %s NOJA recloser: oplockout = %s "
            "(_logic rows gave %s, _TripstoLockout rows gave %s)",
            pf_device.loc_name, op_to_lockout, logic_trips, trip_setting
        )
    else:
        op_to_lockout = element.GetAttribute("e:oplockout")

    if not op_to_lockout or op_to_lockout < 1:
        logger.warning(
            " %s reclosing element oplockout is %r; reclose logic table "
            "left at default (all disabled)", pf_device.loc_name, op_to_lockout
        )
        return

    row_dict = _build_logic_rows(
        app, mapping_file, setting_dictionary,
        device_object, op_to_lockout, trip_setting
    )

    _apply_logic_to_element(element, row_dict, op_to_lockout, pf_device.loc_name)


def _noja_trips_to_lockout(
    mapping_file: List[List],
    setting_dictionary: Dict[str, Any],
) -> int:
    """
    Derive trips-to-lockout for a NOJA recloser from its _logic rows.

    The trip count is carried by the numeric rows (OC/EF "NumberofTrips");
    the categorical rows ("D"/"L") carry no count. Floored at 1.
    """
    trips = 1

    for mapped_set in mapping_file:
        if "_logic" not in mapped_set[1]:
            continue

        setting = setting_dictionary.get(build_setting_key(mapped_set))

        try:
            value = int(float(setting))
        except (TypeError, ValueError):
            continue

        trips = max(trips, value)

    return trips


def _is_noja_recloser(device_type: str) -> bool:
    """
    Check if the device is a NOJA recloser.

    NOJA reclosers use a simplified reclosing configuration.

    Args:
        device_type: The device type string

    Returns:
        True if the device is a NOJA recloser
    """
    return any(noja in device_type for noja in NOJA_RECLOSERS)


def _find_reclosing_element(
    app,
    pf_device: Any,
    mapping_file: List[List]
) -> Optional[Any]:
    """
    Find the reclosing element (RelRecl) from the mapping file.

    Searches the mapping file for entries with "_logic" suffix to
    identify the reclosing element configuration.

    Args:
        app: PowerFactory application object
        pf_device: The PowerFactory relay object
        mapping_file: List of mapping file rows

    Returns:
        The RelRecl element, or None if not found
    """
    for mapped_set in mapping_file:
        if "_logic" not in mapped_set[1]:
            continue

        # Create a search line without the "_logic" suffix
        search_line = mapped_set.copy()
        search_line[1] = mapped_set[1].replace("_logic", "")

        element = _find_element_in_relay(app, pf_device, search_line)

        if element and element.GetClassName() == "RelRecl":
            return element

    return None


def _find_element_in_relay(
    app,
    pf_device: Any,
    line: List
) -> Optional[Any]:
    """
    Find a PowerFactory element within a relay.

    This is a simplified version of find_element from relay_settings.py
    used specifically for reclosing element lookup.

    Args:
        app: PowerFactory application object
        pf_device: The PowerFactory relay object
        line: Mapping line with [folder, element_name, ...]

    Returns:
        The PowerFactory element, or None if not found
    """
    obj_contents = pf_device.GetContents(line[1], True)

    if not obj_contents:
        return None

    for obj in obj_contents:
        if obj.fold_id.loc_name == line[0]:
            return obj

    return None


def _find_element_by_name(
    app,
    pf_device: Any,
    element_name: str
) -> Optional[Any]:
    """
    Find an element by name within a relay.

    Args:
        app: PowerFactory application object
        pf_device: The PowerFactory relay object
        element_name: Name of the element to find

    Returns:
        The element, or None if not found
    """
    line = [pf_device.loc_name, element_name]
    return _find_element_in_relay(app, pf_device, line)


def _build_logic_rows(
    app,
    mapping_file: List[List],
    setting_dictionary: Dict[str, Any],
    device_object: Any,
    op_to_lockout: int,
    trip_setting: int
) -> Dict[str, List[float]]:
    """
    Build the logic row dictionary from the mapping file.

    Each row in the logic table defines behavior for a specific
    protection element (e.g., OC1+, OC2+) across all trip numbers.

    Args:
        app: PowerFactory application object
        mapping_file: List of mapping file rows
        setting_dictionary: Dictionary of all settings
        device_object: The ProtectionDevice being configured
        op_to_lockout: Number of operations to lockout
        trip_setting: Trip-to-lockout setting value

    Returns:
        Dictionary mapping row names to lists of logic values
    """
    row_dict = {}

    for mapped_set in mapping_file:
        if "_logic" not in mapped_set[1]:
            continue

        row_name = mapped_set[2]

        # Parse trip number from mapping
        try:
            trip_num = int(mapped_set[-3])
        except ValueError:
            trip_num = mapped_set[-3]

        on_off_key = mapped_set[-2]
        recl = mapped_set[-1]
        key = build_setting_key(mapped_set)

        # Get setting value
        setting = setting_dictionary.get(key, mapped_set[-2])

        # Try to apply setting adjustment
        try:
            if mapped_set[6] != "None":
                setting = setting_adjustment(
                    app, mapped_set, setting_dictionary, device_object
                )
        except (IndexError, KeyError, ValueError, TypeError):
            if mapped_set[3] == "ON":
                trip_num = "ALL"
                setting = trip_setting
            else:
                setting = "off"
                recl = "N"
                trip_num = "ALL"
                on_off_key = "off"

        # Build the logic string for this row
        logic_str = _build_single_row_logic(
            setting, trip_num, on_off_key, recl, op_to_lockout
        )

        row_dict[row_name] = logic_str

    return row_dict


def _build_single_row_logic(
    setting: Any,
    trip_num: Any,
    on_off_key: str,
    recl: str,
    op_to_lockout: int
) -> List[float]:
    """
    Build the logic values for a single row.

    Args:
        setting: The setting value
        trip_num: Trip number (int) or "ALL"
        on_off_key: Key indicating on/off state
        recl: "N" for no reclosing, otherwise allows reclosing
        op_to_lockout: Number of operations to lockout

    Returns:
        List of logic values [trip1, trip2, ..., tripN]
    """
    logic_str = []

    # Check if element allows reclosing
    if recl == "N":
        # No reclosing - determine if disabled or lockout
        if str(setting).lower() == on_off_key.lower():
            set_log = 0.0  # Disabled
        else:
            set_log = 2.0  # Lockout

        for i in range(op_to_lockout):
            if trip_num == "ALL":
                logic_str.append(set_log)
            elif i + 1 == trip_num:
                logic_str.append(set_log)
            else:
                logic_str.append(0.0)

        return logic_str

    # Check if it applies to all trips
    if trip_num == "ALL":
        if setting == "None":
            setting = 1

        for i in range(op_to_lockout):
            if i + 1 < op_to_lockout and i + 1 < float(setting):
                logic_str.append(1.0)  # Reclose
            elif i + 1 == op_to_lockout or i + 1 == float(setting):
                logic_str.append(2.0)  # Lockout
            elif i + 1 > float(setting):
                logic_str.append(0.0)  # Disabled

        return logic_str

    # Row is associated with a specific trip
    for i in range(op_to_lockout):
        if i + 1 != trip_num:
            logic_str.append(0.0)  # Disabled
        elif i + 1 == trip_num and trip_num < op_to_lockout:
            logic_str.append(1.0)  # Reclose
        elif i + 1 == trip_num and trip_num == op_to_lockout:
            logic_str.append(2.0)  # Lockout

    return logic_str


def _apply_logic_to_element(element, row_dict, op_to_lockout, device_name=""):
    """
    Apply the logic row dictionary to the reclosing element.

    Updates the element's ilogic attribute with the calculated values.

    Args:
        element: The RelRecl element
        row_dict: Dictionary mapping row names to logic values
    """
    if not element:
        return

    block_ids = element.GetAttribute("r:typ_id:e:blockid")

    ilogic = []
    unmapped = []
    for block in block_ids:
        values = row_dict.get(block)
        if values is None:
            unmapped.append(block)
            values = [0.0] * op_to_lockout
        ilogic.append([float(v) for v in values])

    if unmapped:
        logger.warning(
            " %s reclose logic: no _logic row for %s; those blocks written "
            "as disabled", device_name, ", ".join(unmapped)
        )

    element.SetAttribute("e:ilogic", ilogic)

    # If reclosing is not active, set to single operation lockout
    if element.GetAttribute("e:reclnotactive"):
        element.SetAttribute("e:oplockout", 1)


def get_trip_num(
    app,
    mapping_file: List[List],
    setting_dictionary: Dict[str, Any]
) -> int:
    """
    Get the number of trips to lockout setting.

    Counts the number of active trip-to-lockout settings in the mapping.

    Args:
        app: PowerFactory application object
        mapping_file: List of mapping file rows
        setting_dictionary: Dictionary of all settings

    Returns:
        Number of trips to lockout (default: 1)
    """
    trips_to_lockout = 1

    for mapped_set in mapping_file:
        if "_TripstoLockout" not in mapped_set[1]:
            continue

        reclosing_key = mapped_set[-1]
        key = build_setting_key(mapped_set)
        setting = setting_dictionary.get(key)

        if setting == reclosing_key:
            trips_to_lockout += 1

    return trips_to_lockout