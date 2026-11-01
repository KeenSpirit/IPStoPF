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
    # Group 1: SOLKOR-N, failing (102 rows) and working (120 rows)
    "BTYSCH9_Solkor-N": "BCC3A4AE-0BB4-476E-A424-4C49618C66E9",
    "BTYSCH9_J02B": "B919ADCE-D3CD-4893-AC33-3815E31D6185",
    # Group 2: stubs
    "KRA10_J25": "C68C2912-CA53-4E08-BE2A-3C6FB87B2F0E",
    "CSQOXG5_CAG12": "A97CFD7E-A444-4009-8C76-FEB6A1A5258D",
    # Group 3: failing / working pairs
    "RBA6A+B_J31": "B5453807-E923-4FC8-BC90-5C709D58A2EC",
    "AJV1_J04B": "63C965A9-DF2E-400B-85B1-25D732EE622D",
    "AHSCPX5_J06": "BD2A7268-86CC-49D8-BE85-FB90B6623ECF",
    "IPS3A_J26_P123_1": "2362178C-DAAF-454E-A002-A61F35D10DCF",
    "QEHSBK13_J07": "D65520F5-2A2B-48D3-83F1-99612E513423",
    "AHDKMM11_J15B": "A39251A0-53C8-49FB-923D-F5D3D9750C40",
    "KRNQNP7+KRN7B_-J31": "965D60D7-C06C-47D3-95DB-0081B4758171",
    "CGTNSD21+NSD21B_J42": "AF73F039-30C4-4004-B971-9C99321C7932",
    "HHCRBH2_J15": "B5EFA963-5D0A-4575-8E74-15A7949522EF",
    "X753354_RE815": "0C768F3F-4B9A-47EB-AA02-32D9B5744C92",
    "TGW4_CAPM 2": "0801C2F8-3EAA-4967-9433-232FF3ADFF12",
    "X1328479-A_RE1929": "1339E33D-63A0-4A59-85FF-4E548A5247D1",
    # Group 4: SEL351
    "OHCSRA1_J01": "2E084C40-9825-4020-85C0-EF362165CF35",
    "QMRSRA2_J05B": "64305322-D448-476A-A878-895D07C9AE64",
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
