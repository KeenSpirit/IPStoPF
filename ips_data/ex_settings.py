"""
Energex (EX/SEQ) settings processing for IPS to PowerFactory transfer.

This module handles the Energex-specific logic for:
- Matching PowerFactory switches to IPS relay setting records
- Handling double cable box configurations
- Creating ProtectionDevice objects with associated settings

The module uses SettingIndex for efficient O(1) lookups instead of
linear scans through the settings list.

Switch matching: every switch is matched to IPS by the switch matcher
(mapping/switch_matcher.py, run by ips_data/ex_switch_matching.py; settings
in config/switch_matching.py). The old name lookup (switch name up to the
first "_", the "legacy" and "shadow" modes) was removed on 2026-10-30 after
the 28 Oct fleet run confirmed the matcher in "new" mode.
"""

from typing import Dict, List, Optional, Tuple, Set, Any

from core import ProtectionDevice, SettingRecord, UpdateResult
from ips_data import query_database as qd
from ips_data.setting_index import SettingIndex

from logging_config import get_logger

logger = get_logger(__name__)

from utils.pf_utils import get_all_protection_devices, get_live_relays


def ex_device_list(
    app,
    selections: List[str],
    device_dict: Dict[str, List],
    setting_index: SettingIndex
) -> Tuple[List[str], List[ProtectionDevice]]:
    """
    Create device list for user-selected Energex devices.

    Converts protection device selections to their associated PowerFactory
    switches, then maps switch names to IPS relay setting IDs.

    Args:
        app: PowerFactory application object
        selections: List of device names selected by user
        device_dict: Dictionary mapping device names to [pf_obj, class, phases, feeder, sub]
        setting_index: Indexed IPS settings for O(1) lookups

    Returns:
        Tuple of (setting_ids, list_of_devices)
    """
    prjt = app.GetActiveProject()
    raw_switches = prjt.GetContents("*.StaSwitch", True) + prjt.GetContents("*.ElmCoup", True)

    # Get unique switches for selected devices
    switches = _get_switches_for_selections(app, selections, device_dict, raw_switches)

    # Convert switches to setting IDs and devices
    list_of_devices: List[ProtectionDevice] = []
    late_devices: List[ProtectionDevice] = []
    setting_ids: List[str] = []

    # The selection is matched together with every switch a batch run
    # would process, so the claim rules give the same answer as in batch.
    def considered():
        valid = _get_valid_switches(prjt)
        return list(dict.fromkeys(
            [s for s in valid if _should_process_switch(s)] + list(switches)
        ))
    matching = _start_matching(
        app, prjt, setting_index, False, considered, None, lambda s: True,
    )

    for switch in switches:
        new_ids, list_of_devices = _match_one_switch(
            app, switch, list_of_devices, late_devices, batch=False,
            matching=matching,
        )
        if new_ids is not None:
            setting_ids.extend(new_ids)
    list_of_devices.extend(late_devices)
    _finish_matching(matching, write_log=False)

    # Filter to only include explicitly selected devices
    list_of_devices = _filter_to_selections(list_of_devices, selections, device_dict)

    return setting_ids, list_of_devices


def _get_switches_for_selections(
    app,
    selections: List[str],
    device_dict: Dict[str, List],
    raw_switches: List
) -> List:
    """
    Get unique switches associated with the selected devices.

    Args:
        app: PowerFactory application object
        selections: List of selected device names
        device_dict: Device information dictionary
        raw_switches: All switches in the project

    Returns:
        List of unique switch objects
    """
    switches = []

    for i, device in enumerate(selections):
        if i % 10 == 0:
            logger.info(f"Finding switch for device {i} of {len(selections)}")

        # Extract base device name (before underscore)
        device_name = device.split("_")[0]

        pf_device = device_dict[device][0]
        assoc_switch = _get_assoc_switch(pf_device, raw_switches)

        if assoc_switch and assoc_switch not in switches:
            switches.append(assoc_switch)

    return switches


