"""
Read-only diagnostic: why are some ADVC controllers on switches (IPS device
ID SW...) left in service as protection relays?

Burleigh Heads 28 Oct: X15402-C_SW780 (EQL_ADVC3_ADVC2_5.16) is a fault
indicator, yet IPStoPF left it in service, so SPA used it to cut protection
sections and as the backup for three devices on CRB25A. All 8 Burleigh Heads
pairs whose backup is one of these SW controllers fail coordination.

relay_settings.update_device_function decides switch_ / sect_ from two IPS
parameters, "Sectionaliser" and "Detection", but it stops at the FIRST
"Sectionaliser" row whatever its value. IPS returns rows in RelParModelID
order, not by name, so when a "Sectionaliser" row comes before "Detection"
the Detection value is never read.

For each device below this prints, in IPStoPF's own row order (same query
and same parameter-set choice as a run):
  * every row whose name or block path looks like a protection on/off,
    detection, sectionaliser or fault-indication flag, with its position
  * what update_device_function decides now, and what an order-independent
    scan would decide
and writes every row of every device to CSV.

Run from the IPStoPF folder with the normal VM Python (no PowerFactory
needed). It only issues SELECTs against the ODS.

    "C:\\Program Files\\Python312\\python.exe" diag_device_function.py
"""

import copy
import csv
import os
import re
import time
from contextlib import closing

from core.protection_device import ProtectionDevice
from ips_data import ods_connection
from ips_data.query_database import ENERGEX_BATCH_SQL, batch_get_ips_settings
from update_powerfactory import relay_settings as rs

OUT_DIR = os.path.join(
    r"C:\LocalData\PowerFactory Output Folders\ProtectionBatchRunner",
    f"device_function_diag_{time.strftime('%Y%m%d_%H%M%S')}",
)

