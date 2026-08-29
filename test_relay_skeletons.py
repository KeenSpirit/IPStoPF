"""
test_relay_skeletons.py - standalone diagnostic for the relay-skeleton pass.

Purpose
-------
Answer, in order, the three questions raised by the NetDash ConnectionError
seen in the 22/08 batch log:

    1. Do the four GISEP/Ellipse reports that ``add_relay_skeletons`` depends
       on actually return rows right now, or is ``get_cached_data`` silently
       returning empty / stale data after the NetDash failure?
    2. If rows come back, do their keys JOIN to the ``for_name`` foreign keys
       on the elements in the open project? (A populated report that produces
       zero matches looks identical, downstream, to an empty report.)
    3. Only then: does a live ``add_relay_skeletons`` pass actually create
       relay objects in the model?

Phases 0-3 are READ-ONLY apart from the model walk. Phase 4 mutates the
project and is opt-in via ``--live``.

Usage
-----
Command line (engine mode, from the IPStoPF repo root)::

    cd /d <IPStoPF repo root>
    "C:\\Program Files\\Python312\\python.exe" test_relay_skeletons.py
    "C:\\Program Files\\Python312\\python.exe" test_relay_skeletons.py --live
    "C:\\Program Files\\Python312\\python.exe" test_relay_skeletons.py --reports-only
    "C:\\Program Files\\Python312\\python.exe" test_relay_skeletons.py --dump

Inside the PowerFactory GUI (ComPython script on the Citrix session), argv is
not available - set ``MODE`` below instead and execute the file.

Exit codes: 0 = all phases passed; 1 = a phase failed (see SUMMARY);
2 = could not open PowerFactory / import the repo.
"""

import csv
import datetime as _dt
import logging
import os
import sys
import time
from pathlib import Path

# --- Hosted-mode switches (ignored when argv is present) -------------------
MODE = {
    "live": False,        # True -> actually run add_relay_skeletons (mutates)
    "reports_only": False,  # True -> skip everything needing PowerFactory
    "dump": True,         # True -> write a per-element join CSV
    "debug": False,  # True -> DEBUG-level logging from ips_data.*
    "recache": False,  # True -> force a live fetch, bypassing the cache
}

# Make the IPStoPF repo importable when run directly from anywhere.
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Keep aligned with ips_to_pf_batch.py / build_relay_path_cache.py.
PF_PYTHON_DIR = r"C:\Program Files\DIgSILENT\PowerFactory 2025 SP3\Python\3.12"

# The four reports add_relay_skeletons consumes, with the dict builder each
# one is fed to. Keep this list in step with add_relay_skeletons.
REPORTS = [
    ("List-RelayCBs", "relay", "produce_switch_based_dict"),
    ("List-Reclosers", "recloser", "produce_line_switch_based_dict"),
    ("List-Fuses", "fuse", "produce_line_switch_based_dict"),
    ("List-GasSwitches", "gas_switch", "produce_line_switch_based_dict"),
]

MAX_AGE = 3  # matches the max_age used inside add_relay_skeletons

# Print report rows mentioning this substation, so the key that SHOULD
# match can be compared against the model's extracted ids. "" disables.
FOCUS = "BALB"

log = logging.getLogger("skeleton_test")


# ===========================================================================
# Plumbing
# ===========================================================================

def parse_mode():
    """CLI flags override MODE; hosted runs (no argv) use MODE as-is."""
    argv = sys.argv[1:]
    if not argv:
        return dict(MODE)
    mode = dict(MODE)
    for arg in argv:
        key = arg.lstrip("-").replace("-", "_")
        if key in mode:
            mode[key] = True
        else:
            raise SystemExit(f"Unknown flag {arg!r}. Valid: --live "
                             f"--reports-only --dump --debug")
    return mode


