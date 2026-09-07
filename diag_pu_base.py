"""
diag_pu_base.py - identify what makes PowerFactory re-derive the pu base
used to convert Ipset (pu) into Ipsetr (secondary A) and cpIpset (primary A).

BACKGROUND
----------
PowerFactory derives, at the moment e:Ipset is assigned:

    Ipsetr  = Ipset  x  <pu base>
    cpIpset = Ipsetr x  ptapset / stapset

and then stores both. It does not re-derive them when the CT or the
measurement element is corrected afterwards.

On the Cleveland project two orderings of relay_settings() have now been
observed:

  * settings written BEFORE update_ct  (4 Sep) -> 3 relays wrong
  * settings written AFTER  update_ct  (7 Sep) -> 57 relays wrong, x80 on
    the phase elements exactly (the 400/5 CT ratio); earth elements and all
    reclosers unaffected

In every wrong case the base used was the CT PRIMARY tap (e.g. 400) instead
of the secondary (5). This script measures the base directly, and then walks
a sequence of candidate "settling" actions to find which one restores it.

WHAT IT DOES
------------
PHASE A (measure): for each target relay, write a probe value to e:Ipset,
read back e:Ipsetr, and compute  base = Ipsetr / probe.  Then restore the
original Ipset. This tells you the CURRENT effective base per element.

PHASE B (sequence): on one relay, perform candidate settling actions one at
a time, re-measuring the base after each. The FIRST action after which the
base reads correctly is the settling trigger, and dictates the permanent fix.

RUN THIS ON A COPY OF THE PROJECT
---------------------------------
The script mutates the model: it re-writes Ipset (which re-derives the stored
values), re-assigns pdiselm, re-writes CT taps and RelMeasure Inom, and
optionally deactivates/reactivates the project. Restoring Ipset to its
original value does NOT restore the previously stored (wrong) derived values
- it re-derives them against whatever base is current. So a device can only
be measured in its "broken" state ONCE. Work on a throwaway copy.

USAGE
-----
Add as a ComPython object in the active study case and execute, or run from
an external Python session that has already attached to PowerFactory.
Configure via the MODE dict below - no command line arguments.
"""

import datetime
import logging
import os
import sys

# =============================================================================
# Configuration
# =============================================================================

MODE = {
    # --- what to run -------------------------------------------------------
    "MEASURE_BASE": True,      # Phase A: report current base for every target
    "RUN_SEQUENCE": True,      # Phase B: walk the settling actions

    # --- Phase A targets ---------------------------------------------------
    # RBY13_J36      : correct on 4 Sep, broken on 7 Sep  (the key probe)
    # VPT13A+B_J37A  : broken in both runs
    # RBY13_J46      : correct in both runs (control)
    # X867537_RC10   : recloser, correct in both runs (control, early-return
    #                  branch of update_ct - never reaches Inom write)
    "TARGETS": [
        "RBY18_J36",       # broken in this model - the key probe
        "RBY7_J36",        # broken in this model
        "VPT13A+B_J37A",   # broken in this model (see caveat below)
        "RBY13_J36",       # correct in this model - control
        "X867537_RC10",    # recloser control - early-return branch of update_ct
    ],

    # --- Phase B subject ---------------------------------------------------
    # Use a relay that is currently WRONG, or the sequence proves nothing.
    "SEQUENCE_RELAY": "RBY18_J36",

    # Steps to execute, in order. Comment out to skip. Each is applied on top
    # of the previous ones.
    "SEQUENCE_STEPS": [
        "baseline",          # no action - what is the base right now?
        "reassign_pdiselm",  # re-write the slot list with its current value
        "rewrite_ct_taps",   # re-write ptapset / stapset with current values
        "rewrite_inom",      # write RelMeasure Inom = CT secondary
        "write_changes",     # app.WriteChangesToDb()
        "reactivate",        # deactivate + reactivate the project (heavy)
    ],

    # Wrap the mutating steps in the write cache, to reproduce the conditions
    # inside update_pf(). Set False to test whether the cache is implicated.
    "EMULATE_WRITE_CACHE": True,

    # --- probe -------------------------------------------------------------
    # Probe value written to Ipset to measure the base. 1.0 makes the read
    # trivial: Ipsetr == base. Avoid 0.
    "PROBE_IPSET": 1.0,

    # --- output ------------------------------------------------------------
    "LOG_DIR": r"C:\LocalData",
    "LOG_NAME": "diag_pu_base",
}