# Every IPS record with a SW device ID in the Burleigh Heads 28 Oct run:
# (plant number, setting ID, IPS pattern, what IPStoPF did).
DEVICES = [
    ("X1063730-A_SW510", "43BB952C-5857-47A1-9658-343A6E2BEE5A", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X1063768_SW1137", "413B62E0-705B-4226-ABB4-C0326FDC38F4", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X11854-E_SW1308", "C5BCC1A3-AB89-4B78-84AD-0EB6C5F172DD", "WSOS5_Energex", "switch"),
    ("X120080_SW1092", "3D6647D7-4983-475A-A03A-7712E55DFE07", "ADVC2 WSOS5 V5.13.26 ", "switch"),
    ("X15189-F_SW797", "21CAC0D0-BD34-49C5-A0C5-293FAAF4C494", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X231119_SW1487", "AE00A474-D2A7-4464-95D9-ABD237D12379", "WSOS5_Energex", "switch"),
    ("X39484-E_SW1089", "B45A6089-FD9F-47CC-BA1C-B567C82EED1C", "ADVC2 WSOS5 V5.13.26 ", "switch"),
    ("X6860-F_SW445", "616DF6E1-1FF8-427E-BD0C-47082D48EEC7", "WSOS4_Energex", "sectionaliser"),
    ("X92613-B_SW1427", "0B749F13-501F-40B2-9353-2A51A49CCDEB", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X92618-B_SW1304", "16464C65-D38E-4516-BD92-213C9304B807", "WSOS5_Energex", "switch"),
    ("X11864-F_SW611", "BFCF69FE-2F38-4845-949C-AF8ED17E4350", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X319756_SW1271", "52663325-C714-4FA3-9709-2E6017C06FB8", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X7506-E_SW1308", "80709887-F743-415E-BB8F-789588EB3E8C", "WSOS5_Energex", "switch"),
    ("X784414_SW1327", "6F68C9DF-A5FA-4E70-82CF-63C2D6C3EE7D", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X1063829_SW1087", "4A5688B4-DDFB-4B61-937B-8A1561B02E4E", "EQL_ADVC3_ADVC2_5.16", "sectionaliser"),
    ("X9009-F_SW1809", "7B388CD0-3E06-4D3F-A058-0E68A8ED15FB", "WSOS5_Energex", "switch"),
    ("X8838-D_SW1307", "602A2E1D-05E5-45A2-B000-1BA655329CD7", "WSOS5_Energex", "switch"),
    ("X38655-A_SW1351", "32B722F7-8262-405B-8E59-755FEF04F5B9", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X40810-C_SW1396", "E70986BE-1F4A-44E1-93DC-DD368018107A", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X806577-A_SW1042", "F77610B6-BF72-4922-BCD3-CCC1AF6E311E", "EQL_ADVC3_ADVC2_5.16", "sectionaliser"),
    ("X810720_SW1375", "7B3077FA-C33E-4F58-BD3D-988CC5A31A50", "EQL_ADVC3_ADVC2_5.16", "switch"),
    ("X8999-E_SW1354", "B1B11167-98EA-40FD-B089-EA2172DCDF5C", "WSOS5_Energex", "in service"),
    ("X120489_SW1195", "80CE06A3-D3C1-41FB-A297-9D1AC77675C6", "ADVC2 WSOS5 V5.13.26 ", "in service"),
    ("X13369-E_SW1335", "02C3B26F-DEA5-4D1B-B9F7-8326355AD7F9", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X15402-C_SW780", "FD3B783C-15AC-4484-8099-B6AD24219CBC", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X810721_SW1417", "5955BB1C-680F-4957-AD63-572E1FE4DBE4", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X8864-D_SW1007", "E7A085C4-C4FC-49F5-972C-D5F4C2E53D55", "ADVC2 WSOS5 V5.13.26 ", "switch"),
    ("X21916-D_SW1397", "7E4BE4F0-9A2F-487A-A810-8AA012198E0F", "EQL_ADVC3_ADVC2_5.16", "switch"),
    ("X7621-B_SW787", "F63F224E-A328-4E1C-931A-62A76493E00C", "EQL_ADVC3_ADVC2_5.16", "in service"),
    ("X1063842_SW1150", "0D8BD384-E1C6-4F28-8FD4-4F0F854639BC", "EQL_ADVC3_ADVC2_5.16", "sectionaliser"),
    ("X1504641_SW1310", "65E8D3DF-F349-43F1-909D-198205AA9FA6", "EQL_ADVC3_ADVC2_5.16", "switch"),
    ("X15246-D_SW691", "AB40586F-8BDB-4CC9-A49B-F8C8FE4D8533", "EQL_ADVC3_ADVC2_5.16", "switch"),
    ("X15260-C_SW833", "4C71071A-20DA-43BE-B9A1-698F4385871C", "EQL_ADVC3_ADVC2_5.16", "switch"),
    ("X15408-C_SW1319", "79858585-593B-4E36-9A60-B30D9A329923", "EQL_ADVC3_ADVC2_5.16", "switch"),
    ("X21608-D_SW669", "1D6E0173-FC2B-45CD-9A82-87C8F484FFBB", "EQL_ADVC3_ADVC2_5.16", "switch"),
    ("X26632-C_SW1338", "BCCC03B5-854D-42F4-B4A2-4E6A532FBA86", "EQL_ADVC3_ADVC2_5.16", "switch"),
    ("X792271_SW882", "D0D2AE88-73B1-4F03-96B4-0BBC81BA7AEA", "EQL_ADVC3_ADVC2_5.16", "switch"),
    ("X15278-B_SW994", "DC3009D9-4745-43EF-B38D-8FCBC70087E7", "EQL_ADVC3_ADVC2_5.16", "switch"),
    ("X63538-D_SW1314", "BBACC29E-016B-4B6B-AFD1-AA24263E3BA3", "EQL_ADVC3_ADVC2_5.16", "sectionaliser"),
    ("X63541-C_SW1311", "A18995A3-5CED-4D20-81FA-EF5286CBFB71", "EQL_ADVC3_ADVC2_5.16", "sectionaliser"),
]

# Parameter names / block paths worth looking at for a protection on/off or
# fault-indication flag.
FLAG_PATTERN = re.compile(
    r"detect|sectional|indicat|fpi|passage|protection\s*(on|off|enable|mode)?$"
    r"|operat\w*\s*mode|^mode$|trip\s*(enable|mode)|lbs|work\s*tag",
    re.IGNORECASE,
)

SECTIONALISER_ON = ("on", "auto")
DETECTION_ON = ("on",)


def current_decision(pattern, rows):
    """update_device_function exactly as deployed, on a scratch device."""
    dev = ProtectionDevice(None, pattern, "diag", "diag", None, None, "")
    dev.settings = copy.deepcopy(rows)
    rs.update_device_function(dev)
    for prefix in ("sect_", "switch_"):
        if dev.device.startswith(prefix):
            return prefix.rstrip("_")
    return "protection"


def scan_decision(rows):
    """Order-independent: every Sectionaliser / Detection row is read."""
    sect = any(r[1] == "Sectionaliser" and str(r[2]).lower() in SECTIONALISER_ON for r in rows)
    detect = any(r[1] == "Detection" and str(r[2]).lower() in DETECTION_ON for r in rows)
    if sect:
        return "sect"
    if detect:
        return "switch"
    return "protection"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    ids = [d[1] for d in DEVICES]
    with closing(ods_connection.connect_to_db("Energex")) as connection:
        raw = batch_get_ips_settings(
            None, connection, ids, ENERGEX_BATCH_SQL, skip_empty_setting=False
        )

    summary, all_rows = [], []
    for name, sid, pattern, outcome in DEVICES:
        dev = ProtectionDevice(None, pattern, name, sid, None, None, "")
        dev.associated_settings(raw)
        rows = dev.settings
        for i, r in enumerate(rows):
            all_rows.append([name, i] + list(r))

        now = current_decision(pattern, rows) if rows else "switch (no settings)"
        scan = scan_decision(rows) if rows else "switch (no settings)"
        flags = [(i, r) for i, r in enumerate(rows)
                 if FLAG_PATTERN.search(str(r[1])) or FLAG_PATTERN.search(str(r[0]))]

        print(f"\n=== {name}  ({pattern.strip()}, {len(rows)} rows, run: {outcome})")
        print(f"    decision now: {now:<24} order-independent scan: {scan}")
        for i, r in flags[:40]:
            print(f"    [{i:4}] {r[0]} | {r[1]} = {r[2]} {r[3] or ''}")
        if len(flags) > 40:
            print(f"    ... {len(flags) - 40} more flag-like rows in the CSV")
        summary.append([name, pattern.strip(), outcome, len(rows), now, scan,
                        "; ".join(f"[{i}] {r[1]}={r[2]}" for i, r in flags
                                  if r[1] in ("Sectionaliser", "Detection"))])

    with open(os.path.join(OUT_DIR, "01_summary.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["device", "pattern", "run_outcome", "rows", "decision_now",
                    "decision_scan", "sectionaliser_detection_rows"])
        w.writerows(summary)
    with open(os.path.join(OUT_DIR, "02_all_rows.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["device", "row", "blockpath", "parameter", "value", "unit"])
        w.writerows(all_rows)

    changed = [s for s in summary if s[4] != s[5]]
    print(f"\n{len(changed)} of {len(summary)} devices change decision under the "
          f"order-independent scan: {[s[0] for s in changed]}")
    print(f"Output: {OUT_DIR}")


if __name__ == "__main__":
    main()
