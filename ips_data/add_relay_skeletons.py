"""
Unified protection relay skeleton builder.

The intended usage is selected via the ``network_level`` argument
to :func:`add_relay_skeletons`:

    from ips_data import add_relay_skeletons as ars

    # Distribution (whole-project scan, feeder-CB gating):
    ars.add_relay_skeletons(app, network_level=ars.NETWORK_DISTRIBUTION)

    # Subtransmission (single-grid scan, no feeder-CB gating):
    ars.add_relay_skeletons(
        app,
        network_level=ars.NETWORK_SUBTRANSMISSION,
        selected_grid=grid,
    )

Behavioural differences per mode
------------------------------------------
DISTRIBUTION:
    * Scans the entire active project for ElmCoup and StaSwitch elements.
    * Builds the feeder-CB list and skips substation switches that are
      not feeder CBs.
    * ``determine_root_cub_relay_exists`` returns ALL relays of the
      requested class found in the element's cubicles; ``setup_relay``
      then deletes near-duplicates by plant-number name matching. This
      purge behaviour is intentional for distribution models.
    * ``ellipse_ecorp_asset_id_extraction`` pads the extracted id to
      12 characters with leading zeros (ecorp asset-id format).

SUBTRANSMISSION:
    * Scans only the passed ``selected_grid`` and additionally includes
      ElmLne, ElmTr2 and ElmTerm elements (line, transformer and busbar
      protection schemes).
    * No feeder-CB gating - every matched element is processed.
    * ``determine_root_cub_relay_exists`` filters the found relays to
      those whose ``for_name`` equals the expected foreign key, so
      multiple protection schemes sharing one CB coexist without being
      deleted by the distribution purge logic.
    * ``ellipse_ecorp_asset_id_extraction`` strips a leading prefix such
      as ``"001:"`` (everything up to and including the first colon) and
      returns the extracted id UNPADDED.
"""

import sys
import re
from collections import defaultdict

from config.paths import ASSET_CLASSES_PATH

# Guard the asset_classes library so
# repeated imports/reloads don't append duplicates to sys.path.
if ASSET_CLASSES_PATH not in sys.path:
    sys.path.append(ASSET_CLASSES_PATH)
import assetclasses  # noqa: F401,E402
from assetclasses.corporate_data import get_cached_data  # noqa: E402

from logging_config.logging_utils import get_logger  # noqa: E402

logger = get_logger(__name__)

DATA_SOURCE_STRING = "PRS"
GAS_SWITCH_STRING = "Gas Switch"

NETWORK_DISTRIBUTION = "distribution"
NETWORK_SUBTRANSMISSION = "subtransmission"
_VALID_NETWORK_LEVELS = (NETWORK_DISTRIBUTION, NETWORK_SUBTRANSMISSION)


