"""
Current Transformer (CT) settings configuration for PowerFactory.

This module handles the configuration of CT devices associated with
protection relays in PowerFactory using settings from the IPS database.

It includes:
- CT slot assignment and update
- CT type selection and creation
- Measurement element configuration
"""

from typing import Any, Optional, Tuple

from utils.pf_utils import all_relevant_objects
from core import UpdateResult
from logging_config import get_logger

logger = get_logger(__name__)


# Slot filters treated as the relay's CT input. Deliberately the exact
# strings the original code used, plus plain 'StaCt' (the CT slot filter on
# the ASEA RI 3OC and RXIDF 2H types; Gladstone 2026-09-24). A pattern match
# would also catch second CT slots (neutral/SEF) and overwrite their CTs with
# the phase CT. Relay types with other spellings are skipped with their
# filters logged instead.
CT_SLOT_FILTERS = ("StaCt*", "StaCt*,StaCombi", "StaCt")


def is_ct_slot(filtmod: Any) -> bool:
    """True when a relay-type slot filter is one of CT_SLOT_FILTERS."""
    return filtmod in CT_SLOT_FILTERS


def get_ct_library(app) -> Any:
    """Find or create the local 'Current Transformers' library folder.

    Resolving this folder is a full recursive walk of the local library, so it
    is expensive. Resolve it once per run (orchestrator.update_pf) and pass it
    into update_ct, rather than recomputing it for every device.
    """
    ct_library = all_relevant_objects(
        app, [app.GetLocalLibrary()], "Current Transformers.IntFolder"
    )
    if not ct_library:
        return app.GetLocalLibrary().CreateObject(
            "IntFolder", "Current Transformers"
        )
    return ct_library[0]


def update_ct(
        app,
        device_object: Any,
        result: UpdateResult,
        ct_library: Optional[Any] = None,
) -> UpdateResult:
    """
    Update CT configuration for a protection device.

    Once the correct mapping table can be accessed in IPS then this function
    will be expanded. Initially it deals with the need to convert a SWER
    recloser from a 3phase to 1 phase CT.

    Args:
        app: PowerFactory application object
        device_object: The ProtectionDevice to configure
        result: UpdateResult to update with CT information

    Returns:
        Updated UpdateResult with CT configuration status
    """
    # At this point the script needs to update the appropriate primary and
    # secondary turns. This means that the type needs to contain the appropriate
    # attributes.
    if device_object.pf_obj.typ_id is None:
        # check_relay_type could not assign a type (already recorded and
        # set out of service there). Without a type there are no slots, and
        # the attribute chain below raised, turning the result into
        # "Script Failed".
        result.ct_result = "No relay type"
        return result

    if ct_library is None:
        ct_library = get_ct_library(app)

    if device_object.pf_obj.typ_id.fold_id.loc_name == "Reclosers":
        current_trans = update_ct_slots(app, device_object)
        if current_trans is None:
            logger.warning(
                f"{device_object.pf_obj.loc_name}: recloser type has no CT "
                f"slot; CT not configured. Slot filters: "
                f"{_slot_filters(device_object.pf_obj)}"
            )
            result.ct_result = "No CT slot on relay type"
            return result
        # Check the type
        ct_type = current_trans.GetAttribute("e:typ_id")
        if not ct_type:
            ct_type = select_ct_type(app, ct_library, 1, 1)
            current_trans.SetAttribute("e:typ_id", ct_type)
        if "swer_" in device_object.device:
            # Only need to reconfigure the CT if it was configured
            current_trans.SetAttribute("iphase", 1)

        ct_name = "{}_CT".format(device_object.pf_obj.loc_name)
        result.set_ct_info(ct_name, "Recloser CT was updated")
        return result

    # Check to see if the relay has a type and if it has available CT ratios
    primary = int(float(device_object.ct_primary))
    if primary == 1:
        # No CT linked in IPS. This branch used to CLEAR the CT slot, which
        # leaves the relay reading secondary amps as primary: SPA then saw
        # 1-2 A primary pickups on 14 Gladstone feeder relays (2026-10-03,
        # e.g. FILASS-FB51-J01-S101-3OC-A-A, CLINSS-FB10-J01-J11) and graded
        # and damage-checked against them. Missing data must not become
        # plausible-looking wrong data, so:
        #   - a CT already in the slot is kept (the best data available);
        #   - with no CT anywhere the relay is set out of service and the
        #     result says why.
        return _handle_no_ips_ct(device_object, result)

    secondary = int(float(device_object.ct_secondary))
    current_trans = update_ct_slots(app, device_object)
    if current_trans is None:
        slots = _slot_filters(device_object.pf_obj)
        logger.warning(
            f"{device_object.pf_obj.loc_name}: relay type has no CT slot; "
            f"CT {primary}/{secondary} not applied. Slot filters: {slots}"
        )
        result.ct_result = "No CT slot on relay type"
        return result
    required_ct_type = select_ct_type(app, ct_library, primary, secondary)

    try:
        if required_ct_type.loc_name != current_trans.GetAttribute(
                "r:typ_id:e:loc_name"
        ):
            current_trans.SetAttribute("e:typ_id", required_ct_type)
    except AttributeError:
        # This means the CT does not have a type ID already
        current_trans.SetAttribute("e:typ_id", required_ct_type)

    current_trans.SetAttribute("e:ptapset", primary)
    current_trans.SetAttribute("e:stapset", secondary)

    ct_date = getattr(device_object, "ct_datesetting", None)
    if device_object.ct_op_id and ct_date:
        current_trans.SetAttribute("e:sernum", ct_date)

    result.set_ct_info(device_object.ct_op_id, "CT info updated")

    # Check that measuring devices have matching CT secondary
    check_update_measurement_elements(app, device_object.pf_obj, secondary)

    return result