def _filter_to_selections(
    list_of_devices: List[ProtectionDevice],
    selections: List[str],
    device_dict: Dict[str, List]
) -> List[ProtectionDevice]:
    """
    Filter device list to only include explicitly selected devices.

    Args:
        list_of_devices: All devices found
        selections: User's selections
        device_dict: Device information dictionary

    Returns:
        Filtered list of devices
    """
    filtered = []

    for device in list_of_devices:
        # Build possible name variations, shortened exactly as
        # _get_device_name shortens them, so a relay whose full name is
        # over PowerFactory's 40-character limit can still be selected.
        if device.device_id:
            name_v1 = _fit_pf_name(device.name, f"_{device.device_id}")
            name_v2 = _fit_pf_name(
                device.name, f"_{device.device_id}_{device.seq_name}"
            )
        else:
            name_v1 = _fit_pf_name(device.name, f"_{device.seq_name}")
            name_v2 = None

        # Check if any variation is in selections
        if name_v1 in selections:
            device.pf_obj = device_dict[name_v1][0]
            filtered.append(device)
        elif name_v2 and name_v2 in selections:
            device.pf_obj = device_dict[name_v2][0]
            filtered.append(device)

    return filtered


def create_new_devices(
    app,
    setting_index: SettingIndex,
    batch: bool
) -> Tuple[List[ProtectionDevice], List, List[str]]:
    """
    Batch update for Energex (SEQ) models.

    Processes all switches in the active project, finding or creating
    protection devices based on IPS data.

    Args:
        app: PowerFactory application object
        setting_index: Indexed IPS settings for O(1) lookups
        batch: True if called from batch update

    Returns:
        Tuple of (list_of_devices, failed_cbs, setting_ids)
    """
    prjt = app.GetActiveProject()

    # Get all valid switches
    switches = _get_valid_switches(prjt)

    failed_cbs: List = []
    setting_ids: List[str] = []
    list_of_devices: List[ProtectionDevice] = []
    late_devices: List[ProtectionDevice] = []

    # Match every switch first, so the match log records each cubicle's
    # relays before an unmatched switch's are deleted. A failure here
    # raises TransferError before anything in the model has changed.
    matching = _start_matching(
        app, prjt, setting_index, batch,
        lambda: (
            prjt.GetContents("*.StaSwitch", True)
            + prjt.GetContents("*.ElmCoup", True)
        ),
        switches, _should_process_switch,
    )

    for i, switch in enumerate(switches):
        if i % 10 == 0:
            logger.info(f"IPS is being checked for switch {i} of {len(switches)}")

        # Skip certain ElmCoup switches
        if not _should_process_switch(switch):
            continue

        new_ids, list_of_devices = _match_one_switch(
            app, switch, list_of_devices, late_devices, batch=batch,
            matching=matching,
        )

        # The matcher could not read this switch: leave its cubicle as it
        # is (no devices, nothing deleted). Its relays then go through the
        # orphan sweep, which logs them.
        if new_ids is None:
            continue

        setting_ids.extend(new_ids)

        # Handle switches with no IPS data found
        if not new_ids:
            _handle_unmatched_switch(switch, failed_cbs)

    _finish_matching(matching, write_log=True)

    # Devices from records the old name lookup would not have found are
    # named last, so every device name the old lookup gave (the names the
    # master models carry) is kept. Permanent: changing the order would
    # rename devices and orphan the master's relays.
    list_of_devices.extend(late_devices)

    # Create/assign PowerFactory objects for all devices
    list_of_devices = _assign_pf_objects(list_of_devices)

    # Debug code:
    # for device in list_of_devices:
    #     logger.info(vars(device))

    return list_of_devices, failed_cbs, setting_ids


def _get_valid_switches(prjt) -> List:
    """
    Get all valid switches for processing.

    Args:
        prjt: Active PowerFactory project

    Returns:
        List of valid switch objects
    """
    raw_switches = prjt.GetContents("*.StaSwitch", True) + prjt.GetContents("*.ElmCoup", True)

    return [
        switch for switch in raw_switches
        if switch.GetAttribute("cpGrid")
        and not switch.IsOutOfService()
        and switch.IsCalcRelevant()
        and switch.IsEnergized()
    ]


def _should_process_switch(switch) -> bool:
    """
    Determine if a switch should be processed for protection devices.

    Args:
        switch: The switch object to check

    Returns:
        True if switch should be processed
    """
    try:
        # Skip ElmCoup switches with numeric third character (not feeder bays)
        int(switch.loc_name[2])
        if switch.GetClassName() != "StaSwitch":
            return False
    except (ValueError, IndexError):
        pass

    try:
        if switch.GetClassName() == "StaSwitch":
            if not switch.GetAttribute("r:fold_id:r:obj_id:e:loc_name"):
                return False
    except AttributeError:
        return False

    return True