def add_relay_skeletons(app, network_level, selected_grid=None, project=None):
    """
    Get all the protection information from ellipse/gisep and process
    each relevant element within the model. Add the skeletons if they do
    not yet exist.

    Args:
        app: PowerFactory application object.
        network_level: NETWORK_DISTRIBUTION or NETWORK_SUBTRANSMISSION.
        selected_grid: The grid to scan. Required for subtransmission;
            ignored for distribution (which scans the whole project).
        project: Optional project override; defaults to the active
            project and must match it.

    Returns:
        List of newly created (or found) relay objects, or None on an
        early exit (no active project / project mismatch).

    Distribution mode: switches inside substations that are not feeder
    CBs are ignored. Subtransmission mode: no feeder-CB gating; lines,
    transformers and terminals are also scanned.
    """
    if network_level not in _VALID_NETWORK_LEVELS:
        raise ValueError(
            f"network_level must be one of {_VALID_NETWORK_LEVELS}, "
            f"got {network_level!r}"
        )
    if network_level == NETWORK_SUBTRANSMISSION and selected_grid is None:
        raise ValueError(
            "selected_grid is required when network_level is "
            f"{NETWORK_SUBTRANSMISSION!r}"
        )

    if project is None:
        project = app.GetActiveProject()
    if project is None:
        logger.error("No Active Project or passed project, Ending Script")
        return
    if project != app.GetActiveProject():
        logger.error("Passed project is not the active project. Ending Script")
        return

    logger.debug("Deleting PDS elements")
    remove_pds_elements(project)

    # Get information from GISEP/Ellipse
    logger.info("Getting Relay Information")
    relay_info = get_cached_data(report="List-RelayCBs", max_age=3)
    logger.debug("Got Relays")
    logger.info("Getting Recloser Information")
    recloser_info = get_cached_data(report="List-Reclosers", max_age=3)
    logger.debug("Got Reclosers")
    logger.info("Getting Fuse Information")
    fuses_info = get_cached_data(report="List-Fuses", max_age=3)
    logger.debug("Got Fuses")
    logger.info("Getting Gas Switch Information")
    gas_switch_info = get_cached_data(report="List-GasSwitches", max_age=3)
    logger.debug("Got Gas Switch")

    # Building Dictionaries
    logger.info("Building Relay Dictionary")
    relay_dict = produce_switch_based_dict(relay_info)
    logger.info("Building Recloser Dictionary")
    recloser_dict = produce_line_switch_based_dict(recloser_info)
    logger.info("Building Fuse Dictionary")
    fuse_dict = produce_line_switch_based_dict(fuses_info)
    logger.info("Building Gas Switch Dictionary")
    gas_switch_dict = produce_line_switch_based_dict(gas_switch_info)
    logger.info("Dictionaries Built")

    if network_level == NETWORK_DISTRIBUTION:
        # Feeder-CB gating applies: build the list of feeder CBs and
        # scan the whole project for switches only.
        feeder_cbs = produce_list_of_model_feeder_cbs(project)
        logger.info("Feeder CBs Identified")

        elm_coups = project.GetContents("*.ElmCoup", True)
        sta_switches = project.GetContents("*.StaSwitch", True)
        elements = elm_coups + sta_switches
    else:
        # Subtransmission: no feeder-CB gating; scan the selected grid
        # only, and include lines, transformers and terminals.
        feeder_cbs = None

        elm_coups = selected_grid.GetContents("*.ElmCoup", 1)
        sta_switches = selected_grid.GetContents("*.StaSwitch", 1)
        lines = selected_grid.GetContents("*.ElmLne", 1)
        tfmr = selected_grid.GetContents("*.ElmTr2", 1)
        terms = selected_grid.GetContents("*.ElmTerm", 1)
        elements = elm_coups + sta_switches + lines + tfmr + terms

    num_elements = len(elements)

    all_new = []
    for i, elm in enumerate(elements):
        if i % 100 == 0:
            logger.info(f"Checking Switch {i+1}/{num_elements}")

        new_devices = process_switch_for_relay_check(
            app,
            elm,
            relay_dict,
            fuse_dict,
            recloser_dict,
            gas_switch_dict,
            feeder_cbs,
            network_level,
        )
        all_new.extend(new_devices)

    logger.info(f"Relay skeleton pass complete: {len(all_new)} new devices")
    return all_new


def remove_pds_elements(project):

    protection_elms = list()
    for class_name in [
        "ElmRelay",
        "RelFuse",
        "RelIoc",
        "RelLogdip",
        "RelLogic",
        "RelMeasure",
        "RelRecl",
        "RelToc",
        "StaCt",
    ]:
        objs = project.GetContents(f"*.{class_name}", True)
        protection_elms.extend(objs)

    deleted_elms = list()
    problem_classes = list()
    for obj in protection_elms:
        try:
            data_source = obj.GetAttribute("dat_src")
        except AttributeError:
            problem_classes.append(obj.GetClassName())
            logger.warning(f'{obj} has no attribute "dat_scr"')
            continue
        if data_source == "PDS":
            if not obj.IsDeleted():
                ef = obj.Delete()
                if ef:
                    logger.error(f"Unable to delete {obj}")
                else:
                    deleted_elms.append(obj)

    for problem_class in problem_classes:
        logger.warning(f"{problem_class} has no dat_scr attribute")

    logger.info(f"Deleted {len(deleted_elms)} PDS Objects.")


