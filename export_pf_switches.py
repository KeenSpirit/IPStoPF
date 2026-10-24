"""
Export every StaSwitch and ElmCoup in every Energex (SEQ) distribution
project to one CSV, for offline matching against the IPS setting-ID cache
(Report-Cache-ProtectionSettingIDs-EX.csv).

READ-ONLY. The script activates each project, reads, and moves on. It
never writes an attribute, creates, deletes or saves anything.

Which projects: the derived copies the batch runs on, i.e.
    \\<current user>.IntUser\\MasterProjects - Derived\\SEQ Models\\*.IntPrj

Which study case: whatever the project activates with. IPStoPF does not
switch study case before enumerating switches, so this is the case its
energised / calc-relevant filters see. The case name is recorded per row.

Two columns replicate IPStoPF's own switch filters so the matcher can say
why a switch was never looked up:
    valid_switch    ex_settings._get_valid_switches
    should_process  ex_settings._should_process_switch
They mirror the code as of 2026-10-23; if those functions change, update
_passes_valid_filter / _passes_should_process here to match.

Note on the relay/fuse columns: the derived projects reflect the last
IPStoPF run, so cubicles whose switch failed to match have already had
their relays deleted by _handle_unmatched_switch.

Run externally (like ips_to_pf_batch.py) by default, or from inside
PowerFactory as a ComPython script (set RUN_INSIDE_POWERFACTORY).

Output: UTF-8 (with BOM, so Excel opens it cleanly), every field quoted,
whitespace in names preserved exactly. Rows are written project by
project, so a failure part-way keeps everything exported up to then.
"""

import csv
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
PF_INSTALL_DIR = r"C:\Program Files\DIgSILENT\PowerFactory 2025 SP3"
PF_PYTHON_DIR = rf"{PF_INSTALL_DIR}\Python\3.12"
PF_INI_FILE = "PowerFactory_PRD.ini"
# Same config.yaml ips_to_pf_batch.py reads (powerfactory: user:). The
# password comes from Windows Credential Manager via keyring, as there.
CONFIG_YAML = r"C:\LocalData\ProtectionBatchRunner\config.yaml"

OUTPUT_DIR = r"C:\LocalData\PF switch export"

DERIVED_FOLDER = "MasterProjects - Derived"
REGION_FOLDER = "SEQ Models"

# False: run externally with python.exe (GetApplicationExt, like the batch).
# True: run as a ComPython script inside PowerFactory. Keep that script
# object in your own user folder, not inside a project: the export
# activates and deactivates every project in turn.
RUN_INSIDE_POWERFACTORY = False

# Set of project names to restrict the run to (e.g. {"Richlands"}) for a
# first test; None exports every project in the folder.
PILOT = None

# Feeder membership uses ElmFeeder.GetAll() once per feeder per project.
# Set False if it proves slow; the column is then left blank.
INCLUDE_FEEDER = True

FIELDS = [
    "export_timestamp",
    "project",
    "project_full_name",
    "study_case",
    "class",
    "loc_name",
    "full_name",
    "fold_id_name",
    "fold_id_class",
    "aUsage",
    "on_off",
    "out_of_service",
    "energised",
    "calc_relevant",
    "grid",
    "valid_switch",
    "should_process",
    "cubicle_name",
    "connected_obj_name",
    "connected_obj_class",
    "terminal_name",
    "terminal_kv",
    "other_terminal_name",
    "other_terminal_kv",
    "substation",
    "substation_class",
    "feeder",
    "relays",
    "fuses",
    "relays_other_cubicle",
    "read_errors",
]

SUBSTATION_CLASSES = ("ElmSubstat", "ElmTrfstat", "ElmSite")

logger = logging.getLogger("export_pf_switches")

# (class, field) pairs already warned about, so a wrong attribute name
# gives one warning per run instead of one per switch.
_warned = set()


# --------------------------------------------------------------------------
# Guarded reads
# --------------------------------------------------------------------------
def _read(fn, field, cls, errors):
    """Call fn(); on any failure record the field and return None."""
    try:
        return fn()
    except Exception as e:  # PF raises AttributeError and others
        errors.append(field)
        if (cls, field) not in _warned:
            _warned.add((cls, field))
            logger.warning(f"{cls}.{field}: read failed ({e!r}); left blank")
        return None


def _name(obj):
    return obj.loc_name if obj is not None else None


def _cls(obj):
    return obj.GetClassName() if obj is not None else None


def _flag(value):
    """PF ints/bools -> 1/0; None stays None (blank cell)."""
    if value is None:
        return None
    return 1 if value else 0


# --------------------------------------------------------------------------
# IPStoPF filter replicas (see module docstring)
# --------------------------------------------------------------------------
def _passes_valid_filter(switch):
    """ex_settings._get_valid_switches, per switch."""
    return bool(
        switch.GetAttribute("cpGrid")
        and not switch.IsOutOfService()
        and switch.IsCalcRelevant()
        and switch.IsEnergized()
    )