# Element classes that carry a pu pickup and its derived values.
PICKUP_CLASSES = ("RelToc", "RelIoc", "RelFmeas")

# Attributes that make up the derivation chain.
ATTR_IPSET = "e:Ipset"
ATTR_IPSETR = "e:Ipsetr"
ATTR_CPIPSET = "e:cpIpset"


# =============================================================================
# Logging / output
# =============================================================================

logger = logging.getLogger("diag_pu_base")


def _setup_logging():
    """File + stream logging. PrintPlain is unreliable in engine mode."""
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = None
    try:
        os.makedirs(MODE["LOG_DIR"], exist_ok=True)
        path = os.path.join(MODE["LOG_DIR"], "{}_{}.log".format(MODE["LOG_NAME"], stamp))
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
    except OSError as exc:
        print("Could not open log file: {}".format(exc))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(sh)
    return path


def _out(app, message):
    """Log, and mirror to the PF output window when running in the GUI."""
    logger.info(message)
    try:
        app.PrintPlain(message)
    except AttributeError:
        pass


# =============================================================================
# PF helpers - every one log-and-skip, never raise
# =============================================================================

def _get_app():
    """Return the PowerFactory application object, or None."""
    try:
        import powerfactory
    except ImportError as exc:
        logger.error("powerfactory module not importable: {}".format(exc))
        return None
    try:
        return powerfactory.GetApplication()
    except Exception as exc:
        logger.error("GetApplication failed: {}".format(exc))
        return None


def _find_relay(app, name):
    """Resolve an ElmRelay by loc_name. Returns the object or None."""
    try:
        relays = app.GetCalcRelevantObjects("*.ElmRelay")
    except Exception as exc:
        logger.warning("GetCalcRelevantObjects failed: {}".format(exc))
        return None
    for relay in relays:
        try:
            if relay.loc_name == name:
                return relay
        except AttributeError:
            continue
    logger.warning("Relay '{}' not found among {} calc-relevant relays".format(
        name, len(relays)))
    return None


def _pickup_elements(relay):
    """Every child element carrying Ipset / Ipsetr / cpIpset."""
    found = []
    try:
        children = relay.GetContents("*", 1)
    except Exception as exc:
        logger.warning("{}: GetContents failed: {}".format(relay.loc_name, exc))
        return found
    for child in children:
        try:
            if child.GetClassName() not in PICKUP_CLASSES:
                continue
            child.GetAttribute(ATTR_IPSET)
            child.GetAttribute(ATTR_IPSETR)
        except (AttributeError, RuntimeError):
            continue
        found.append(child)
    return found


def _relay_ct(relay):
    """The StaCt sitting in one of the relay's slots, or None."""
    try:
        slots = relay.GetAttribute("pdiselm")
    except (AttributeError, RuntimeError) as exc:
        logger.warning("{}: could not read pdiselm: {}".format(relay.loc_name, exc))
        return None
    for obj in slots or []:
        try:
            if obj is not None and obj.GetClassName() == "StaCt":
                return obj
        except AttributeError:
            continue
    return None


def _ct_taps(ct):
    """(ptapset, stapset) or (None, None)."""
    if ct is None:
        return None, None
    try:
        return ct.GetAttribute("e:ptapset"), ct.GetAttribute("e:stapset")
    except (AttributeError, RuntimeError) as exc:
        logger.warning("CT tap read failed: {}".format(exc))
        return None, None


def _measure_base(element, probe):
    """
    Write a probe Ipset, read the derived Ipsetr/cpIpset, restore Ipset.

    Returns a dict with the original value, the measured base
    (Ipsetr / probe) and the measured ratio (cpIpset / Ipsetr).
    """
    out = {
        "element": getattr(element, "loc_name", "?"),
        "orig_ipset": None,
        "base": None,
        "ratio": None,
        "error": None,
    }
    try:
        out["orig_ipset"] = element.GetAttribute(ATTR_IPSET)
    except (AttributeError, RuntimeError) as exc:
        out["error"] = "read Ipset: {}".format(exc)
        return out

    try:
        element.SetAttribute(ATTR_IPSET, probe)
        ipsetr = element.GetAttribute(ATTR_IPSETR)
        try:
            cpipset = element.GetAttribute(ATTR_CPIPSET)
        except (AttributeError, RuntimeError):
            cpipset = None
        if ipsetr is not None and probe:
            out["base"] = ipsetr / probe
        if cpipset is not None and ipsetr:
            out["ratio"] = cpipset / ipsetr
    except (AttributeError, RuntimeError) as exc:
        out["error"] = "probe write: {}".format(exc)
    finally:
        # Restore even if the probe failed part way.
        try:
            if out["orig_ipset"] is not None:
                element.SetAttribute(ATTR_IPSET, out["orig_ipset"])
        except (AttributeError, RuntimeError) as exc:
            out["error"] = "{} | restore failed: {}".format(out["error"], exc)
    return out


