"""
Out-of-service bookkeeping for relays the transfer itself switches off.

IPStoPF takes a relay out of service for many reasons (orphan sweep,
mapping file not found, script failure, no CT, no protection settings,
open associated switch ...). Nothing put such a relay back, so on a derived
copy that keeps its state between runs the relay stayed off after the
cause was fixed (issues log: "a relay the script takes out of service is
never put back").

The rule (September out-of-service rule): a relay that was in service and
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
* An unmarked out-of-service relay is left exactly as it is.
* Skeleton relays (dat_src "PRS") that are out of service are left to
  relay_settings._enable_configured_skeleton, as before.

MODE
----
``"off"``    no marks, nothing restored (behaviour before this module).
``"log"``    marks are written; a marked relay that would be returned to
             service is logged and left out of service.
``"apply"``  marks are written and relays are returned to service.

Pure functions (mark text handling) are tested offline in
update_powerfactory/test_service_state.py; the PowerFactory access is
confined to _read_desc / _write_desc.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, List, Optional

from logging_config import get_logger

logger = get_logger(__name__)

# "off" | "log" | "apply" - see the module docstring.
MODE = "log"

MARK_PREFIX = "IPStoPF-OOS"
# Longest reason kept in the mark, so the description line stays short.
_MAX_REASON = 70
DEFAULT_REASON = "switched out of service during the transfer"

# relay_settings.SKELETON_DATA_SOURCE (not imported: this module is also
# used from ips_data and must stay free of the relay_settings imports).
# Skeleton relays are created out of service and put in service by
# relay_settings._enable_configured_skeleton; that rule keeps owning them.
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

# Set when the Description cannot be read or written, so one failure is
# reported once and the run carries on without marks.
_desc_unusable = False


def _read_desc(pf_obj: Any) -> Optional[List[str]]:
    """Description lines of a relay; None if they cannot be read."""
    global _desc_unusable
    if _desc_unusable:
        return None
    try:
        value = pf_obj.GetAttribute("desc")
    except Exception as err:  # noqa: BLE001 - bookkeeping must not stop a relay
        _desc_unusable = True
        logger.warning(
            f"Service state: cannot read the Description of "
            f"{_name(pf_obj)} ({err!r}); out-of-service marks are disabled "
            f"for the rest of this run"
        )
        return None
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(line) for line in value]


def _write_desc(pf_obj: Any, lines: List[str]) -> bool:
    """Write the description lines; False if PowerFactory refused."""
    global _desc_unusable
    if _desc_unusable:
        return False
    try:
        pf_obj.SetAttribute("desc", list(lines))
        return True
    except Exception as err:  # noqa: BLE001 - bookkeeping must not stop a relay
        _desc_unusable = True
        logger.warning(
            f"Service state: cannot write the Description of "
            f"{_name(pf_obj)} ({err!r}); out-of-service marks are disabled "
            f"for the rest of this run"
        )
        return False


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
# Run counters
# =============================================================================

@dataclass
class _Counts:
    marked: int = 0          # switched off in this run and marked
    restored: int = 0        # marked earlier, back in service now
    would_restore: int = 0   # log mode: would be back in service
    still_out: int = 0       # marked earlier, switched off again


_counts = _Counts()


def reset_counts() -> None:
    """Start counting for a new transfer (and re-arm the desc check)."""
    global _counts, _desc_unusable
    _counts = _Counts()
    _desc_unusable = False


def log_summary() -> None:
    """
    One line per transfer, then reset for the next project.

    Called at the end of update_pf, so the counts include the sweeps that
    ran before it (orphan sweep, unusable plant numbers). Nothing is
    logged when there was nothing to say.
    """
    c = _counts
    reset_counts()
    if MODE == "off" or not (c.marked or c.restored or c.would_restore
                             or c.still_out):
        return
    if MODE == "apply":
        back = f"{c.restored} returned to service"
    else:
        back = (f"{c.would_restore} would be returned to service "
                f"(log mode: left out)")
    logger.info(
        f"Service state: {c.marked} relay(s) switched out of service and "
        f"marked; of the relays marked by earlier runs, {back} and "
        f"{c.still_out} switched out again"
    )


# =============================================================================
# The two calls round a relay's processing, and the stand-alone switch-off
# =============================================================================

@dataclass
class Tracker:
    """State carried from begin() to finish() for one relay."""
    active: bool = False              # False: finish() does nothing
    was_in_service: bool = False      # in service before any restore
    restored_from: Optional[str] = None   # reason of the mark lifted


def begin(pf_obj: Any) -> Tracker:
    """
    Call before a relay is processed.

    A relay that is out of service and carries the mark is put back in
    service (the mark stays until finish() decides the outcome). Returns
    the tracker to hand to finish(). Never raises.
    """
    if MODE == "off" or not _is_relay(pf_obj):
        return Tracker()
    try:
        if not _out_of_service(pf_obj):
            return Tracker(active=True, was_in_service=True)
        if _is_skeleton(pf_obj):
            # Out-of-service skeleton relay: the skeleton rule decides.
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
        name = _name(pf_obj)

        if tracker.restored_from is None:
            # Was in service when processing started.
            if off_now:
                desc = _read_desc(pf_obj)
                if desc is not None and _write_desc(
                    pf_obj, with_mark(desc, make_mark(reason))
                ):
                    _counts.marked += 1
                return True
            # In service throughout: drop a stale mark, if any (a relay
            # put back in service by hand, or by the skeleton rule).
            desc = _read_desc(pf_obj)
            if desc and find_mark(desc):
                _write_desc(pf_obj, without_mark(desc))
            return False

        # Carried a mark and was put back in service by begin().
        desc = _read_desc(pf_obj) or []
        if off_now:
            _write_desc(pf_obj, with_mark(desc, make_mark(reason)))
            _counts.still_out += 1
            return False
        if MODE == "apply":
            _write_desc(pf_obj, without_mark(desc))
            _counts.restored += 1
            logger.info(
                f"{name}: returned to service; an earlier run had switched "
                f"it out ({tracker.restored_from}) and this run has no "
                f"reason to"
            )
            return True
        # Log mode: report, then put it back as it was (mark kept).
        pf_obj.SetAttribute("outserv", 1)
        _counts.would_restore += 1
        logger.info(
            f"{name}: would be returned to service (log mode, left out); "
            f"an earlier run had switched it out ({tracker.restored_from}) "
            f"and this run has no reason to"
        )
        return False
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
    with no IPS match: Energex orphan sweep, Ergon "not in IPS" and
    unusable plant number). Use it in place of
    ``pf_obj.SetAttribute("outserv", 1)``. The relay always ends out of
    service, whatever MODE is; only the mark depends on MODE. A relay
    that was already out of service gets no mark.
    """
    was_in_service = False
    try:
        was_in_service = not _out_of_service(pf_obj)
    except Exception:  # noqa: BLE001 - fall through to the switch-off
        pass
    pf_obj.SetAttribute("outserv", 1)
    if MODE == "off" or not was_in_service or not _is_relay(pf_obj):
        return
    try:
        desc = _read_desc(pf_obj)
        if desc is not None and _write_desc(
            pf_obj, with_mark(desc, make_mark(reason))
        ):
            _counts.marked += 1
    except Exception:  # noqa: BLE001 - bookkeeping must not stop a relay
        logger.warning(
            f"Service state: could not mark {_name(pf_obj)}", exc_info=True
        )
