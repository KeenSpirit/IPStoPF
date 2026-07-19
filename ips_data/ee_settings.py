"""
Ergon (EE) regional settings processing for IPS to PowerFactory transfer.

This module handles the Ergon-specific logic for:
- Extracting plant numbers from device names
- Matching PowerFactory devices to IPS setting records
- Creating ProtectionDevice objects with associated settings

The module uses SettingIndex for efficient O(1) lookups instead of
linear scans through the settings list.
"""

from collections import Counter
from typing import Dict, List, Optional, Tuple, Any, Union

from core import ProtectionDevice, SettingRecord, UpdateResult
from utils.pf_utils import determine_fuse_role
from ips_data import query_database as qd
from ips_data.setting_index import SettingIndex
from logging_config import get_logger

logger = get_logger(__name__)

# No plausible single cubicle holds more matches than this; beyond it the
# identifier is degenerate and the match set is garbage.
_MAX_PARTIAL_MATCHES = 10

# Diagnostic tallies for the enumeration match layer (reset per batch run)
_MATCH_STATS: Counter = Counter()
_MATCH_OFFENDERS: List[Tuple[int, str, str, List[str]]] = []


def _is_usable_plant_number(plant_number: Optional[str]) -> bool:
    """Reject identifiers too degenerate to match safely.

    A bare prefix like 'RC-' substring-matched 2,948 asset records
    (every recloser in the region) on 2026-07-19. Real plant numbers
    contain digits and are at least 4 characters.
    """
    if not plant_number or len(plant_number) < 4:
        return False
    return any(ch.isdigit() for ch in plant_number)


def _record_match(plant_number: str, source: str, records) -> None:
    """Tally a match and flag candidates that sweep many asset records."""
    n = len(records)
    _MATCH_STATS[source] += 1
    _MATCH_STATS[f"records_via_{source}"] += n
    if n > 1:
        samples = [r.assetname for r in records[:3]]
        _MATCH_OFFENDERS.append((n, plant_number, source, samples))
        if n > 5:
            logger.warning(
                f"Enumeration: plant number '{plant_number}' matched {n} "
                f"asset records via {source} match, e.g. {samples}"
            )


def _log_match_summary(
    n_candidates: int,
    setting_ids: List[str],
    list_of_devices: List,
) -> None:
    logger.info(
        f"Enumeration summary: {n_candidates} candidates -> "
        f"{len(list_of_devices)} devices, {len(setting_ids)} setting IDs "
        f"({len(set(setting_ids))} unique)"
    )
    logger.info(f"Enumeration match sources: {dict(_MATCH_STATS)}")
    for n, plant, source, samples in sorted(_MATCH_OFFENDERS, reverse=True)[:10]:
        logger.info(
            f"Enumeration offender: '{plant}' -> {n} records ({source}), "
            f"e.g. {samples}"
        )