# =============================================================================
# Phase A - measure the current base
# =============================================================================

def measure_relay(app, name, probe):
    """Report the effective pu base for every pickup element on one relay."""
    relay = _find_relay(app, name)
    if relay is None:
        return

    ct = _relay_ct(relay)
    ptap, stap = _ct_taps(ct)
    ct_name = getattr(ct, "loc_name", None) if ct else None

    inoms = []
    try:
        for meas in relay.GetContents("*.RelMeasure", 1):
            try:
                inoms.append((meas.loc_name, meas.GetAttribute("e:Inom")))
            except (AttributeError, RuntimeError):
                inoms.append((meas.loc_name, "unreadable"))
    except Exception as exc:
        logger.warning("{}: RelMeasure scan failed: {}".format(name, exc))

    _out(app, "")
    _out(app, "=== {} ===".format(name))
    _out(app, "  CT: {}  ptapset={}  stapset={}".format(ct_name, ptap, stap))
    _out(app, "  RelMeasure Inom: {}".format(inoms if inoms else "none found"))

    elements = _pickup_elements(relay)
    if not elements:
        _out(app, "  no pickup elements found")
        return

    for element in elements:
        res = _measure_base(element, probe)
        verdict = ""
        if res["base"] is not None and stap:
            if abs(res["base"] - stap) < 0.01:
                verdict = "  <- base OK (= CT secondary)"
            elif ptap and abs(res["base"] - ptap) < 0.01:
                verdict = "  <- BASE IS CT PRIMARY (the bug)"
            else:
                verdict = "  <- base unexpected"
        _out(app, "  {:<28} Ipset={:<10} base={:<10} ratio={}{}".format(
            res["element"],
            res["orig_ipset"],
            round(res["base"], 4) if res["base"] is not None else "?",
            round(res["ratio"], 4) if res["ratio"] is not None else "?",
            verdict,
        ))
        if res["error"]:
            _out(app, "      error: {}".format(res["error"]))


# =============================================================================
# Phase B - candidate settling actions
# =============================================================================

def _step_baseline(app, relay):
    return relay, "no action"


def _step_reassign_pdiselm(app, relay):
    try:
        slots = relay.GetAttribute("pdiselm")
        relay.SetAttribute("pdiselm", slots)
        return relay, "pdiselm re-assigned ({} slots)".format(len(slots or []))
    except (AttributeError, RuntimeError) as exc:
        return relay, "pdiselm re-assign FAILED: {}".format(exc)


def _step_rewrite_ct_taps(app, relay):
    ct = _relay_ct(relay)
    if ct is None:
        return relay, "no CT in slots - skipped"
    ptap, stap = _ct_taps(ct)
    if ptap is None or stap is None:
        return relay, "taps unreadable - skipped"
    try:
        ct.SetAttribute("e:ptapset", ptap)
        ct.SetAttribute("e:stapset", stap)
        return relay, "CT taps re-written ({}/{})".format(ptap, stap)
    except (AttributeError, RuntimeError) as exc:
        return relay, "CT tap write FAILED: {}".format(exc)


def _step_rewrite_inom(app, relay):
    ct = _relay_ct(relay)
    _, stap = _ct_taps(ct)
    if stap is None:
        return relay, "no CT secondary - skipped"
    written = 0
    try:
        for meas in relay.GetContents("*.RelMeasure", 1):
            try:
                meas.SetAttribute("e:Inom", stap)
                written += 1
            except (AttributeError, RuntimeError) as exc:
                logger.warning("Inom write failed on {}: {}".format(
                    meas.loc_name, exc))
    except Exception as exc:
        return relay, "RelMeasure scan FAILED: {}".format(exc)
    return relay, "Inom={} written to {} RelMeasure element(s)".format(stap, written)


