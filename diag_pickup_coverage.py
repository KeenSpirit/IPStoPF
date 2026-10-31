"""
Read-only diagnostic: which IPS records give a relay no pickup at all?

Burleigh Heads 28 Oct, diag_device_function.py: every in-service controller
on a switch (IPS device ID SW...) carries only ADVC *detection* settings
(Root/Detection/Detection Group A: Phase Fault Current, Trip LBS After ...)
or, for X8999-E and X120489, nothing but DNP3 rows. None has an overcurrent
protection setting, so the mapping file reads no pickup for them and the
model keeps its default 200 A / 40 A elements in service. SPA then grades
those defaults (X15402-C_SW780 reads 240 A / 28 A in IPS, 200 A / 40 A in
the model).

Proposed rule (not deployed): a relay whose mapping file has pickup rows
(Ipset / Ipsetr) but none of them found in its IPS settings has no
protection function in IPS; place it out of service and report it, as the
NOJA trip-map check already does.

This script measures that rule before anything changes. For every device
in one or more IPStoPF results CSVs (dp072_SEQ_Models_<project>.csv) it
re-reads the IPS settings exactly as a run does and reports:
  * the switch_ / sect_ decision with every Sectionaliser/Detection row read
  * pickup rows in the mapping file: found in IPS / missing
  * outserv rows read from IPS: found / missing
  * whether the proposed rule would place the relay out of service

Energex (SEQ) CSVs only. No PowerFactory needed; SELECTs against the ODS.

    "C:\\Program Files\\Python312\\python.exe" diag_pickup_coverage.py
        (no argument: every dp072_SEQ_Models_*.csv in the ProtectionBatchRunner
        output folder)
    "C:\\Program Files\\Python312\\python.exe" diag_pickup_coverage.py "<folder>\\dp072_SEQ_Models_Burleigh_Heads.csv"
    "C:\\Program Files\\Python312\\python.exe" diag_pickup_coverage.py "<run folder>\\dp072_SEQ_Models_*.csv"
"""

import csv
import glob
import os
import sys
import time

# Captured before the project imports below, in case one of them (or a
# library they pull in from the share) changes sys.argv.
_ARGS = list(sys.argv[1:])
import types
from collections import Counter
from contextlib import closing

from core.protection_device import ProtectionDevice
from ips_data import ods_connection
from ips_data.query_database import ENERGEX_BATCH_SQL, batch_get_ips_settings
from update_powerfactory import mapping_file as mf
from update_powerfactory import noja_function as nf
from update_powerfactory import relay_settings as rs
from update_powerfactory.setting_utils import build_setting_key

RESULTS_DIR = r"C:\LocalData\PowerFactory Output Folders\ProtectionBatchRunner"
# Used when no path is given on the command line: every SEQ project's
# results CSV in the ProtectionBatchRunner output folder.
DEFAULT_PATTERN = os.path.join(RESULTS_DIR, "dp072_SEQ_Models_*.csv")

OUT_DIR = os.path.join(
    RESULTS_DIR, f"pickup_coverage_diag_{time.strftime('%Y%m%d_%H%M%S')}",
)

PREFIXES = ("switch_", "sect_")
CONSTANT = frozenset({"ON", "On", "OFF", "Off", "None"})


def _function(settings):
    """switch / sect / None, reading every row (the Step 2 fix)."""
    if hasattr(rs, "settings_function"):
        return rs.settings_function(settings)
    sect = any(len(r) > 2 and r[1] == "Sectionaliser"
               and str(r[2] or "").strip().lower() in ("on", "auto") for r in settings)
    det = any(len(r) > 2 and r[1] == "Detection"
              and str(r[2] or "").strip().lower() == "on" for r in settings)
    return "sect" if sect else "switch" if det else None


def pickup_coverage(mapping_rows, setting_dict):
    """(found, missing) over the mapping rows that set a pickup from IPS."""
    found = missing = 0
    for line in mapping_rows:
        if len(line) < 4 or line[2] not in rs.PICKUP_ATTRIBUTES or line[3] in CONSTANT:
            continue
        if build_setting_key(line) in setting_dict:
            found += 1
        else:
            missing += 1
    return found, missing