def produce_switch_based_dict(relay_info):
    """Produce a dictionary for the relays, ignores null CB rows"""
    d = defaultdict(list)
    nulls = list()
    for data in relay_info:
        # logger.debug(data)
        asset_id = data.cb_asset_id
        if not asset_id:
            nulls.append(data)
        else:
            d[str(asset_id)].append(data)

    logger.debug(
        f"There are {len(nulls)} relays with no CB associated"
        f" with the protection scheme. "
        f"These should be bus or tx protection schemes without local CBs."
    )

    return d


def produce_line_switch_based_dict(info):
    """
    Produce a dictionary for fuses and recloser information.

    Rows without an ``asset_id`` attribute are skipped (defensive
    behaviour originally from the subtransmission module, now applied
    in both modes).
    """
    d = defaultdict(list)

    for data in info:
        try:
            asset_id = data.asset_id
        except AttributeError:
            continue
        if not asset_id:
            logger.warning(f'Null asset_id: "{asset_id}" in {data}')
        else:
            d[str(asset_id)].append(data)

    return d


def produce_list_of_model_feeder_cbs(project):
    """Produce a list of CBs associated with feeders (distribution only)"""
    feeders = project.GetContents("*.ElmFeeder", True)

    feeder_cbs = list()

    for feeder in feeders:
        cub = feeder.GetAttribute("obj_id")
        if cub:
            switch = cub.GetAttribute("obj_id")
            if switch:
                feeder_cbs.append(switch)

    return feeder_cbs