def ee_device_list(
    app,
    selections: List[str],
    device_dict: Dict[str, List],
    setting_index: SettingIndex,
    data_capture_list: List[UpdateResult]
) -> Tuple[List[str], List[ProtectionDevice], List[UpdateResult]]:
    """
    Create device list for user-selected Ergon devices.

    Processes the user's device selections and creates ProtectionDevice
    objects with their associated IPS settings.

    Args:
        app: PowerFactory application object
        selections: List of device names selected by user
        device_dict: Dictionary mapping device names to [pf_obj, class, phases, feeder, sub]
        setting_index: Indexed IPS settings for O(1) lookups
        data_capture_list: List to append status/error records to

    Returns:
        Tuple of (setting_ids, list_of_devices, data_capture_list)
    """
    list_of_devices: List[ProtectionDevice] = []
    setting_ids: List[str] = []

    for i, device_name in enumerate(selections):
        if i % 10 == 0:
            logger.info(f"IPS is being checked for device {i} of {len(selections)}")

        plant_number = get_plant_number(device_name)
        pf_device = device_dict[device_name][0]

        if not _is_usable_plant_number(plant_number):
            if plant_number:
                logger.warning(
                    f"Enumeration: device '{pf_device.loc_name}' yields "
                    f"unusable plant number '{plant_number}'; skipping match"
                )
            data_capture_list.append(
                UpdateResult.info_record(pf_device, "Not a protection device")
            )
            continue

        # Handle non-relay devices (fuses)
        if device_dict[device_name][1] != "ElmRelay":
            fuse_result = _process_fuse_device(app, pf_device, list_of_devices)
            if fuse_result == "skip":
                continue
            elif fuse_result == "failed":
                data_capture_list.append(
                    UpdateResult.info_record(pf_device, "FAILED FUSE")
                )
                continue
            fuse_type, fuse_size = fuse_result
        else:
            fuse_type = None
            fuse_size = None

        # Look up setting in index and create device
        setting_ids, list_of_devices = _get_setting_id_indexed(
            app=app,
            plant_number=plant_number,
            list_of_devices=list_of_devices,
            setting_ids=setting_ids,
            pf_device=pf_device,
            fuse_type=fuse_type,
            fuse_size=fuse_size,
            setting_index=setting_index,
            batch=False,
        )

    return setting_ids, list_of_devices, data_capture_list


def ergon_all_dev_list(
    app,
    data_capture_list: List[UpdateResult],
    setting_index: SettingIndex,
    batch: bool
) -> Tuple[List[str], List[ProtectionDevice], List[UpdateResult]]:
    """
    Process all protection devices in the active Ergon project.

    This is used for batch updates where all devices are processed
    rather than a user selection.

    Args:
        app: PowerFactory application object
        data_capture_list: List to append status/error records to
        setting_index: Indexed IPS settings for O(1) lookups
        batch: True if called from batch update

    Returns:
        Tuple of (setting_ids, list_of_devices, data_capture_list)
    """
    _MATCH_STATS.clear()
    _MATCH_OFFENDERS.clear()
    prot_devices = get_all_protection_devices(app)
    list_of_devices: List[ProtectionDevice] = []
    setting_ids: List[str] = []

    for i, pf_device in enumerate(prot_devices):
        if i % 10 == 0:
            logger.info(f"IPS is being checked for device {i} of {len(prot_devices)}")

        # Delete duplicate devices (names ending with parentheses)
        if pf_device.loc_name.endswith(")"):
            pf_device.Delete()
            continue

        plant_number = get_plant_number(pf_device.loc_name)

        if not _is_usable_plant_number(plant_number):
            if plant_number:
                logger.warning(
                    f"Enumeration: device '{pf_device.loc_name}' yields "
                    f"unusable plant number '{plant_number}'; skipping match"
                )
            data_capture_list.append(
                UpdateResult.info_record(pf_device, "Not a protection device")
            )
            continue

        # Handle non-relay devices (fuses)
        if pf_device.GetClassName() != "ElmRelay":
            fuse_result = _process_fuse_device(app, pf_device, list_of_devices)
            if fuse_result == "skip":
                continue
            elif fuse_result == "failed":
                data_capture_list.append(
                    UpdateResult.info_record(pf_device, "FAILED FUSE")
                )
                continue
            fuse_type, fuse_size = fuse_result
        else:
            fuse_type = None
            fuse_size = None

        # Look up setting in index and create device
        setting_ids, list_of_devices = _get_setting_id_indexed(
            app=app,
            plant_number=plant_number,
            list_of_devices=list_of_devices,
            setting_ids=setting_ids,
            pf_device=pf_device,
            fuse_type=fuse_type,
            fuse_size=fuse_size,
            setting_index=setting_index,
            batch=batch,
        )

    _log_match_summary(len(prot_devices), setting_ids, list_of_devices)

    return setting_ids, list_of_devices, data_capture_list


