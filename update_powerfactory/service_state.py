"""
Out-of-service bookkeeping for relays the transfer itself switches off.

IPStoPF takes a relay out of service for many reasons (orphan sweep,
mapping file not found, script failure, no CT, no protection settings,
open associated switch ...). The rule: a relay that was in service and
that the script switched off is put back in service once the script no
longer has a reason to switch it off; a relay that is out of service for
any other reason (off in the master model, switched off by hand) is never
touched.

How it works
------------
* When the script switches an in-service relay off, a one-line mark with
  the reason and date is written to the relay's Description (``desc``),
  e.g. ``IPStoPF-OOS 2026-11-12: Mapping file not found``. The mark
  travels with the derived copy and disappears when the copy is reset to
  a new base version.
* When a marked relay is processed again (it has an IPS match and reaches
  update_pf), it is put back in service before processing. If processing
  switches it off again it is re-marked with the current reason; if not,
  it stays in service and the mark is removed.
* A relay that is not processed (still an orphan, still not in IPS) keeps
  its mark and stays out of service.
* An unmarked out-of-service relay is left exactly as it is. To keep a
  marked relay out of service by hand, delete the mark line.
* Skeleton relays (dat_src "PRS") that are out of service are left to
  relay_settings._enable_configured_skeleton.

The mark text handling is pure and tested offline in
update_powerfactory/test_service_state.py.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, List, Optional

from logging_config import get_logger

logger = get_logger(__name__)

MARK_PREFIX = "IPStoPF-OOS"
# Longest reason kept in the mark, so the description line stays short.
_MAX_REASON = 70
DEFAULT_REASON = "switched out of service during the transfer"

# relay_settings.SKELETON_DATA_SOURCE (not imported: this module is also
# used from ips_data and must stay free of the relay_settings imports).
SKELETON_DATA_SOURCE = "PRS"


# =============================================================================
# Pure functions: the mark inside a description (list of lines)
# =============================================================================

def make_mark(reason: Optional[str], today: Optional[date] = None) -> str:
    """The description line recording a switch-off."""
    reason = " ".join(str(reason or DEFAULT_REASON).split())[:_MAX_REASON]
    stamp = (today or date.today()).isoformat()
    return f"{MARK_PREFIX} {stamp}: {reason}"


def find_mark(lines: Optional[List[str]]) -> Optional[str]:
    """The mark line in a description, or None."""
    for line in lines or []:
        if str(line).strip().startswith(MARK_PREFIX):
            return str(line).strip()
    return None


def mark_reason(mark: Optional[str]) -> str:
    """The reason part of a mark line ('' when there is none)."""
    if not mark or ":" not in mark:
        return ""
    return mark.split(":", 1)[1].strip()


def without_mark(lines: Optional[List[str]]) -> List[str]:
    """The description with every mark line removed; other lines kept."""
    return [
        line for line in (lines or [])
        if not str(line).strip().startswith(MARK_PREFIX)
    ]


def with_mark(lines: Optional[List[str]], mark: str) -> List[str]:
    """The description with exactly one mark line, placed last."""
    return without_mark(lines) + [mark]


# =============================================================================
# PowerFactory access
# =============================================================================

def _read_desc(pf_obj: Any) -> List[str]:
    """Description lines of a relay."""
    value = pf_obj.GetAttribute("desc")
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(line) for line in value]


def _set_mark(pf_obj: Any, reason: Optional[str]) -> None:
    pf_obj.SetAttribute(
        "desc", with_mark(_read_desc(pf_obj), make_mark(reason))
    )


def _clear_mark(pf_obj: Any) -> None:
    desc = _read_desc(pf_obj)
    if find_mark(desc):
        pf_obj.SetAttribute("desc", without_mark(desc))


def _name(pf_obj: Any) -> str:
    try:
        return str(pf_obj.loc_name)
    except Exception:  # noqa: BLE001
        return "<relay>"


def _is_relay(pf_obj: Any) -> bool:
    try:
        return pf_obj is not None and pf_obj.GetClassName() == "ElmRelay"
    except Exception:  # noqa: BLE001
        return False


def _out_of_service(pf_obj: Any) -> bool:
    return pf_obj.GetAttribute("outserv") == 1


def _is_skeleton(pf_obj: Any) -> bool:
    try:
        return pf_obj.GetAttribute("dat_src") == SKELETON_DATA_SOURCE
    except Exception:  # noqa: BLE001 - no data source: not a skeleton
        return False


# =============================================================================
# Counts for the per-transfer log line
# =============================================================================

@dataclass
class _Counts:
    marked: int = 0       # switched off in this run and marked
    restored: int = 0     # marked earlier, back in service now
    still_out: int = 0    # marked earlier, switched off again


_counts = _Counts()


def log_summary() -> None:
    """
    One line per transfer, then reset for the next project.

    Called at the end of update_pf, so the counts include the sweeps that
    ran before it (orphan sweep, unusable plant numbers). Nothing is
    logged when there was nothing to say.
    """
    global _counts
    c, _counts = _counts, _Counts()
    if c.marked or c.restored or c.still_out:
        logger.info(
            f"Service state: {c.marked} relay(s) switched out of service "
            f"and marked; of the relays marked by earlier runs, "
            f"{c.restored} returned to service and {c.still_out} switched "
            f"out again"
        )


# =============================================================================
# The two calls round a relay's processing, and the stand-alone switch-off
# =============================================================================

@dataclass
class Tracker:
    """State carried from begin() to finish() for one relay."""
    active: bool = False                  # False: finish() does nothing
    restored_from: Optional[str] = None   # reason of the mark lifted


def begin(pf_obj: Any) -> Tracker:
    """
    Call before a relay is processed.

    A relay that is out of service and carries the mark is put back in
    service (the mark stays until finish() settles the outcome). Returns
    the tracker to hand to finish(). Never raises.
    """
    if not _is_relay(pf_obj):
        return Tracker()
    try:
        if not _out_of_service(pf_obj):
            return Tracker(active=True)
        if _is_skeleton(pf_obj):
            return Tracker()
        mark = find_mark(_read_desc(pf_obj))
        if mark is None:
            # Out of service for some other reason: never touched.
            return Tracker()
        pf_obj.SetAttribute("outserv", 0)
        return Tracker(
            active=True, restored_from=mark_reason(mark) or DEFAULT_REASON
        )
    except Exception:  # noqa: BLE001 - bookkeeping must not stop a relay
        logger.warning(
            f"Service state: begin failed for {_name(pf_obj)}; relay left "
            f"as it is", exc_info=True,
        )
        return Tracker()


def finish(pf_obj: Any, tracker: Tracker, reason: Optional[str]) -> bool:
    """
    Call after a relay is processed (and after any later switch-off).

    Returns True when the relay's service state differs from what it was
    before begin(). Never raises. Must not be called for a deleted relay.
    """
    if not tracker.active:
        return False
    try:
        off_now = _out_of_service(pf_obj)
        if off_now:
            _set_mark(pf_obj, reason)
            if tracker.restored_from is None:
                _counts.marked += 1
                return True
            _counts.still_out += 1
            return False
        _clear_mark(pf_obj)
        if tracker.restored_from is None:
            return False
        _counts.restored += 1
        logger.info(
            f"{_name(pf_obj)}: returned to service; an earlier run had "
            f"switched it out ({tracker.restored_from}) and this run has "
            f"no reason to"
        )
        return True
    except Exception:  # noqa: BLE001 - bookkeeping must not stop a relay
        logger.warning(
            f"Service state: finish failed for {_name(pf_obj)}",
            exc_info=True,
        )
        return False


def switch_off(pf_obj: Any, reason: Optional[str]) -> None:
    """
    Take a relay out of service, marking it if it was in service.

    For the places that switch a relay off without processing it (relays
    with no IPS match: Energex orphan sweep, "not in IPS", unusable plant
    number). Use it in place of ``pf_obj.SetAttribute("outserv", 1)``. A
    relay that was already out of service gets no mark.
    """
    was_in_service = _is_relay(pf_obj) and not _out_of_service(pf_obj)
    pf_obj.SetAttribute("outserv", 1)
    if not was_in_service:
        return
    try:
        _set_mark(pf_obj, reason)
        _counts.marked += 1
    except Exception:  # noqa: BLE001 - bookkeeping must not stop a relay
        logger.warning(
            f"Service state: could not mark {_name(pf_obj)}", exc_info=True
        )