def process_switch_for_relay_check(
    app,
    elm,
    relay_dict,
    fuse_dict,
    recloser_dict,
    gas_switch_dict,
    feeder_cbs,
    network_level,
):
    """
    Check if elm should have a relay, recloser or fuse.
    If it should ensure the appropriate rel object exists.

    Will also delete any objects that are in the wrong location
    from the ETL where the correct switch location is within the model.

    ``feeder_cbs`` is the list of feeder CBs (distribution mode) or
    None (subtransmission mode - no feeder-CB gating).
    """

    # Distribution only: ignore switches in the substation that are not
    # feeder CBs. Not short-circuited so the error message can be more
    # detailed.
    pot_feeder_cb = True
    if feeder_cbs is not None:
        parent = elm.GetParent()
        if parent.GetClassName() == "ElmSubstat":
            if elm not in feeder_cbs:
                pot_feeder_cb = False

    # Get the expected foreign key (ellipse ID)
    foreign_key = elm.GetAttribute("for_name")
    ecorp_id = ellipse_ecorp_asset_id_extraction(foreign_key, network_level)

    if ecorp_id == "25268198":
        logger.debug(f"** {elm} should have multiple relays")

    # Now process each type of protection device sequentially for
    # information associated with the switch

    new_devices = list()

    # Relays
    try:
        relay_info = relay_dict[ecorp_id]
    except KeyError:
        # Elm does not have a relay associated with it,
        # check the other types
        pass
    else:
        if len(relay_info) > 1:
            logger.debug(f"Multiple Protection Relays found for {elm}")

        # Handle potential for multiple fuses on one recloser
        for relay_data in relay_info:
            # Relay associated, ensure it exists
            if pot_feeder_cb:
                logger.debug(
                    f"setting up relay {relay_data.plant_no} on {elm},"
                    f" {relay_data.ellipse_equip_no}"
                )
                new_relay = setup_relay(
                    app=app,
                    elm=elm,
                    asset_id=relay_data.asset_id,
                    plant_no=relay_data.plant_no,
                    ellipse_id=relay_data.ellipse_equip_no,
                    relay_class="ElmRelay",
                    network_level=network_level,
                )
                new_devices.append(new_relay)
            else:
                logger.debug(
                    f"Skipping {elm} as it is not a feeder CB in a sub: "
                    f"Data: {relay_data} "
                )

    # Reclosers
    try:
        recloser_info = recloser_dict[ecorp_id]
    except KeyError:
        # Elm does not have a relay associated with it,
        # check the other types
        pass
    else:
        if len(recloser_info) > 1:
            logger.info(f"Multiple Reclosers found for {elm}")

        # Handle potential for multiple fuses on one recloser
        for recloser_data in recloser_info:
            # Relay associated, ensure it exists
            if pot_feeder_cb:
                new_recloser = setup_relay(
                    app=app,
                    elm=elm,
                    asset_id=recloser_data.asset_id,
                    plant_no=recloser_data.plant_no,
                    ellipse_id=recloser_data.equip_no,
                    relay_class="ElmRelay",
                    network_level=network_level,
                )
                new_devices.append(new_recloser)
            else:
                logger.info(
                    f"Skipping {elm} as it is not a feeder CB in a sub: "
                    f"Data: {recloser_data} "
                )

    # Fuses
    try:
        fuse_info = fuse_dict[ecorp_id]
    except KeyError:
        # Elm does not have a fuse associated with it,
        # check the other types or do nothing
        pass
    else:
        if len(fuse_info) > 1:
            logger.info(f"Multiple Fuses found for {elm}")

        # Handle potential for multiple fuses on one switch
        for fuse_data in fuse_info:
            # Fuse associated, ensure it exists
            if pot_feeder_cb:
                new_fuse = setup_relay(
                    app=app,
                    elm=elm,
                    asset_id=fuse_data.asset_id,
                    plant_no=fuse_data.plant_no,
                    ellipse_id=fuse_data.equip_no,
                    relay_class="RelFuse",
                    network_level=network_level,
                )
                new_devices.append(new_fuse)
            else:
                logger.info(
                    f"Skipping {elm} as it is not a feeder CB in a sub: "
                    f"Data: {fuse_data} "
                )

    # Gas Switches
    try:
        gas_switch_info = gas_switch_dict[ecorp_id]
    except KeyError:
        # Elm does not have a Gas Switch associated with it,
        # check the other types or do nothing
        pass
    else:
        if len(gas_switch_info) > 1:
            logger.info(f"Multiple Gas Switches found for {elm}")

        # Handle potential for multiple Gas Switches on one switch
        for gas_switch_data in gas_switch_info:
            # Gas switch associated, ensure it exists
            if pot_feeder_cb:
                new_gs = setup_relay(
                    app=app,
                    elm=elm,
                    asset_id=gas_switch_data.asset_id,
                    plant_no=gas_switch_data.plant_no,
                    ellipse_id=gas_switch_data.equip_no,
                    relay_class="ElmRelay",
                    network_level=network_level,
                    gas_switch=True,
                )
                new_devices.append(new_gs)
            else:
                logger.info(
                    f"Skipping {elm} as it is not a feeder CB in a sub: "
                    f"Data: {gas_switch_data} "
                )

    return new_devices