def _process_fuse_device(
    app,
    pf_device,
    list_of_devices: List[ProtectionDevice]
) -> Union[str, Tuple[str, str]]:
    """
    Process a fuse device and determine if it should be added to the list.

    Args:
        app: PowerFactory application object
        pf_device: The PowerFactory fuse object
        list_of_devices: List to potentially add device to

    Returns:
        "skip" if device should be skipped (Tx fuse added to list)
        "failed" if fuse type couldn't be determined
        (fuse_type, fuse_size) tuple otherwise
    """
    fuse_type, fuse_size = determine_fuse_role(app, pf_device)

    if fuse_type == "Tx Fuse":
        prot_dev = ProtectionDevice(
            app, fuse_type, pf_device.loc_name, None, None, pf_device, None
        )
        prot_dev.fuse_size = fuse_size
        prot_dev.fuse_type = fuse_type
        list_of_devices.append(prot_dev)
        return "skip"

    if not fuse_type:
        return "failed"

    return (fuse_type, fuse_size)


def get_plant_number(device_name: str) -> Optional[str]:
    """
    Extract plant number from a device name.

    Plant numbers have specific structures depending on device type:
    - Reclosers: RC-{Number} or RE-{Number}
    - Relays: {SUB}SS-{BAY}-{Device}
    - Fuses: DO-{Number}, FU-{Number}, or DL-{Number}

    Args:
        device_name: The full device name from PowerFactory

    Returns:
        The extracted plant number, or None if not a valid protection device name
    """
    # Check if name matches expected patterns
    valid_patterns = [
        device_name[4:7] == "SS-",  # Relay pattern
        device_name[:3] == "RC-",   # Recloser
        device_name[:3] == "RE-",   # Recloser
        device_name[:3] == "DO-",   # Dropout fuse
        device_name[:3] == "FU-",   # Fuse
        device_name[:3] == "DL-",   # Distribution line fuse
    ]

    if not any(valid_patterns):
        return None

    # Extract plant number (everything before first space)
    return device_name.split(" ")[0]


def get_all_protection_devices(app) -> List:
    """
    Get all active protection devices in the current project.

    Args:
        app: PowerFactory application object

    Returns:
        List of relay and fuse PowerFactory objects
    """
    net_mod = app.GetProjectFolder("netmod")

    # Get all relays that are in valid locations and active
    all_relays = net_mod.GetContents("*.ElmRelay", True)
    relays = [
        relay for relay in all_relays
        if relay.GetAttribute("cpGrid")
        and relay.cpGrid.IsCalcRelevant()
        and relay.GetParent().GetClassName() == "StaCubic"
    ]

    # Get all fuses that are active
    all_fuses = net_mod.GetContents("*.RelFuse", True)
    fuses = [
        fuse for fuse in all_fuses
        if fuse.GetAttribute("cpGrid")
        and fuse.cpGrid.IsCalcRelevant()
    ]

    return relays + fuses


