"""
Subtransmission device-list builder.

Bridges the subtransmission matching pipeline (``mapping.reconcile`` ->
``ReconciliationResult``) to the existing PowerFactory update machinery
(``update_powerfactory.orchestrator.update_pf``), which consumes
``core.ProtectionDevice`` objects.

This is the subtransmission analogue of ``ips_data.ex_settings.ex_all_dev_list``:

* The distribution path matches a *switch* by name to IPS records
  (``_get_setting_id_indexed`` -> ``get_by_switch_name``) and then builds
  ``ProtectionDevice`` objects, deferring the relay's PF object to
  ``_assign_pf_objects``, which re-derives the cubicle from the switch.

* The subtransmission path has no meaningful switch names, so matching is done
  upstream by ``mapping.reconcile`` on the canonical ``MappingKey``. By the time
  we get here the cubicle is already resolved (``matched.pf.cubicle``). We build
  the *same* ``ProtectionDevice`` objects and find-or-create each relay directly
  in that cubicle.

The device-construction recipe (positional args, device-id cleaning, fuse
marking, naming) deliberately mirrors
``ips_data.ex_settings._create_device_from_record`` /
``ips_data.ex_settings._get_device_name`` /
``ips_data.ex_settings._find_or_create_pf_device`` so behaviour downstream of
``update_pf`` is identical to the proven distribution flow.

Detailed setting and CT/VT association reuses
``ips_data.query_database.batch_settings`` exactly as the distribution
orchestration (``ips_data.ips_settings.get_ips_settings``) does, so a single
batched fetch covers every matched setting ID.
"""

from typing import List, Set, Tuple

from core import ProtectionDevice
from ips_data import query_database as qd
from mapping.reconciliation import ReconciliationResult

# Subtransmission is an Energex (SEQ) dataset: Report-Cache-ProtectionSettingIDs-EX.
REGION = "Energex"


# =============================================================================
# Public entry point
# =============================================================================

def build_devices_from_reconciliation(
    app,
    result: ReconciliationResult,
) -> Tuple[List[str], List[ProtectionDevice]]:
    """Turn a reconciliation result into update-ready ``ProtectionDevice`` objects.

    For every matched element, every IPS setting at that element becomes one
    ``ProtectionDevice`` whose ``pf_obj`` is a relay found-or-created in the
    element's relay cubicle. Detailed settings and CT/VT attributes are then
    loaded in a single batched fetch and associated with each device.

    Args:
        app:    PowerFactory application object.
        result: Output of ``mapping.reconcile`` (its ``.matched`` list is used).

    Returns:
        ``(setting_ids, list_of_devices)`` - the same tuple shape that
        ``ips_data.ex_settings.ex_all_dev_list`` returns, ready to hand to
        ``update_powerfactory.orchestrator.update_pf``.
    """
    setting_ids, list_of_devices = _create_devices(app, result)

    # Single batched fetch of detailed settings + instrument-transformer rows,
    # mirroring ips_data.ips_settings.get_ips_settings (batch path).
    if setting_ids:
        app.PrintPlain(f"Fetching IPS settings for {len(setting_ids)} setting IDs")
        ips_settings, ips_it_settings = qd.batch_settings(
            app, REGION, called_function=True, set_ids=setting_ids
        )
        for device in list_of_devices:
            device.associated_settings(ips_settings)
            device.seq_instrument_attributes(ips_it_settings)

    return setting_ids, list_of_devices


# =============================================================================
# Device creation
# =============================================================================