def setup_relay(
    app,
    elm,
    asset_id,
    plant_no,
    ellipse_id,
    relay_class,
    network_level,
    gas_switch=False,
):
    """
    Find the existing relay or fuses,
    or create a new one with the minimum required parameters

    Delete incorrectly positioned relays
    """
    # Determine required skeleton params
    if not asset_id or not plant_no or not ellipse_id:
        logger.error(
            f"Do not have all of the required values: Planto_no: {plant_no} "
            f"asset_id: {asset_id}, ellipse: {ellipse_id}, relay_class: {relay_class}"
        )
        return
    try:
        plant_no = plant_no.strip()
    except AttributeError:
        plant_no = f"NoPlantNo_Ellipse_{ellipse_id}"
        logger.error(
            f"No Plant_no for {asset_id}, ellipse: {ellipse_id}, relay_class: {relay_class}"
        )
    if relay_class == "ElmRelay":
        expected_relay_foreign_key = f"ELMREL{int(ellipse_id)}"
    elif relay_class in ["ElmFuse", "RelFuse"]:
        expected_relay_foreign_key = f"RELFUS{int(ellipse_id)}"
    else:
        raise RuntimeError(
            f"Unhandled Class {relay_class} for "
            f"{elm}, {asset_id}, {ellipse_id}, {plant_no}"
        )

    # Check for an existing relay in the correct location
    root_cub, found_relays = determine_root_cub_relay_exists(
        elm,
        expected_relay_foreign_key=expected_relay_foreign_key,
        relay_class=relay_class,
        network_level=network_level,
    )
    if found_relays:
        relay_exists = False
        for relay in found_relays:
            # Delete any relay that does not match the new
            existing_name = relay.GetAttribute("loc_name")
            if plant_no not in existing_name:
                relay.Delete()
                continue
            try:
                int(existing_name.replace(plant_no, "")[0])
                relay.Delete()
            except (ValueError, IndexError):
                logger.debug(f"{relay} was found for {plant_no}")
                relay_exists = True
                found_relay = relay
        if relay_exists:
            return found_relay
    else:
        # Check for a relay associated somewhere else
        existing_relay = app.SearchObjectByForeignKey(expected_relay_foreign_key)
        if existing_relay:
            relay_parent = determine_existing_relay_switch(existing_relay)

            logger.warning(
                f"{existing_relay} was found based on "
                f"{expected_relay_foreign_key} with "
                f"{relay_parent}. \n"
                f"It was not associated with {elm}. "
                f"Deleting {existing_relay}"
            )
            ef = existing_relay.Delete()
            if ef:
                if not existing_relay.IsDeleted():
                    logger.error(f"Unable to delete {existing_relay}")
            else:
                logger.debug(f"Deleted {existing_relay}")
    if root_cub is None:
        logger.error(f"Unable to create a relay for {elm} as it has no cub0")
        return None
    # Build the new relay skeleton
    new_relay = root_cub.CreateObject(relay_class, plant_no)
    try:
        new_relay.SetAttribute("for_name", expected_relay_foreign_key)
    except AttributeError:
        # Handle a relay that already has a second foreign key
        existing_relay = app.SearchObjectByForeignKey(expected_relay_foreign_key)
        if existing_relay:
            existing_relay_parent = determine_existing_relay_switch(existing_relay)
            if not existing_relay_parent:
                logger.debug(
                    f"{existing_relay} is not associated with a switch, deleting it"
                )
                existing_relay.SetAttribute("for_name", "")
                ef = existing_relay.Delete()
                try:
                    new_relay.SetAttribute("for_name", expected_relay_foreign_key)
                except AttributeError:
                    logger.error(
                        f"Still unable to set {new_relay} foreign key to "
                        f"{expected_relay_foreign_key}"
                    )
        else:
            logger.error(
                f"Unable to set {new_relay} to foreign key "
                f"{expected_relay_foreign_key} however there is no relay named "
                f"after it. Manually fix relay foreign key."
            )
    new_relay.SetAttribute("dat_src", DATA_SOURCE_STRING)
    if gas_switch:
        new_relay.SetAttribute("chr_name", GAS_SWITCH_STRING)
    new_relay.SetAttribute("outserv", 1)

    logger.info(f"Added {new_relay} for {elm} based on {asset_id}")
    return new_relay