def _get_setting_id_indexed(
    app,
    plant_number: str,
    list_of_devices: List[ProtectionDevice],
    setting_ids: List[str],
    pf_device,
    fuse_type: Optional[str],
    fuse_size: Optional[str],
    setting_index: SettingIndex,
    batch: bool,
) -> Tuple[List[str], List[ProtectionDevice]]:
    """
    Find setting ID(s) for a device using the indexed lookup.

    This replaces the original reg_get_setting_id function with O(1) lookups
    instead of O(n) linear scans.

    Args:
        app: PowerFactory application object
        plant_number: The plant number to look up
        list_of_devices: List to append new devices to
        setting_ids: List to append found setting IDs to
        pf_device: The PowerFactory device object
        fuse_type: Type of fuse if applicable
        fuse_size: Size of fuse if applicable
        setting_index: The indexed settings for O(1) lookup
        batch: True if called from batch update

    Returns:
        Tuple of (updated setting_ids, updated list_of_devices)
    """
    # First try exact match (O(1) lookup)
    exact_matches = setting_index.get_by_asset_exact(plant_number)

    if exact_matches:
        _record_match(plant_number, "exact", exact_matches)
        # Found exact match - use it
        for record in exact_matches:
            device = _create_device_from_record(
                app, record, pf_device, fuse_type, fuse_size, batch
            )
            if device:
                list_of_devices.append(device)
                setting_ids.append(record.relaysettingid)
        return setting_ids, list_of_devices

    # Try partial match (device name contained in asset name)
    partial_matches = setting_index.get_by_asset_contains(plant_number)

    if len(partial_matches) > _MAX_PARTIAL_MATCHES:
        logger.warning(
            f"Enumeration: '{plant_number}' (device '{pf_device.loc_name}') "
            f"partial-matched {len(partial_matches)} asset records - "
            f"implausible for one cubicle; treating as no IPS match"
        )
        _MATCH_STATS["capped"] += 1
        partial_matches = []

    if partial_matches:
        # Distinguish which fallback inside get_by_asset_contains fired.
        # (Reads a private index attr - diagnostic only, remove with the fix.)
        source = (
            "prefix"
            if setting_index._by_asset_prefix.get(plant_number)
            else "substring"
        )
        _record_match(plant_number, source, partial_matches)
        # Handle multiple devices in a single cubicle
        pf_device_name = pf_device.loc_name

        for record in partial_matches:
            asset_name = record.assetname

            # Try to find or create the appropriate PF device
            target_device = _find_or_create_relay(
                pf_device, pf_device_name, asset_name
            )

            if target_device:
                device = _create_device_from_record(
                    app, record, target_device, fuse_type, fuse_size, batch
                )
                if device:
                    list_of_devices.append(device)
                    setting_ids.append(record.relaysettingid)

        if partial_matches:
            return setting_ids, list_of_devices

    # No match found - create device without settings
    _MATCH_STATS["no_match"] += 1
    no_setting_device = ProtectionDevice(
        app, None, None, None, None, pf_device, None
    )
    no_setting_device.fuse_type = fuse_type
    no_setting_device.fuse_size = fuse_size
    list_of_devices.append(no_setting_device)

    return setting_ids, list_of_devices


def _create_device_from_record(
    app,
    record: SettingRecord,
    pf_device,
    fuse_type: Optional[str],
    fuse_size: Optional[str],
    called_function: bool
) -> Optional[ProtectionDevice]:
    """
    Create a ProtectionDevice from a SettingRecord.

    Args:
        app: PowerFactory application object
        record: The IPS setting record
        pf_device: The PowerFactory device object
        fuse_type: Type of fuse if applicable
        fuse_size: Size of fuse if applicable
        called_function: True if called from batch update

    Returns:
        ProtectionDevice object or None if creation failed
    """
    prot_dev = ProtectionDevice(
        app,
        record.patternname,
        record.assetname,
        record.relaysettingid,
        record.datesetting,
        pf_device,
        None,
    )

    # Load settings if not a batch call
    if not called_function:
        ips_settings = qd.reg_get_ips_settings(app, record.relaysettingid)
        prot_dev.associated_settings(ips_settings)

    prot_dev.fuse_type = fuse_type
    prot_dev.fuse_size = fuse_size

    return prot_dev


def _find_or_create_relay(
    pf_device,
    pf_device_name: str,
    asset_name: str
):
    """
    Find an existing relay with the asset name or create/rename one.

    This handles cases where multiple relays exist in a single cubicle.

    Args:
        pf_device: The original PowerFactory device
        pf_device_name: Original device name
        asset_name: The IPS asset name to match

    Returns:
        The PowerFactory device to use (existing, renamed, or new)
    """
    cubicle = pf_device.fold_id

    # Check if a device with this name already exists
    for device in cubicle.GetContents("*.ElmRelay"):
        if device.loc_name == asset_name:
            return device
        elif device.loc_name == pf_device_name:
            # Rename the original device
            device.loc_name = asset_name
            return device

    # Create new device in the cubicle
    return cubicle.CreateObject("ElmRelay", asset_name)

