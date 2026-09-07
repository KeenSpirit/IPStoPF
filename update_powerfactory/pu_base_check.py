"""
Re-derive pu pickup values that PowerFactory converted against a stale base.

THE PROBLEM
-----------
PowerFactory derives, at the instant e:Ipset is assigned:

    Ipsetr  = Ipset  x  <pu base>          (secondary amps)
    cpIpset = Ipsetr x  ptapset / stapset  (primary amps)

and then STORES both. It does not re-derive them if the CT link, the CT
taps or the measurement element's Inom change afterwards.

During an IPStoPF run some relays are momentarily in a state where the pu
base reads as the CT PRIMARY tap instead of the secondary. Any Ipset
written in that window is stored with one extra CT-ratio factor:

    correct:   cpIpset = Ipset x ptapset
    corrupted: cpIpset = Ipset x ptapset x (ptapset / stapset)

The transient does not survive the run. Diagnostic diag_pu_base.py,
executed against the 4 September Cleveland copy, measured base = 5.0 (the
CT secondary) on RBY18_J36, RBY7_J36 and VPT13A+B_J37A - all three of
which carried corrupted stored values from that run. Re-assigning Ipset
after the fact therefore re-derives correctly.

Reordering relay_settings() cannot fix this: the 4 September order
(settings before update_ct) corrupted 3 relays, the 7 September order
(update_ct before settings) corrupted 57. This module repairs the stored
values instead, after the device loop has finished.

WHAT IT DOES
------------
For every pickup element on every relay processed in the run:

  * compute expected = Ipset x ptapset  (the pu base cancels out, so this
    holds regardless of what the base happened to be)
  * if the stored cpIpset matches, do nothing
  * if it is out by exactly ptapset / stapset, re-write Ipset with its own
    current value, which forces PowerFactory to re-derive both values
    against the now-correct base, then verify
  * if it is out by any other factor, report it and DO NOT touch it

That last branch matters. "Minimum Current Multiplier" elements are
referenced to their parent element's pickup, not to the CT (diag_pu_base
measured bases of 30, 200 and 300 on X867537_RC10), so a plain
cpIpset == Ipset x ptapset test would flag them as broken and corrupt
them. Requiring the discrepancy to equal the CT ratio excludes them.

USAGE
-----
    from update_powerfactory import pu_base_check as pbc

    summary = pbc.finalise_pu_derivations(app, lst_of_devs)

Call it after the device loop and after the write cache has been flushed.
Never raises: every failure is logged and skipped, so a bad element cannot
end a project or the fleet run.
"""

from typing import Any, Dict, List, Optional, Tuple

from logging_config import get_logger

logger = get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Element classes that carry a pu pickup and its derived values.
PICKUP_CLASSES: Tuple[str, ...] = ("RelToc", "RelIoc", "RelFmeas")

# Relative tolerance for both the consistency test and the ratio-signature
# test. Ipset is stored as a float32, so exact equality is not available.
_TOL = 0.01

# Below this the element is either disabled or a fallback value; a relative
# comparison against it is meaningless.
_MIN_IPSET = 1e-9

ATTR_IPSET = "e:Ipset"
ATTR_CPIPSET = "e:cpIpset"


# =============================================================================
# PF helpers - all log-and-skip, none raise
# =============================================================================

def _relay_ct(relay: Any) -> Optional[Any]:
    """The StaCt occupying one of the relay's slots, or None."""
    try:
        slots = relay.GetAttribute("pdiselm")
    except (AttributeError, RuntimeError) as exc:
        logger.warning(f"{_name(relay)}: could not read pdiselm ({exc})")
        return None
    for obj in slots or []:
        try:
            if obj is not None and obj.GetClassName() == "StaCt":
                return obj
        except AttributeError:
            continue
    return None


def _ct_taps(ct: Any) -> Tuple[Optional[float], Optional[float]]:
    """(ptapset, stapset), or (None, None) if unreadable."""
    if ct is None:
        return None, None
    try:
        return ct.GetAttribute("e:ptapset"), ct.GetAttribute("e:stapset")
    except (AttributeError, RuntimeError) as exc:
        logger.warning(f"{_name(ct)}: CT taps unreadable ({exc})")
        return None, None


def _pickup_elements(relay: Any) -> List[Any]:
    """Every child element exposing both Ipset and cpIpset."""
    found: List[Any] = []
    try:
        children = relay.GetContents("*", 1)
    except Exception as exc:
        logger.warning(f"{_name(relay)}: GetContents failed ({exc})")
        return found
    for child in children:
        try:
            if child.GetClassName() not in PICKUP_CLASSES:
                continue
            child.GetAttribute(ATTR_IPSET)
            child.GetAttribute(ATTR_CPIPSET)
        except (AttributeError, RuntimeError):
            # Element of the right class but without the derived-value
            # chain; nothing to check.
            continue
        found.append(child)
    return found


def _name(obj: Any) -> str:
    return getattr(obj, "loc_name", "<unnamed>")


def _close(a: float, b: float, tol: float = _TOL) -> bool:
    """Relative comparison, safe when b is zero."""
    if b == 0:
        return abs(a) < tol
    return abs(a - b) / abs(b) < tol