def setup_logging(debug=False):
    """Console logging. add_relay_skeletons logs through logging_config, so
    pin that namespace explicitly rather than trusting the root level."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                          datefmt="%H:%M:%S")
    )
    handler.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(handler)

    logfile = REPO_ROOT / (
        f"skeleton_test_{_dt.datetime.now():%Y%m%d_%H%M%S}.log"
    )
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    ))
    fh.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(fh)
    print(f"skeleton test logging to {logfile}")
    for ns in ("ips_data", "utils", "update_powerfactory", "skeleton_test"):
        logging.getLogger(ns).setLevel(
            logging.DEBUG if debug else logging.INFO
        )


def banner(text):
    log.info("")
    log.info("=" * 72)
    log.info(text)
    log.info("=" * 72)


def open_powerfactory():
    pf_install_dir = str(Path(PF_PYTHON_DIR).parents[1])
    os.add_dll_directory(pf_install_dir)
    if PF_PYTHON_DIR not in sys.path:
        sys.path.append(PF_PYTHON_DIR)
    import powerfactory as pf
    app = pf.GetApplicationExt()
    if app is None:
        raise RuntimeError("GetApplicationExt returned None")
    return app


def get_app():
    """Return (app, hosted). hosted=True means we are inside a PF GUI session.

    Engine mode starts its own session; note that a session started this way
    will NOT see the project you have open in the Citrix GUI unless it is
    activated for the same user, so prefer running this hosted for the live
    phase.
    """
    try:
        import powerfactory as pf
        app = pf.GetApplication()
        if app is not None:
            return app, True
    except ImportError:
        pass
    return open_powerfactory(), False


# ===========================================================================
# Phase 0 - provenance
# ===========================================================================

def phase_provenance():
    banner("PHASE 0 - provenance")
    log.info(f"script      : {Path(__file__).resolve()}")
    log.info(f"interpreter : {sys.executable}")
    log.info(f"cwd         : {os.getcwd()}")
    log.info(f"repo root   : {REPO_ROOT}")

    # A stale duplicate of the repo earlier on sys.path is a recurring cause
    # of "the fix did not take effect". Report where each module really came
    # from rather than assuming REPO_ROOT won.
    import config.paths as cp
    log.info(f"config.paths from : {cp.__file__}")
    log.info(f"ASSET_CLASSES_PATH: {cp.ASSET_CLASSES_PATH}")

    from ips_data import add_relay_skeletons as ars
    log.info(f"add_relay_skeletons from: {ars.__file__}")

    import assetclasses.corporate_data as cd
    log.info(f"corporate_data from     : {cd.__file__}")
    try:
        import netdashread
        log.info(f"netdashread from        : {netdashread.__file__}")
    except ImportError as exc:
        log.warning(f"netdashread not importable: {exc}")

    # Surface anything in the corporate_data module that looks like a cache
    # location, so a stale on-disk cache can be spotted by mtime.
    for name in sorted(dir(cd)):
        if any(t in name.upper() for t in ("CACHE", "DIR", "PATH", "ROOT")):
            value = getattr(cd, name)
            if isinstance(value, (str, Path)):
                p = Path(value)
                stamp = ""
                try:
                    if p.exists():
                        mtime = _dt.datetime.fromtimestamp(p.stat().st_mtime)
                        stamp = f"  (exists, mtime {mtime:%Y-%m-%d %H:%M})"
                    else:
                        stamp = "  (MISSING)"
                except OSError as exc:
                    stamp = f"  (stat failed: {exc})"
                log.info(f"corporate_data.{name} = {value}{stamp}")
    return ars


# ===========================================================================
# Phase 1 - report probe
# ===========================================================================

def phase_reports(ars, recache=False):
    """Fetch each report, materialise it, and report row counts and shape.

    get_cached_data returns a lazy generator, so a failure inside it surfaces
    on iteration, not on the call. Materialising here is what makes an empty
    or broken report distinguishable from a working one.
    """
    banner("PHASE 1 - source reports")
    results = {}
    for report, label, builder_name in REPORTS:
        t0 = time.perf_counter()
        rows, error = [], None
        try:
            kwargs = {"report": report, "max_age": MAX_AGE}
            if recache:
                kwargs["recache"] = True
            rows = list(ars.get_cached_data(**kwargs) or [])
        except Exception as exc:  # noqa: BLE001 - we want the class name
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - t0

        if error:
            log.error(f"{report:<20} RAISED after {elapsed:5.1f}s  {error}")
        elif not rows:
            log.error(f"{report:<20} 0 rows in {elapsed:5.1f}s  "
                      f"<-- empty: source unreachable or report retired")
        else:
            log.info(f"{report:<20} {len(rows):>7,} rows in {elapsed:5.1f}s")
            sample = rows[0]
            fields = [f for f in dir(sample)
                      if not f.startswith("_") and not callable(getattr(sample, f, None))]
            log.info(f"{'':<20} fields: {', '.join(fields[:14])}")
            log.info(f"{'':<20} first row: {sample!r}"[:300])

        # Build the dict exactly as add_relay_skeletons does, so key format
        # is observed rather than assumed.
        builder = getattr(ars, builder_name)
        d = builder(rows) if rows else {}
        keys = list(d.keys())
        if keys:
            lengths = sorted({len(str(k)) for k in keys})
            log.info(f"{'':<20} dict: {len(keys):,} keys, "
                     f"key length(s) {lengths}, sample {keys[:3]}")

        if FOCUS and rows:
            for row in rows:
                text = " ".join(
                    str(getattr(row, f, "")) for f in
                    ("plant_no", "assetname", "ellipse_equip_no", "equip_no")
                )
                if FOCUS.upper() in text.upper():
                    log.info(f"{'':<20} FOCUS {report}: {row!r}"[:400])

        results[label] = {
            "report": report, "rows": rows, "dict": d,
            "error": error, "elapsed": elapsed,
        }

    ok = all(r["rows"] and not r["error"] for r in results.values())
    if not ok:
        log.error("PHASE 1 FAILED - at least one report is empty or errored. "
                  "Every downstream count below will be zero for that device "
                  "class regardless of the model.")
    else:
        log.info("PHASE 1 PASSED - all four reports returned rows.")
    return results, ok


# ===========================================================================
# Phase 2 - PowerFactory context
# ===========================================================================

def count_protection(project):
    counts = {}
    for cls in ("ElmRelay", "RelFuse", "RelToc", "RelIoc", "RelRecl",
                "StaCt", "RelMeasure"):
        counts[cls] = len(project.GetContents(f"*.{cls}", True))
    return counts


def phase_pf_context(app):
    banner("PHASE 2 - PowerFactory context")
    project = app.GetActiveProject()
    if project is None:
        log.error("No active project. Activate the Ergon project first.")
        return None, None, False

    log.info(f"active project : {project.loc_name}")
    parent = project.GetAttribute("fold_id")
    log.info(f"parent folder  : {parent.loc_name if parent else '<none>'}")

    try:
        from utils.pf_utils import determine_region
        log.info(f"determine_region -> {determine_region(project)}")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"determine_region failed: {exc}")

    counts = count_protection(project)
    log.info("existing protection objects:")
    for cls, n in counts.items():
        log.info(f"    {cls:<12} {n:>6,}")
    total = counts["ElmRelay"] + counts["RelFuse"]
    if total:
        log.warning(f"{total:,} ElmRelay/RelFuse still present - the "
                    f"'created' count in phase 4 will include found-existing "
                    f"devices, not just new ones.")
    else:
        log.info("Model is clean of relays/fuses - a good baseline.")

    t0 = time.perf_counter()
    elm_coups = project.GetContents("*.ElmCoup", True)
    sta_switches = project.GetContents("*.StaSwitch", True)
    log.info(f"ElmCoup {len(elm_coups):,}, StaSwitch {len(sta_switches):,} "
             f"(walk {time.perf_counter() - t0:.1f}s)")

    return project, elm_coups + sta_switches, True


# ===========================================================================
# Phase 3 - dry-run join
# ===========================================================================

def phase_join(ars, project, elements, reports, dump=False):
    """Replicate the element loop WITHOUT creating anything.

    NOTE: produce_switch_based_dict returns a defaultdict, so ``d[key]``
    never raises KeyError - it inserts an empty list. Membership must be
    tested with ``in``, or every element would look like a match.
    """
    banner("PHASE 3 - dry-run join (no objects created)")

    t0 = time.perf_counter()
    feeder_cbs = ars.produce_list_of_model_feeder_cbs(project)
    log.info(f"feeder CBs: {len(feeder_cbs):,} "
             f"({time.perf_counter() - t0:.1f}s)")
    try:
        feeder_cb_set = set(feeder_cbs)
    except TypeError:
        feeder_cb_set = feeder_cbs  # unhashable PF objects: fall back to list

    stats = {
        "elements": len(elements),
        "no_for_name": 0,
        "no_ecorp_id": 0,
        "gated_not_feeder_cb": 0,
        "matched_elements": 0,
        "would_create": 0,
    }
    per_class = {label: 0 for _, label, _ in REPORTS}
    ecorp_ids = []
    rows_out = []

    for i, elm in enumerate(elements):
        if i and i % 500 == 0:
            log.info(f"  scanned {i:,}/{len(elements):,}")

        try:
            for_name = elm.GetAttribute("for_name")
        except AttributeError:
            for_name = None
        if not for_name:
            stats["no_for_name"] += 1
            continue

        ecorp_id = ars.ellipse_ecorp_asset_id_extraction(
            for_name, ars.NETWORK_DISTRIBUTION
        )
        if not ecorp_id:
            stats["no_ecorp_id"] += 1
            continue
        ecorp_ids.append(ecorp_id)

        gated = False
        parent = elm.GetParent()
        if parent is not None and parent.GetClassName() == "ElmSubstat":
            if elm not in feeder_cb_set:
                gated = True
                stats["gated_not_feeder_cb"] += 1

        hits = {}
        for label in per_class:
            d = reports[label]["dict"]
            if ecorp_id in d:
                hits[label] = len(d[ecorp_id])

        if hits:
            stats["matched_elements"] += 1
            for label, n in hits.items():
                per_class[label] += n
            if not gated:
                stats["would_create"] += sum(hits.values())

        if dump:
            rows_out.append({
                "element": str(elm),
                "class": elm.GetClassName(),
                "for_name": for_name,
                "ecorp_id": ecorp_id,
                "gated_not_feeder_cb": int(gated),
                **{f"n_{label}": hits.get(label, 0) for label in per_class},
            })

    log.info("")
    log.info("join results:")
    for key, value in stats.items():
        log.info(f"    {key:<24} {value:>8,}")
    log.info("    devices by class (ungated):")
    for label, n in per_class.items():
        log.info(f"        {label:<20} {n:>8,}")

    # The single most useful line: is this a data problem or a key-format
    # problem? Same numbers, very different fix.
    if ecorp_ids:
        model_ids = set(ecorp_ids)
        log.info("")
        log.info(f"distinct ecorp ids on model elements: {len(model_ids):,} "
                 f"(sample {sorted(model_ids)[:3]})")
        for label in per_class:
            d = reports[label]["dict"]
            if not d:
                continue
            overlap = len(model_ids & set(d.keys()))
            log.info(f"    {label:<12} report keys {len(d):>7,}  "
                     f"overlap with model {overlap:>7,}")
            if overlap == 0:
                log.error(
                    f"    {label}: ZERO overlap. Report has data but no key "
                    f"matches. Compare key formats above - the distribution "
                    f"path zero-pads the model id to 12 chars; the report "
                    f"key is str(asset_id) unpadded."
                )

    if dump and rows_out:
        out = REPO_ROOT / (
            f"skeleton_join_{_dt.datetime.now():%Y%m%d_%H%M%S}.csv"
        )
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()))
            writer.writeheader()
            writer.writerows(rows_out)
        log.info(f"per-element join dump written to {out}")

    ok = stats["would_create"] > 0
    log.info("PHASE 3 PASSED - the join produces work to do."
             if ok else
             "PHASE 3 FAILED - nothing would be created; do not bother with "
             "the live run until this is non-zero.")
    return stats, ok


# ===========================================================================
# Phase 4 - live run
# ===========================================================================

def phase_live(ars, app, project):
    banner("PHASE 4 - LIVE add_relay_skeletons (mutates the project)")
    before = count_protection(project)
    log.warning("This deletes dat_src=='PDS' protection objects and creates "
                "new ones. Ctrl-C now if that is not what you want.")

    gui_was_on = True
    t0 = time.perf_counter()
    try:
        app.SetGuiUpdateEnabled(0)
        created = ars.add_relay_skeletons(
            app, network_level=ars.NETWORK_DISTRIBUTION, project=project
        )
    finally:
        if gui_was_on:
            app.SetGuiUpdateEnabled(1)
    elapsed = time.perf_counter() - t0

    if created is None:
        log.error("add_relay_skeletons returned None - it exited early "
                  "(no active project, or project mismatch).")
        return False

    after = count_protection(project)
    log.info(f"add_relay_skeletons returned {len(created):,} device handles "
             f"in {elapsed:.1f}s")
    log.info(f"{sum(1 for d in created if d is None):,} of those are None "
             f"(setup_relay bailed on missing asset_id/plant_no/ellipse_id)")
    log.info("object counts before -> after:")
    for cls in before:
        log.info(f"    {cls:<12} {before[cls]:>6,} -> {after[cls]:>6,} "
                 f"({after[cls] - before[cls]:+,})")

    prs = [r for r in project.GetContents("*.ElmRelay", True)
           if r.GetAttribute("dat_src") == ars.DATA_SOURCE_STRING]
    log.info(f"ElmRelay with dat_src=='{ars.DATA_SOURCE_STRING}': {len(prs):,}")
    for relay in prs[:10]:
        log.info(f"    {relay.loc_name:<28} for_name="
                 f"{relay.GetAttribute('for_name')!r} "
                 f"outserv={relay.GetAttribute('outserv')}")

    ok = (after["ElmRelay"] + after["RelFuse"]) > (
        before["ElmRelay"] + before["RelFuse"]
    )
    log.info("PHASE 4 PASSED - the model gained relay skeletons."
             if ok else
             "PHASE 4 FAILED - no net new relays or fuses.")
    return ok


# ===========================================================================
# Driver
# ===========================================================================

def main():
    mode = parse_mode()
    setup_logging(debug=mode["debug"])
    log.info(f"skeleton test starting {_dt.datetime.now():%Y-%m-%d %H:%M:%S} "
             f"mode={mode}")

    outcomes = {}
    ars = phase_provenance()

    reports, ok = phase_reports(ars, recache=mode["recache"])
    outcomes["reports"] = ok

    if mode["reports_only"]:
        return summarise(outcomes)

    app, hosted = get_app()
    log.info(f"PowerFactory session: {'hosted (GUI)' if hosted else 'engine mode'}")

    project, elements, ok = phase_pf_context(app)
    outcomes["pf_context"] = ok
    if not ok:
        return summarise(outcomes)

    _, ok = phase_join(ars, project, elements, reports, dump=mode["dump"])
    outcomes["join"] = ok

    if mode["live"]:
        outcomes["live"] = phase_live(ars, app, project)
    else:
        log.info("")
        log.info("Live phase skipped. Re-run with --live to create skeletons.")

    return summarise(outcomes)


def summarise(outcomes):
    banner("SUMMARY")
    for phase, ok in outcomes.items():
        log.info(f"    {phase:<12} {'PASS' if ok else 'FAIL'}")
    failed = [p for p, ok in outcomes.items() if not ok]
    if failed:
        log.error(f"FAILED PHASES: {', '.join(failed)}")
        return 1
    log.info("All phases passed.")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:  # noqa: BLE001
        logging.getLogger("skeleton_test").exception("test aborted")
        rc = 2
    # sys.exit inside a hosted PF script surfaces as a script error; only the
    # command-line path should use it.
    print(f"skeleton test finished with code {rc}")
    if sys.argv[1:]:
        sys.exit(rc)