def determine_root_cub_relay_exists(
    elm, expected_relay_foreign_key, relay_class, network_level
):
    """
    Return the root Cubicle where the relay should be put and if
    the relay already exists associated with elm.

    Mode differences:
        * Distribution handles ElmCoup and StaSwitch only, and returns
          ALL relays of the requested class found in the element's
          cubicles (the caller purges near-duplicates by plant number).
        * Subtransmission additionally handles ElmLne, ElmTr2 and
          ElmTerm, and filters the found relays to those whose
          ``for_name`` matches the expected foreign key, so multiple
          protection schemes on one CB are not deleted.
    """
    class_name = elm.GetClassName()

    if network_level == NETWORK_SUBTRANSMISSION:
        cubicle_classes = ["ElmCoup", "ElmLne", "ElmTr2"]
    else:
        cubicle_classes = ["ElmCoup"]

    if class_name in cubicle_classes:
        # Get the 0ths cubicle for returning
        root_cub = elm.GetCubicle(0)
        # Check for an existing relay or fuse
        cubs = [elm.GetCubicle(i) for i in range(elm.GetConnectionCount())]
        cubs = [c for c in cubs if c]

        relays = list()
        for cub in cubs:
            cub_relays = cub.GetContents(f"*.{relay_class}")
            relays.extend(cub_relays)

    elif class_name == "StaSwitch":
        # Get the cubicle to put the relay in
        root_cub = elm.GetParent()
        # Check for an existing relay or fuse.
        relays = root_cub.GetContents(f"*.{relay_class}")

    elif class_name == "ElmTerm" and network_level == NETWORK_SUBTRANSMISSION:
        # Check for cubicles on the terminal; relay goes in the first.
        cubs = elm.GetContents("*.StaCubic")
        cubs = [c for c in cubs if c]
        if not cubs:
            logger.error(f"{elm} has no cubicles; cannot place a relay")
            return None, []
        root_cub = cubs[0]

        # Check for an existing relay or fuse
        relays = list()
        for cub in cubs:
            cub_relays = cub.GetContents(f"*.{relay_class}")
            relays.extend(cub_relays)

    else:
        raise RuntimeError(f"{elm} is not of a handled classname")

    if network_level == NETWORK_SUBTRANSMISSION:
        # Only treat relays with the exact expected foreign key as
        # "found" so co-located schemes are preserved.
        relays = [
            relay
            for relay in relays
            if relay.GetAttribute("for_name") == expected_relay_foreign_key
        ]

    return root_cub, relays


def ellipse_ecorp_asset_id_extraction(foreign_key: str, network_level: str):
    """
    Get the ellipse or ecorp asset id from the foreign key. Takes a
    string and returns a string of 2 or more numbers in length or None.

    Note that because the ellipse asset id is truncated it cannot get
    ellipse ids 000000000020 and below. This should not be an issue.

    Mode differences:
        * Subtransmission strips a leading prefix such as "001:"
          (everything up to and including the first colon) before
          decoding, and returns the extracted id unpadded.
        * Distribution does no prefix stripping and pads the result to
          12 characters with leading zeros (ecorp asset-id format).
    """
    # The remove list only needs to be those that end in a number
    remove_list = [
        "ELMTR2",
        "ELMTR3",
        "ELMTR4",
        "BNDTR2",
        "BNDTR3",
        "BNDTR4",
    ]

    if foreign_key is None:
        return None
    elif not isinstance(foreign_key, str):
        raise ValueError(
            "Unable to determine ecorp_id from the foreign key {} "
            "which is a {} not a string".format(foreign_key, type(foreign_key))
        )

    if network_level == NETWORK_SUBTRANSMISSION:
        # Strip a leading prefix such as "001:" (everything up to and
        # including the first colon) before decoding.
        if ":" in foreign_key:
            foreign_key = foreign_key.split(":", 1)[1]

    for delete_string in remove_list:
        foreign_key = foreign_key.replace(delete_string, "")

    p = re.compile(r"[\d]{2}[\d]*")

    s = p.search(foreign_key)
    if s:
        if network_level == NETWORK_DISTRIBUTION:
            return s.group().rjust(12, "0")
        return s.group()
    else:
        return None


def determine_existing_relay_switch(existing_relay):
    """
    Determine the StaSwitch/ElmCoups associated with the existing relay
    Designed for debugging and will produce:
        a list of StaSwitches,
        or a single ElmCoup or StaSwitch
    """

    # Get the relay's cub
    parent_cub = existing_relay.GetParent()

    # Check for same level within cub StaSwitches
    sta_switches = parent_cub.GetContents("*.StaSwitch")
    if sta_switches:
        if len(sta_switches) == 1:
            return sta_switches[0]
        else:
            return sta_switches

    # Get the Connected Branch Cubicle
    try:
        cub_obj = parent_cub.GetAttribute("obj_id")
    except AttributeError:
        cub_obj = None
    return cub_obj