def read_results(paths):
    devices = []
    for path in paths:
        project = os.path.basename(path).replace("dp072_SEQ_Models_", "").replace(".csv", "")
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if not row.get("SETTING_ID") or not row.get("PLANT_NUMBER"):
                    continue
                pattern = row["RELAY_PATTERN"]
                for p in PREFIXES:
                    if pattern.startswith(p):
                        pattern = pattern[len(p):]
                devices.append({
                    "project": project, "name": row["PLANT_NUMBER"],
                    "setting_id": row["SETTING_ID"], "pattern": pattern,
                    "run_result": row.get("RESULT") or "in service",
                })
    return devices


def main(argv):
    args = [a.strip().strip('"') for a in argv if a.strip()] or [DEFAULT_PATTERN]
    print(f"Arguments received: {args}")
    found = set()
    for arg in args:
        matches = glob.glob(arg) or ([arg] if os.path.isfile(arg) else [])
        print(f"  {arg}\n    -> {len(matches)} file(s)")
        found.update(matches)
    paths = sorted(p for p in found if "_SEQ_" in os.path.basename(p))
    if not paths:
        sys.exit(
            "No SEQ results CSV found. Pass dp072_SEQ_Models_<project>.csv "
            f"path(s) (wildcards allowed), or put them in {RESULTS_DIR}"
        )
    devices = read_results(paths)
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"{len(devices)} devices in {len(paths)} project file(s)")

    ids = sorted({d["setting_id"] for d in devices})
    with closing(ods_connection.connect_to_db("Energex")) as connection:
        raw = batch_get_ips_settings(
            None, connection, ids, ENERGEX_BATCH_SQL, skip_empty_setting=False
        )

    out = []
    for d in devices:
        pf_stub = types.SimpleNamespace(loc_name=d["name"])
        dev = ProtectionDevice(None, d["pattern"], d["name"], d["setting_id"], None, pf_stub, "")
        dev.associated_settings(raw)
        rec = dict(d, ips_rows=len(dev.settings), decision="", pickup_found="",
                   pickup_missing="", outserv_found="", outserv_missing="", rule="")
        function = None if not dev.settings else _function(dev.settings)
        if not dev.settings:
            rec["decision"] = "switch (no settings)"
        elif function:
            rec["decision"] = function
        else:
            rows, _relay_type = mf.read_mapping_file(None, d["pattern"], pf_stub, dev.ct_secondary)
            if not rows:
                rec["decision"] = "protection (no mapping file)"
            else:
                rec["decision"] = "protection"
                sdict = rs.create_setting_dictionary(None, dev.settings, rows, pf_stub)
                pf, pm = pickup_coverage(rows, sdict)
                of, om = nf.trip_map_coverage(rows, sdict)
                rec.update(pickup_found=pf, pickup_missing=pm, outserv_found=of, outserv_missing=om)
                if pm and not pf:
                    rec["rule"] = "OUT OF SERVICE (no pickup in IPS)"
                elif not pm and not pf:
                    rec["rule"] = "no pickup rows in mapping"
                else:
                    rec["rule"] = "unchanged"
        out.append(rec)

    fields = list(out[0].keys()) if out else []
    with open(os.path.join(OUT_DIR, "pickup_coverage.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)

    newly_oos = [r for r in out if r["rule"].startswith("OUT")]
    newly_switch = [r for r in out if r["run_result"] == "in service"
                    and r["decision"] in ("switch", "sect")]
    print(f"\nWould change from in service with the Step 2 fix (Sectionaliser/Detection): {len(newly_switch)}")
    print(f"Would go out of service under the no-pickup rule: {len(newly_oos)}")
    for (pattern, suffix), n in Counter(
        (r["pattern"], r["name"].split("_")[-1][:2]) for r in newly_oos
    ).most_common():
        print(f"    {n:4}  {pattern}  (device ID {suffix}..)")
    partial = [r for r in out if r["rule"] == "unchanged" and r["pickup_missing"]]
    print(f"Pickups partly found (left as they are, listed in the CSV): {len(partial)}")
    print(f"Output: {OUT_DIR}")


if __name__ == "__main__":
    main(_ARGS)