# =============================================================================
# Per-element check
# =============================================================================

def _check_element(
    element: Any,
    relay_name: str,
    ptap: float,
    stap: float,
    ct_ratio: float,
    summary: Dict[str, int],
) -> None:
    """Verify one element's stored derivation and repair it if corrupted."""
    try:
        ipset = element.GetAttribute(ATTR_IPSET)
        cpipset = element.GetAttribute(ATTR_CPIPSET)
    except (AttributeError, RuntimeError) as exc:
        logger.warning(
            f"{relay_name}/{_name(element)}: derived values unreadable ({exc})"
        )
        summary["skipped"] += 1
        return

    if ipset is None or cpipset is None or abs(ipset) < _MIN_IPSET:
        summary["skipped"] += 1
        return

    expected = ipset * ptap
    if _close(cpipset, expected):
        summary["ok"] += 1
        return

    # Inconsistent. Only act when the discrepancy is exactly the CT ratio -
    # the signature of a pu conversion performed against the CT primary.
    # Anything else is a differently-referenced element (e.g. the
    # "Minimum Current Multiplier" elements, which are referenced to their
    # parent pickup) and must be left alone.
    factor = cpipset / expected if expected else None
    if factor is None or not _close(factor, ct_ratio):
        logger.warning(
            f"{relay_name}/{_name(element)}: cpIpset {cpipset:g} A does not "
            f"match Ipset {ipset:g} x CT primary {ptap:g} A "
            f"(expected {expected:g} A, out by "
            f"{'x%g' % factor if factor else 'an unknown factor'}); this is "
            f"not the stale-base signature (x{ct_ratio:g}) so it was left "
            f"unchanged"
        )
        summary["unexpected"] += 1
        return

    # Re-assigning Ipset forces PowerFactory to re-derive Ipsetr and cpIpset
    # against the current base.
    try:
        element.SetAttribute(ATTR_IPSET, ipset)
        new_cpipset = element.GetAttribute(ATTR_CPIPSET)
    except (AttributeError, RuntimeError) as exc:
        logger.error(
            f"{relay_name}/{_name(element)}: re-derivation failed ({exc}); "
            f"cpIpset left at {cpipset:g} A instead of {expected:g} A"
        )
        summary["failed"] += 1
        return

    if _close(new_cpipset, expected):
        logger.warning(
            f"{relay_name}/{_name(element)}: cpIpset corrected from "
            f"{cpipset:g} A to {new_cpipset:g} A (Ipset {ipset:g} pu, CT "
            f"{ptap:g}/{stap:g}); the original write used the CT primary as "
            f"the pu base"
        )
        summary["corrected"] += 1
    else:
        # The base is still wrong at this point in the run. Loud, because
        # the model now carries a wrong protection setting.
        logger.error(
            f"{relay_name}/{_name(element)}: re-derivation did NOT correct "
            f"cpIpset (still {new_cpipset:g} A, expected {expected:g} A). The "
            f"pu base is still stale at this point in the run; the finalise "
            f"pass may need to run after a project reactivation."
        )
        summary["failed"] += 1


# =============================================================================
# Entry point
# =============================================================================

def finalise_pu_derivations(app, device_list: List[Any]) -> Dict[str, int]:
    """
    Repair pu pickups stored against a stale conversion base.

    Args:
        app: PowerFactory application object (unused today, kept so the
            signature matches the rest of update_powerfactory and so a
            future reactivation step can be added without a call-site
            change).
        device_list: The ProtectionDevice objects processed by this run.
            Only entries whose pf_obj is an ElmRelay are examined - this
            deliberately avoids a topology walk over the whole model.

    Returns:
        Summary counts: relays, elements, ok, corrected, unexpected,
        failed, skipped.
    """
    summary = {
        "relays": 0,
        "elements": 0,
        "ok": 0,
        "corrected": 0,
        "unexpected": 0,
        "failed": 0,
        "skipped": 0,
    }

    for device_object in device_list:
        relay = getattr(device_object, "pf_obj", None)
        if relay is None:
            continue
        try:
            if relay.GetClassName() != "ElmRelay":
                continue
        except AttributeError:
            continue

        relay_name = _name(relay)
        ptap, stap = _ct_taps(_relay_ct(relay))

        if not ptap or not stap:
            # No CT in the slots (update_ct's "No CT Linked" branch) or the
            # taps could not be read. Nothing to verify against.
            summary["skipped"] += 1
            continue

        ct_ratio = ptap / stap
        if _close(ct_ratio, 1.0):
            # 1/1 CTs (reclosers) cannot exhibit the fault: the corrupted
            # and correct values are identical.
            continue

        summary["relays"] += 1
        for element in _pickup_elements(relay):
            summary["elements"] += 1
            _check_element(element, relay_name, ptap, stap, ct_ratio, summary)

    logger.info(
        f"pu base finalisation: {summary['relays']} relay(s), "
        f"{summary['elements']} element(s) checked, "
        f"{summary['corrected']} corrected, {summary['ok']} already correct, "
        f"{summary['unexpected']} inconsistent but not the stale-base "
        f"signature, {summary['failed']} failed, {summary['skipped']} skipped"
    )
    return summary