def _handle_unmatched_switch(switch, failed_cbs: List) -> None:
    """
    Handle a switch that has no matching IPS protection devices.

    Deletes any existing protection devices and records the switch as failed.

    Args:
        switch: The unmatched switch
        failed_cbs: List to append failed CBs to
    """
    # Get contents to clean up
    if switch.GetClassName() == "StaSwitch":
        contents = switch.fold_id.GetContents()
    else:
        root_cub = switch.GetCubicle(0)
        contents = root_cub.GetContents() if root_cub else []

    # Delete existing protection devices
    for content in contents:
        if content.GetClassName() in ["ElmRelay", "RelFuse", "StaCt"]:
            content.Delete()

    # Record CB as failed if it's a circuit breaker
    if switch.GetClassName() == "ElmCoup" and switch.GetAttribute("e:aUsage") == "cbk":
        if switch not in failed_cbs:
            failed_cbs.append(switch)


def _assign_pf_objects(
        list_of_devices: List[ProtectionDevice],
        data_capture_list=None
    ) -> List[ProtectionDevice]:
    """
    Create or assign PowerFactory objects for all devices.

    Args:
        list_of_devices: List of devices needing PF objects
        data_capture_list: Optional list to append info records to when a
            device cannot be placed. If None, drops are logged only.
    Returns:
        Updated list with PF objects assigned
    """
    used_names: Set[str] = set()
    result: List[ProtectionDevice] = []

    for device in list_of_devices:
        full_name, device_name = _device_names(device, used_names)
        used_names.add(device_name)

        switch = device.switch
        # Fuse or relay is decided on the full name, as before: a
        # shortened name may have lost the "fuse" in it.
        pf_obj = _find_or_create_pf_device(
            switch, device_name, is_fuse="fuse" in full_name.lower()
        )

        if pf_obj and pf_obj.loc_name == device_name:
            device.pf_obj = pf_obj
            result.append(device)
        elif pf_obj:
            # PF returned an object whose name doesn't match what we asked
            # for - typically a silent rename on a name collision. Clean up
            # the stray object. It had an IPS setting match and its loss must be visible.
            actual = pf_obj.loc_name
            pf_obj.Delete()
            logger.warning(
                f"Could not place device '{device_name}' (setting {device.setting_id}): "
                f"PF assigned name '{actual}' instead; device skipped"
            )
            if data_capture_list is not None:
                data_capture_list.append(
                    UpdateResult.info_record(pf_device=None, result_message="Could not place device")
                )
        else:
            # No cubicle resolved for the switch - device cannot be placed.
            logger.warning(
                f"Could not place device '{device_name}' (setting {device.setting_id}): "
                f"no cubicle resolved for switch; device skipped"
            )
            if data_capture_list is not None:
                data_capture_list.append(
                    UpdateResult.info_record(pf_device=None, result_message="No cubicle for device")
                )

    return result

# PowerFactory truncates loc_name to 40 characters. A longer name made
# CreateObject return an object named differently from the one asked for,
# which _assign_pf_objects then deleted and skipped.
PF_NAME_MAX = 40
# Below this many characters of the IPS name the shortened name would no
# longer say which switch it belongs to; the whole name is cut instead.
_MIN_STEM = 8

def _fit_pf_name(stem: str, suffix: str) -> str:
    """
    ``stem + suffix`` (right-stripped), shortened to PowerFactory's limit.

    A name within the limit is returned unchanged, so no existing device
    is renamed. A longer one keeps the whole suffix (the IPS device id and
    asset name, e.g. ``_2OCEF(Cphase)_TJM11_C``, which is what tells sibling
    relays apart) and cuts the stem (the IPS name) to fit. The result is the
    same on every run.
    """
    full = f"{stem}{suffix}".rstrip()
    if len(full) <= PF_NAME_MAX:
        return full
    suffix = suffix.rstrip()
    room = PF_NAME_MAX - len(suffix)
    if room < _MIN_STEM:
        return full[:PF_NAME_MAX].rstrip()
    return stem[:room].rstrip(" _+") + suffix

def _get_device_name(device: ProtectionDevice, used_names: Set[str]) -> str:
    """
    Generate the PowerFactory device name.

    Names are ``<IPS name>_<device id>``, or ``<IPS name>_<device id>_<asset>``
    when the first is already used in the project, or ``<IPS name>_<asset>``
    with no device id. A name over PowerFactory's 40-character limit is
    shortened by _fit_pf_name; if the shortened name is already used, ``~2``,
    ``~3`` ... is added. Names within the limit are unchanged.

    Args:
        device: The protection device
    used_names: Set of already-used names

    Returns:
    The device name to use
"""
    return _device_names(device, used_names)[1]