REMOTE_CT_SLOT_NAMES = ("Ct-3P(remote)", "Winding 2 Ct")


def _model_ct(pf_device: Any) -> Tuple[bool, Optional[Any]]:
    """
    (relay type has a local CT slot, StaCt currently in the first such slot).

    Remote CT slots are ignored: update_ct_slots clears them because
    PowerFactory populates them itself.
    """
    try:
        blocks = pf_device.GetAttribute("typ_id").GetAttribute("pblk")
        slot_objs = pf_device.GetAttribute("pdiselm")
    except AttributeError:
        return False, None
    has_slot = False
    for i, item in enumerate(blocks or []):
        if not item or not is_ct_slot(item.GetAttribute("filtmod")):
            continue
        if item.loc_name in REMOTE_CT_SLOT_NAMES:
            continue
        has_slot = True
        obj = slot_objs[i] if slot_objs and i < len(slot_objs) else None
        if obj is not None:
            return True, obj
    return has_slot, None


def _handle_no_ips_ct(device_object: Any, result: UpdateResult) -> UpdateResult:
    """IPS links no CT to this relay: keep the model's CT, or take it OOS."""
    pf_device = device_object.pf_obj
    name = pf_device.loc_name
    has_slot, ct_obj = _model_ct(pf_device)

    if ct_obj is not None:
        try:
            taps = f"{ct_obj.GetAttribute('e:ptapset'):g}/{ct_obj.GetAttribute('e:stapset'):g}"
        except (AttributeError, TypeError, ValueError):
            taps = "taps unreadable"
        logger.warning(
            f"{name}: no CT linked in IPS; kept the CT already in the model "
            f"({ct_obj.loc_name}, {taps})"
        )
        result.set_ct_info(ct_obj.loc_name, "No CT in IPS - model CT kept")
        return result

    if not has_slot:
        result.ct_result = "No CT slot on relay type"
        return result

    pf_device.SetAttribute("outserv", 1)
    logger.warning(
        f"{name}: no CT linked in IPS and no CT in the model; relay set out "
        f"of service (without a CT it would read secondary amps as primary)"
    )
    result.ct_result = "No CT in IPS or model"
    if not result.result:
        result.result = "No CT - set out of service"
    return result