def _step_write_changes(app, relay):
    try:
        app.WriteChangesToDb()
        return relay, "WriteChangesToDb() called"
    except Exception as exc:
        return relay, "WriteChangesToDb FAILED: {}".format(exc)


def _step_reactivate(app, relay):
    """Deactivate + reactivate. Invalidates all object handles - re-resolve."""
    name = relay.loc_name
    try:
        project = app.GetActiveProject()
        if project is None:
            return relay, "no active project - skipped"
        project.Deactivate()
        project.Activate()
    except Exception as exc:
        return relay, "reactivate FAILED: {}".format(exc)
    fresh = _find_relay(app, name)
    if fresh is None:
        return relay, "reactivated but relay could not be re-resolved"
    return fresh, "project deactivated and reactivated; relay re-resolved"


STEP_FUNCS = {
    "baseline": _step_baseline,
    "reassign_pdiselm": _step_reassign_pdiselm,
    "rewrite_ct_taps": _step_rewrite_ct_taps,
    "rewrite_inom": _step_rewrite_inom,
    "write_changes": _step_write_changes,
    "reactivate": _step_reactivate,
}


def run_sequence(app, name, steps, probe, emulate_cache):
    """Walk the settling actions, measuring the base after each one."""
    relay = _find_relay(app, name)
    if relay is None:
        _out(app, "Sequence relay '{}' not found - sequence skipped".format(name))
        return

    _out(app, "")
    _out(app, "#" * 70)
    _out(app, "SEQUENCE on {} (write cache emulation: {})".format(
        name, "ON" if emulate_cache else "OFF"))
    _out(app, "#" * 70)

    cache_was_on = False
    if emulate_cache:
        try:
            cache_was_on = bool(app.IsWriteCacheEnabled())
            if not cache_was_on:
                app.SetWriteCacheEnabled(1)
        except Exception as exc:
            logger.warning("Could not enable write cache: {}".format(exc))

    try:
        for step in steps:
            func = STEP_FUNCS.get(step)
            if func is None:
                _out(app, "Unknown step '{}' - skipped".format(step))
                continue
            relay, note = func(app, relay)
            _out(app, "")
            _out(app, "--- step: {} -> {}".format(step, note))

            ct = _relay_ct(relay)
            ptap, stap = _ct_taps(ct)
            for element in _pickup_elements(relay):
                res = _measure_base(element, probe)
                verdict = ""
                if res["base"] is not None and stap:
                    if abs(res["base"] - stap) < 0.01:
                        verdict = "  <- SETTLED"
                    elif ptap and abs(res["base"] - ptap) < 0.01:
                        verdict = "  <- still CT primary"
                _out(app, "    {:<28} base={:<10} ratio={}{}".format(
                    res["element"],
                    round(res["base"], 4) if res["base"] is not None else "?",
                    round(res["ratio"], 4) if res["ratio"] is not None else "?",
                    verdict,
                ))
    finally:
        if emulate_cache and not cache_was_on:
            try:
                app.WriteChangesToDb()
                app.SetWriteCacheEnabled(0)
            except Exception as exc:
                logger.warning("Write cache teardown failed: {}".format(exc))


# =============================================================================
# Entry point
# =============================================================================

def main():
    log_path = _setup_logging()
    app = _get_app()
    if app is None:
        logger.error("No PowerFactory application - aborting")
        return

    _out(app, "diag_pu_base starting")
    _out(app, "module: {}".format(os.path.abspath(__file__)))
    if log_path:
        _out(app, "log file: {}".format(log_path))
    try:
        project = app.GetActiveProject()
        _out(app, "active project: {}".format(
            project.loc_name if project else "NONE"))
    except Exception as exc:
        logger.warning("Could not read active project: {}".format(exc))
    _out(app, "RUN THIS ON A COPY - the model is mutated.")

    probe = MODE["PROBE_IPSET"]

    if MODE["MEASURE_BASE"]:
        _out(app, "")
        _out(app, "#" * 70)
        _out(app, "PHASE A - current pu base per element")
        _out(app, "#" * 70)
        for name in MODE["TARGETS"]:
            measure_relay(app, name, probe)

    if MODE["RUN_SEQUENCE"]:
        run_sequence(
            app,
            MODE["SEQUENCE_RELAY"],
            MODE["SEQUENCE_STEPS"],
            probe,
            MODE["EMULATE_WRITE_CACHE"],
        )

    _out(app, "")
    _out(app, "diag_pu_base finished")


if __name__ == "__main__":
    main()