def _device_names(device: ProtectionDevice, used_names: Set[str]) -> Tuple[str, str]:
    """(full name, PowerFactory name); the same string unless shortened."""
    if not device.device_id:
        stem, suffix = device.name, f"_{device.seq_name}"
    else:
        stem, suffix = device.name, f"_{device.device_id}"
        if _fit_pf_name(stem, suffix) in used_names:
            suffix = f"_{device.device_id}_{device.seq_name}"

    name = _fit_pf_name(stem, suffix)
    full = f"{stem}{suffix}".rstrip()
    if name == full:
        return full, name

    # Shortened. Two long names can shorten to the same string; only these
    # (previously unplaceable) names get a tag, so nothing else changes.
    tagged, n = name, 2
    while tagged in used_names:
        tagged = _fit_pf_name(stem, f"{suffix.rstrip()}~{n}")
        n += 1
    logger.info(
        f"Device name '{full}' ({len(full)} characters) is over "
        f"PowerFactory's {PF_NAME_MAX}; named '{tagged}'"
    )
    return full, tagged


def _find_or_create_pf_device(switch, device_name: str, is_fuse: Optional[bool] = None):
    """
    Find existing or create new PowerFactory protection device.

    Args:
        switch: The associated switch object
        device_name: Name for the device
        is_fuse: Create a RelFuse rather than an ElmRelay. None decides
            from device_name, as before; _assign_pf_objects passes the
            answer for the full name, which a shortened name may not show.

    Returns:
        The PowerFactory device object
    """
    if switch.GetClassName() == "StaSwitch":
        cubicle = switch.fold_id
    else:
        cubicle = switch.GetCubicle(0)

    if not cubicle:
        return None

    # Check for existing device
    contents = cubicle.GetContents(f"{device_name}.ElmRelay")
    contents += cubicle.GetContents(f"{device_name}.RelFuse")

    if contents:
        return contents[0]

        # Create new device
    if is_fuse is None:
        is_fuse = "fuse" in device_name.lower()
    if is_fuse:
        new_device = cubicle.CreateObject("RelFuse", device_name)
    else:
        new_device = cubicle.CreateObject("ElmRelay", device_name)
    return new_device


def _get_assoc_switch(pf_device, raw_switches: List):
    """
    Find the switch associated with a protection device.

    Args:
        pf_device: The PowerFactory protection device
        raw_switches: List of all switches

    Returns:
        The associated switch object or None
    """
    for switch in raw_switches:
        try:
            if switch.GetClassName() == "StaSwitch":
                cub = switch.fold_id
                for obj in cub.GetContents():
                    if obj.loc_name == pf_device.loc_name:
                        return switch
            else:
                # Check both bus1 and bus2 for ElmCoup
                for bus_attr in ["bus1", "bus2"]:
                    cub = getattr(switch, bus_attr, None)
                    if cub:
                        for obj in cub.GetContents():
                            if obj == pf_device:
                                return switch
        except AttributeError:
            pass

    return None


def _start_matching(
    app,
    prjt,
    setting_index: SettingIndex,
    batch: bool,
    all_switches,
    valid_switches,
    should_process,
):
    """
    Run the switch matcher for the project.

    Raises TransferError if it fails. There is no fallback lookup any more,
    and an empty result must never be taken as "no switch matched": every
    unmatched switch has its cubicle's relays deleted. Nothing in the model
    has changed when this runs, so the project is recorded as failed and
    left as it was.

    Args:
        all_switches: Callable returning the switches to describe.
        valid_switches: The switches that passed _get_valid_switches, or
            None to treat every switch from all_switches as valid.
        should_process: Filter applied on top (_should_process_switch).
    """
    try:
        from ips_data import ex_switch_matching as esm
        switches = all_switches()
        matching = esm.ProjectSwitchMatching(app, prjt, setting_index, batch)
        matching.match(
            switches,
            switches if valid_switches is None else valid_switches,
            should_process,
        )
        return matching
    except Exception as err:
        logger.error(
            "Switch matching failed; the project's transfer is stopped "
            "before any device is changed",
            exc_info=True,
        )
        raise qd.TransferError(f"Switch matching failed: {err!r}") from err


def _finish_matching(matching, write_log: bool) -> None:
    try:
        matching.finish(write_log=write_log)
    except Exception:
        logger.warning("Switch matching summary failed", exc_info=True)