def _passes_should_process(switch):
    """ex_settings._should_process_switch."""
    try:
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


# --------------------------------------------------------------------------
# Per-switch row
# --------------------------------------------------------------------------
def _containing_substation(terminal):
    """Walk up from a terminal to the first substation-like container."""
    obj = terminal.fold_id if terminal is not None else None
    for _ in range(10):
        if obj is None:
            return None
        cls = obj.GetClassName()
        if cls in SUBSTATION_CLASSES:
            return obj
        if cls in ("ElmNet", "IntPrj", "IntPrjfolder"):
            return None
        obj = obj.fold_id
    return None


def _devices_in(cubicle, pattern):
    """'name [type]; ...' for relays/fuses in a cubicle."""
    if cubicle is None:
        return None
    items = []
    for dev in cubicle.GetContents(pattern):
        typ = dev.typ_id
        items.append(f"{dev.loc_name} [{typ.loc_name if typ else 'no type'}]")
    return "; ".join(items) if items else ""


def _feeders_of(objs, feeder_members):
    names = set()
    for obj in objs:
        if obj is not None:
            names.update(feeder_members.get(obj, ()))
    return "; ".join(sorted(names))


def switch_row(switch, base, feeder_members):
    errors = []
    cls = switch.GetClassName()
    r = dict(base)
    r["class"] = cls
    r["loc_name"] = switch.loc_name
    r["full_name"] = _read(switch.GetFullName, "full_name", cls, errors)

    fold = _read(lambda: switch.fold_id, "fold_id", cls, errors)
    r["fold_id_name"] = _name(fold)
    r["fold_id_class"] = _cls(fold)

    r["aUsage"] = _read(lambda: switch.GetAttribute("aUsage"), "aUsage", cls, errors)
    r["on_off"] = _read(lambda: switch.GetAttribute("on_off"), "on_off", cls, errors)
    r["out_of_service"] = _flag(_read(switch.IsOutOfService, "out_of_service", cls, errors))
    r["energised"] = _flag(_read(switch.IsEnergized, "energised", cls, errors))
    r["calc_relevant"] = _flag(_read(switch.IsCalcRelevant, "calc_relevant", cls, errors))
    r["grid"] = _name(_read(lambda: switch.GetAttribute("cpGrid"), "grid", cls, errors))
    r["valid_switch"] = _flag(_read(lambda: _passes_valid_filter(switch), "valid_switch", cls, errors))
    r["should_process"] = _flag(_read(lambda: _passes_should_process(switch), "should_process", cls, errors))

    # The cubicle IPStoPF places relays in, and its terminal.
    if cls == "StaSwitch":
        cubicle = fold
        other_cubicle = None
    else:
        cubicle = _read(lambda: switch.GetCubicle(0), "cubicle", cls, errors)
        other_cubicle = _read(lambda: switch.GetCubicle(1), "other_cubicle", cls, errors)

    terminal = _read(lambda: cubicle.cterm, "terminal", cls, errors) if cubicle else None
    connected = _read(lambda: cubicle.obj_id, "connected_obj", cls, errors) if cubicle else None
    other_terminal = (
        _read(lambda: other_cubicle.cterm, "other_terminal", cls, errors)
        if other_cubicle else None
    )

    r["cubicle_name"] = _name(cubicle)
    r["connected_obj_name"] = _name(connected)
    r["connected_obj_class"] = _cls(connected)
    r["terminal_name"] = _name(terminal)
    r["terminal_kv"] = (
        _read(lambda: terminal.GetAttribute("uknom"), "terminal_kv", cls, errors)
        if terminal else None
    )
    r["other_terminal_name"] = _name(other_terminal)
    r["other_terminal_kv"] = (
        _read(lambda: other_terminal.GetAttribute("uknom"), "other_terminal_kv", cls, errors)
        if other_terminal else None
    )

    sub = _read(lambda: _containing_substation(terminal), "substation", cls, errors)
    r["substation"] = _name(sub)
    r["substation_class"] = _cls(sub)

    if feeder_members:
        r["feeder"] = _read(
            lambda: _feeders_of([terminal, connected, other_terminal], feeder_members),
            "feeder", cls, errors,
        )

    r["relays"] = _read(lambda: _devices_in(cubicle, "*.ElmRelay"), "relays", cls, errors)
    r["fuses"] = _read(lambda: _devices_in(cubicle, "*.RelFuse"), "fuses", cls, errors)
    if other_cubicle is not None:
        r["relays_other_cubicle"] = _read(
            lambda: _devices_in(other_cubicle, "*.ElmRelay"),
            "relays_other_cubicle", cls, errors,
        )

    r["read_errors"] = "; ".join(errors)
    return r


