"""
Read-only: dump the IPS settings of one or more relays from the ODS.

Writes, for each setting ID:
  * <name>_settings.txt  - the rows IPStoPF actually uses (one parameter set,
    one row per block path + parameter, in the order a run reads them),
    tab-separated: block path, parameter, value, unit. Same layout as the
    Protection-SettingRelay-EX_*.txt exports.
and for all of them together:
  * all_rows.csv          - every row the ODS returns, every parameter set,
    with the set name, reader, import date and model IDs.

Run from the IPStoPF folder with the normal VM Python (no PowerFactory
needed). It only issues SELECTs against the ODS.

    "C:\\Program Files\\Python312\\python.exe" dump_ips_settings.py
        (no argument: the devices in DEFAULT_DEVICES below)

    "C:\\Program Files\\Python312\\python.exe" dump_ips_settings.py X779774_RE1109=E27DE5EC-082F-488B-AAA6-2335CABD8E12
        (NAME=SETTING_ID; several separated by spaces; a bare setting ID
        also works and is used as the name)

Setting IDs are in the SETTING_ID column of the run's
dp072_SEQ Models_<project>.csv, or in Report-Cache-ProtectionSettingIDs-EX.
Energex only.
"""

import csv
import os
import re
import sys
import time

# Captured before the project imports below, in case one of them (or a
# library they pull in from the share) changes sys.argv.
_ARGS = list(sys.argv[1:])

from contextlib import closing  # noqa: E402

from core.protection_device import ProtectionDevice  # noqa: E402
from ips_data import ods_connection  # noqa: E402
from ips_data.query_database import (  # noqa: E402
    ENERGEX_BATCH_SQL,
    batch_get_ips_settings,
)

OUT_DIR = os.path.join(
    r"C:\LocalData\PowerFactory Output Folders\ProtectionBatchRunner",
    f"ips_settings_dump_{time.strftime('%Y%m%d_%H%M%S')}",
)

# Used when no argument is given: WSOS4_Energex reclosers from Burleigh
# Heads whose mapping file reads no pickup (31 Oct), one of each record size.
DEFAULT_DEVICES = {
    "X779774_RE1109": "E27DE5EC-082F-488B-AAA6-2335CABD8E12",  # 265 rows
    "X733405_RE327": "00E32D82-290B-4625-9156-DB9E7C8AE758",   # 72 rows
}

_GUID = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")


def parse_args(args):
    """{name: setting_id} from NAME=ID or bare ID arguments."""
    devices = {}
    for arg in args:
        arg = arg.strip().strip('"')
        if not arg:
            continue
        name, _, sid = arg.rpartition("=")
        sid = sid.strip().upper()
        if not _GUID.match(sid):
            sys.exit(f"Not a setting ID: {arg!r} (expected NAME=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX)")
        devices[name.strip() or sid] = sid
    return devices or dict(DEFAULT_DEVICES)


def _safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def main(args):
    devices = parse_args(args)
    print(f"Devices: {devices}")
    os.makedirs(OUT_DIR, exist_ok=True)

    with closing(ods_connection.connect_to_db("Energex")) as connection:
        raw = batch_get_ips_settings(
            None, connection, list(devices.values()), ENERGEX_BATCH_SQL,
            skip_empty_setting=False,
        )

    cols = ["device", "setting_id", "blockpathenu", "paramnameenu",
            "proposedsetting", "unitenu", "relayparamsetid", "setname",
            "readername", "dateimportrelay", "relparmodelid", "modelsort"]
    with open(os.path.join(OUT_DIR, "all_rows.csv"), "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for name, sid in devices.items():
            for rec in raw.get(sid, []):
                w.writerow([name, sid] + [rec.get(c, "") for c in cols[2:]])

    for name, sid in devices.items():
        rows = raw.get(sid, [])
        sets = {}
        for rec in rows:
            key = rec.get("relayparamsetid")
            info = sets.setdefault(key, [rec.get("setname"), rec.get("readername"),
                                         rec.get("dateimportrelay"), 0])
            info[3] += 1

        dev = ProtectionDevice(None, "", name, sid, None, None, "")
        dev.associated_settings(raw)
        path = os.path.join(OUT_DIR, f"{_safe(name)}_settings.txt")
        with open(path, "w", newline="", encoding="utf-8") as f:
            for block, param, value, unit in dev.settings:
                f.write(f"{block}\t{param}\t{value}\t{unit or ''}\n")

        print(f"\n{name}  ({sid})")
        print(f"    ODS rows: {len(rows)} in {len(sets)} parameter set(s)")
        for key, (setname, reader, date, n) in sets.items():
            print(f"      set {key}: '{setname}', reader '{reader}', imported {date}, {n} rows")
        print(f"    rows IPStoPF uses: {len(dev.settings)} -> {os.path.basename(path)}")
        if not rows:
            print("    NO ROWS: check the setting ID")

    print(f"\nOutput: {OUT_DIR}")


if __name__ == "__main__":
    main(_ARGS)