def _match_one_switch(
    app,
    switch,
    list_of_devices: List[ProtectionDevice],
    late_devices: List[ProtectionDevice],
    batch: bool,
    matching,
) -> Tuple[Optional[List[str]], List[ProtectionDevice]]:
    """
    Setting IDs and devices for one switch, from the matcher's records.

    Devices for records the old name lookup would also have found go to
    list_of_devices; the others go to late_devices, which the caller
    appends after the loop so they are named last.

    Returns (None, list_of_devices) when the matcher has no result for the
    switch (it could not be read); the caller then leaves the switch alone.
    """
    records = matching.records_for(switch)
    if records is None:
        logger.warning(
            f"Switch matching: no result for '{switch.loc_name}' (it could "
            f"not be read); switch skipped, its cubicle left unchanged"
        )
        return None, list_of_devices

    setting_ids: List[str] = []
    for record in records:
        device = _create_device_from_record(app, record, switch, batch)
        if device:
            if matching.is_legacy_record(switch, record.relaysettingid):
                list_of_devices.append(device)
            else:
                late_devices.append(device)
            setting_ids.append(record.relaysettingid)
    return setting_ids, list_of_devices


def _create_device_from_record(
    app,
    record: SettingRecord,
    switch,
    called_function: bool
) -> Optional[ProtectionDevice]:
    """
    Create a ProtectionDevice from a SettingRecord.

    Args:
        app: PowerFactory application object
        record: The IPS setting record
        switch: The associated switch
        called_function: True if batch update

    Returns:
        ProtectionDevice object or None
    """
    # Clean device ID
    device_id = record.deviceid
    if device_id:
        for char in ": /,":
            device_id = device_id.replace(char, "")

    prot_dev = ProtectionDevice(
        app,
        record.patternname,
        record.nameenu,
        record.relaysettingid,
        record.datesetting,
        None,  # pf_obj assigned later
        device_id,
    )
    prot_dev.switch = switch
    prot_dev.seq_name = record.assetname

    # Settings are no longer fetched here one device at a time: that path
    # went through NetDash, which is decommissioned. get_ips_settings bulk-
    # loads every device's settings from the ODS after enumeration, in
    # interactive and batch runs alike.

    # Mark fuses
    if prot_dev.device and "fuse" in prot_dev.device.lower():
        prot_dev.fuse_type = "Line Fuse"

    return prot_dev


def reconcile_orphan_relays(
    app,
    list_of_devices: List[ProtectionDevice],
    data_capture_list: List[UpdateResult],
) -> None:
    """
    Reconcile live ElmRelays that no IPS record accounts for.

    create_new_devices() builds its device list from switches matched to
    IPS, so a pre-existing relay with no IPS counterpart never reaches the
    orchestrator's not_in_ips branch. _handle_unmatched_switch only cleans
    cubicles whose switch matched *nothing*; a switch that matched any IPS
    record - including a switch_* classification carrying no settings -
    leaves every other relay in that cubicle intact.

    Only valid after an exhaustive enumeration. A type-less relay left live
    makes the downstream ComShc raise, so:
      - no relay type assigned -> DELETE
      - relay type assigned    -> set OUT OF SERVICE

    get_all_protection_devices() already restricts to energised, in-service,
    calculation-relevant devices, so anything returned here can reach ComShc.
    """
    # A set: membership was a list scan of PF objects per live relay.
    matched = {d.pf_obj for d in list_of_devices if d.pf_obj}
    # Relays only. get_all_protection_devices also scanned every fuse and
    # classified every device by feeder (feeder.GetAll() per feeder), all
    # discarded here.
    live_devices = get_live_relays(app)

    for pf_device in live_devices:
        if pf_device in matched:
            continue

        if pf_device.typ_id is None:
            # info_record reads loc_name/cpGrid; capture BEFORE Delete()
            data_capture_list.append(
                UpdateResult.info_record(
                    pf_device, "Deleted - no relay type, orphan (no IPS match)"
                )
            )
            logger.info(
                f"{pf_device.loc_name}: orphan relay, no relay type - deleting"
            )
            pf_device.Delete()
        else:
            pf_device.SetAttribute("outserv", 1)
            data_capture_list.append(
                UpdateResult.info_record(
                    pf_device, "Set out of service - orphan (no IPS match)"
                )
            )
            logger.info(
                f"{pf_device.loc_name}: orphan relay - set out of service"
            )