# --------------------------------------------------------------------------
# Per-project
# --------------------------------------------------------------------------
def _feeder_members(app):
    """{element: {feeder names}} for in-service feeders of the active project."""
    members = {}
    net_data = app.GetProjectFolder("netdat")
    if net_data is None:
        return members
    for feeder in net_data.GetContents("*.ElmFeeder", True):
        try:
            if feeder.IsOutOfService():
                continue
            for elm in feeder.GetAll():
                members.setdefault(elm, set()).add(feeder.loc_name)
        except Exception as e:
            logger.warning(f"Feeder {feeder.loc_name}: GetAll failed ({e!r})")
    return members


def export_project(app, project, writer, stamp):
    if project.Activate():
        raise RuntimeError(f"Activate() returned an error code for {project.loc_name}")

    prj = app.GetActiveProject()
    if prj is None or prj.loc_name != project.loc_name:
        raise RuntimeError(f"{project.loc_name} did not become the active project")

    case = app.GetActiveStudyCase()
    base = {
        "export_timestamp": stamp,
        "project": prj.loc_name,
        "project_full_name": prj.GetFullName(),
        "study_case": case.loc_name if case else "",
    }

    t0 = time.monotonic()
    feeder_members = _feeder_members(app) if INCLUDE_FEEDER else {}
    if INCLUDE_FEEDER:
        logger.info(f"  feeder membership built in {time.monotonic() - t0:.1f} s")

    # Same enumeration as ex_settings._get_valid_switches / create_new_devices.
    switches = prj.GetContents("*.StaSwitch", True) + prj.GetContents("*.ElmCoup", True)

    n_errors = 0
    for switch in switches:
        try:
            writer.writerow(switch_row(switch, base, feeder_members))
        except Exception as e:
            n_errors += 1
            logger.warning(
                f"  {getattr(switch, 'loc_name', '?')}: row skipped ({e!r})"
            )
    logger.info(
        f"  {len(switches)} switches exported "
        f"(study case '{base['study_case']}', {n_errors} row(s) skipped)"
    )
    prj.Deactivate()
    return len(switches)


def seq_projects(app):
    user = app.GetCurrentUser()
    derived = user.GetContents(DERIVED_FOLDER)
    if not derived:
        raise RuntimeError(f"No '{DERIVED_FOLDER}' folder under {user.loc_name}")
    region = derived[0].GetContents(REGION_FOLDER)
    if not region:
        raise RuntimeError(f"No '{REGION_FOLDER}' folder under {DERIVED_FOLDER}")
    projects = region[0].GetContents("*.IntPrj")
    if PILOT:
        projects = [p for p in projects if p.loc_name in PILOT]
    return sorted(projects, key=lambda p: p.loc_name)


def run(app):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    out_path = Path(OUTPUT_DIR) / f"pf_switches_SEQ_{datetime.now():%Y%m%d-%H%M%S}.csv"

    projects = seq_projects(app)
    logger.info(f"{len(projects)} SEQ project(s) to export -> {out_path}")

    total, failed = 0, []
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for i, project in enumerate(projects, 1):
            logger.info(f"[{i}/{len(projects)}] {project.loc_name}")
            try:
                total += export_project(app, project, writer, stamp)
            except Exception:
                failed.append(project.loc_name)
                logger.exception(f"  {project.loc_name}: export failed; continuing")
            f.flush()

    logger.info(f"Done: {total} switches from {len(projects) - len(failed)} project(s)")
    if failed:
        logger.warning(f"Failed project(s): {', '.join(failed)}")
    logger.info(f"Output: {out_path}")
    return out_path


# --------------------------------------------------------------------------
# Application handling
# --------------------------------------------------------------------------
def _setup_logging():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.INFO)
    for handler in (
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            Path(OUTPUT_DIR) / f"export_pf_switches_{datetime.now():%Y%m%d-%H%M%S}.log",
            encoding="utf-8",
        ),
    ):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    logger.info(f"Running {__file__} with {sys.executable}")


def _external_app():
    import yaml
    import keyring

    os.add_dll_directory(PF_INSTALL_DIR)
    if PF_PYTHON_DIR not in sys.path:
        sys.path.append(PF_PYTHON_DIR)
    import powerfactory as pf

    with open(CONFIG_YAML) as fh:
        user = yaml.safe_load(fh)["powerfactory"]["user"]
    password = keyring.get_password("PowerFactory", user)
    call = f'/ini "{PF_INSTALL_DIR}\\{PF_INI_FILE}"'
    return pf.GetApplicationExt(user, password, call)


def main():
    _setup_logging()
    external = not RUN_INSIDE_POWERFACTORY
    if external:
        app = _external_app()
    else:
        import powerfactory as pf
        app = pf.GetApplication()
    if app is None:
        logger.error("Could not obtain the PowerFactory application")
        return 1

    try:
        run(app)
    finally:
        if external:
            try:
                active = app.GetActiveProject()
                if active:
                    active.Deactivate()
            except Exception:
                logger.warning("Could not deactivate the active project", exc_info=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