def _slot_filters(pf_device: Any) -> list:
    """Slot names and filters of a relay type, for diagnostics only."""
    try:
        return [
            (item.loc_name, item.GetAttribute("filtmod"))
            for item in pf_device.GetAttribute("typ_id").GetAttribute("pblk")
            if item
        ]
    except (AttributeError, TypeError):
        return []


def select_ct_type(
        app,
        ct_library: Any,
        primary: int,
        secondary: int
) -> Any:
    """
    Check the local library for a suitable CT type or create a new one.

    Args:
        app: PowerFactory application object
        ct_library: The CT library folder
        primary: Primary tap setting
        secondary: Secondary tap setting

    Returns:
        The PowerFactory TypCt object
    """
    ct_types = ct_library.GetContents("*.TypCt")

    for ct_type in ct_types:
        primary_taps = ct_type.GetAttribute("e:primtaps")
        secondary_taps = ct_type.GetAttribute("e:sectaps")
        if primary in primary_taps and secondary in secondary_taps:
            break
    else:
        ct_type = ct_library.CreateObject("TypCt", "{}/{}".format(primary, secondary))
        ct_type.SetAttribute("e:primtaps", [primary])
        ct_type.SetAttribute("e:sectaps", [secondary])

    return ct_type


def update_ct_slots(app, device_object: Any) -> Any:
    """
    Update the slot in the relay that the CT is assigned to.

    A setting slot is part of a list of elements. This function will
    ensure the correct CT is allocated to the correct CT slot.

    Args:
        app: PowerFactory application object
        device_object: The ProtectionDevice being configured

    Returns:
        The PowerFactory StaCt object
    """
    pf_device = device_object.pf_obj
    cubical = pf_device.fold_id
    slot_objs = pf_device.GetAttribute("pdiselm")
    remote_ct_slot_names = REMOTE_CT_SLOT_NAMES

    if not device_object.ct_op_id:
        ct_name = "{}_CT".format(pf_device.loc_name)
    else:
        ct_name = device_object.ct_op_id

    current_trans = None

    for i, item in enumerate(pf_device.GetAttribute("typ_id").GetAttribute("pblk")):
        if not item:
            continue

        filtmod = item.GetAttribute("filtmod")
        if is_ct_slot(filtmod):
            if item.loc_name in remote_ct_slot_names:
                # Clear the remote CT slots. These get automatically populated
                slot_objs[i] = None
                continue

            ct_obj = pf_device.GetSlot(item.GetAttribute("loc_name"))
            if not ct_obj:
                ct_obj_name = "Not Configured"
            else:
                ct_obj_name = ct_obj.loc_name

            if ct_obj_name == ct_name:
                current_trans = ct_obj
            else:
                for obj in cubical.GetContents("*.StaCt"):
                    if not obj:
                        continue
                    obj_name = obj.loc_name

                    if obj_name == ct_name:
                        slot_objs[i] = obj
                        current_trans = obj
                        break

                    if (
                            obj.ptapset == device_object.ct_primary
                            and obj.stapset == device_object.ct_secondary
                            and not device_object.ct_op_id
                    ):
                        new_name = str()
                        for char in device_object.pf_obj.loc_name:
                            if char == "_":
                                break
                            new_name = new_name + char
                        obj.loc_name = f"{new_name}_CT"
                        slot_objs[i] = obj
                        current_trans = obj
                        break
                else:
                    current_trans = cubical.CreateObject("StaCt", ct_name)
                    slot_objs[i] = current_trans

    pf_device.SetAttribute("pdiselm", slot_objs)
    return current_trans


def check_update_measurement_elements(
        app,
        pf_device: Any,
        secondary: int
) -> None:
    """
    Update measurement elements with matching CT secondary rating.

    Not all setting files have a setting that can define the secondary
    rating of the CT. This function will use the CT secondary to configure
    this attribute.

    Args:
        app: PowerFactory application object
        pf_device: The PowerFactory relay object
        secondary: The CT secondary rating
    """
    measurement_elements = pf_device.GetContents("*.RelMeasure")

    for element in measurement_elements:
        try:
            element.SetAttribute("e:Inom", secondary)
        except AttributeError:
            pass