def _create_devices(
    app,
    result: ReconciliationResult,
) -> Tuple[List[str], List[ProtectionDevice]]:
    """Create one ``ProtectionDevice`` per IPS setting at each matched element."""
    list_of_devices: List[ProtectionDevice] = []
    setting_ids: List[str] = []
    used_names: Set[str] = set()

    total = len(result.matched)
    for i, matched in enumerate(result.matched):
        if i % 10 == 0:
            app.PrintPlain(f"Building devices for matched element {i} of {total}")

        cubicle = matched.pf.cubicle
        if cubicle is None:
            # Offline reference (e.g. workbook source) has no live cubicle;
            # nothing to place a relay into. Should not occur on the live path.
            app.PrintWarn(
                f"No live cubicle for {matched.key.site_code} "
                f"{matched.key.designation} @ {matched.key.voltage_kv}kV - skipped"
            )
            continue

        for ips_dev in matched.ips_devices:
            device = _create_device(app, ips_dev, cubicle, used_names)
            if device is not None and device.pf_obj is not None:
                list_of_devices.append(device)
                setting_ids.append(ips_dev.setting_id)

    return setting_ids, list_of_devices


def _create_device(app, ips_dev, cubicle, used_names: Set[str]) -> ProtectionDevice:
    """Build a single ``ProtectionDevice`` and place its relay in ``cubicle``.

    Mirrors ``ex_settings._create_device_from_record``; the only difference is
    that the PF relay object is resolved here (the cubicle is already known)
    rather than deferred to a switch-driven ``_assign_pf_objects`` pass.
    """
    # Clean device id exactly as the distribution path does.
    device_id = ips_dev.device_id
    if device_id:
        for char in ": /,":
            device_id = device_id.replace(char, "")

    # core.ProtectionDevice.__init__ positional order:
    #   (app, device, name, setting_id, date, pf_obj, device_id)
    prot_dev = ProtectionDevice(
        app,
        ips_dev.pattern_name,        # device  -> relay pattern (drives type lookup)
        ips_dev.key.designation,     # name    -> operating designation (BB31/F3379/TR1...)
        ips_dev.setting_id,          # setting_id
        ips_dev.date_setting,        # date
        None,                        # pf_obj  -> assigned just below
        device_id,                   # device_id
    )
    # No meaningful switch in subtransmission. The orchestrator's OOS check
    # (_switch_relay_oos) reads device.switch.on_off inside try/except
    # AttributeError, so None is safe.
    prot_dev.switch = None
    prot_dev.seq_name = ips_dev.asset_name

    device_name = _relay_name(prot_dev, used_names)
    used_names.add(device_name)
    prot_dev.pf_obj = _find_or_create_relay(cubicle, device_name)

    if prot_dev.device and "fuse" in prot_dev.device.lower():
        prot_dev.fuse_type = "Line Fuse"

    return prot_dev


def _relay_name(device: ProtectionDevice, used_names: Set[str]) -> str:
    """PowerFactory loc_name for the relay. Mirrors ex_settings._get_device_name.

    NOTE (design decision): the relay is named from the operating designation
    plus the IPS device id, e.g. ``BB31_<deviceid>``. If subtransmission relays
    should follow a different naming convention in the model, change this one
    function - nothing else depends on the chosen string.
    """
    if not device.device_id:
        return f"{device.name}_{device.seq_name}".rstrip()

    base_name = f"{device.name}_{device.device_id}".rstrip()
    if base_name in used_names:
        return f"{device.name}_{device.device_id}_{device.seq_name}".rstrip()
    return base_name


def _find_or_create_relay(cubicle, device_name: str):
    """Find an existing relay/fuse named ``device_name`` in ``cubicle`` or create one.

    Cubicle-centric analogue of ``ex_settings._find_or_create_pf_device``: there
    the cubicle is derived from a switch; here it is supplied directly by the
    matched PowerFactory element.
    """
    if cubicle is None:
        return None

    contents = cubicle.GetContents(f"{device_name}.ElmRelay")
    contents += cubicle.GetContents(f"{device_name}.RelFuse")
    if contents:
        return contents[0]

    if "fuse" in device_name.lower():
        return cubicle.CreateObject("RelFuse", device_name)
    return cubicle.CreateObject("ElmRelay", device_name)