"""
Read-only diagnostic: why do some IPS setting IDs return the same
(block path, parameter) more than once with different values?

Run from the IPStoPF folder with the normal VM Python (no PowerFactory
needed). It only issues SELECTs against the ODS and writes CSVs.

    "C:\\Program Files\\Python312\\python.exe" diag_duplicate_settings.py

Outputs (in OUT_DIR):
    01_schema.csv          columns of the four IPS tables involved
    02_rows.csv            every setting row for the IDs below, with the
                           row IDs the batch query currently throws away
    03_conflicts.csv       one line per duplicated (setting, block, param)
                           and which ID differs between the copies
    04_paramsets.csv       full IPS_RELAYPARAMSET rows involved
    05_parmodels.csv       full IPS_RELPARMODEL rows involved in conflicts

The verdict printed at the end says which of four explanations the data
supports:
    H1 more than one RelayParamSet per setting
    H2 more than one RelParModel with the same block path and address
       (e.g. firmware variants inside one pattern)
    H3 more than one RelayParam row for the same set and model
    H4 identical IDs, so the join itself fans out
"""

import os
import time
from contextlib import closing

import pandas as pd

from ips_data import ods_connection

REGION = "Energex"

OUT_DIR = os.path.join(
    r"C:\LocalData\PowerFactory Output Folders\ProtectionBatchRunner",
    f"dup_settings_diag_{time.strftime('%Y%m%d_%H%M%S')}",
)

# Setting IDs from Report-Cache-ProtectionSettingIDs-EX (28 Sep run).
SETTING_IDS = {
    "RIS4_J08 (P123_Energex, I> switched out)": "6D8C6FE8-B2A3-4B15-8627-C2042DB2558D",
    "RIS2_J08 (P123_Energex)": "43F0D93F-7130-4E25-AACB-4366FBE9107F",
    "BLN13A_J17A (P123 V13 V14 HEX)": "6D61E939-660A-426A-A0A6-73C64AB22F1C",
    "X14951-A_SE2326 (EQL_ADVC3_ADVC2_5.16)": "EF487044-DA48-4B81-A2B3-E4D7567F35B5",
}

SCHEMA_SQL = """
SELECT table_name, column_id, column_name, data_type
FROM all_tab_columns
WHERE owner = 'EDW_LDG_OWNER'
  AND table_name IN ('IPS_RELAYSETTING', 'IPS_RELAYPARAMSET',
                     'IPS_RELAYPARAM', 'IPS_RELPARMODEL')
ORDER BY table_name, column_id
"""

# Same joins and filter as ENERGEX_BATCH_SQL, plus the row IDs.
ROWS_SQL = """
SELECT
    relaysetting.relaysettingid,
    relayparamset.relayparamsetid,
    relparmodel.relparmodelid,
    relayparam.relayparamid,
    relparblock.relparblockid,
    relparblock.blockpathenu,
    relparmodel.paramnameenu,
    CASE
        WHEN relparmodel.datatype = 'Enum'
        THEN CAST(relparenumitem.textenu AS NVARCHAR2(2000))
        ELSE CAST(relayparam.actual AS NVARCHAR2(2000))
    END AS proposedsetting,
    relparmodel.unitenu
FROM
    edw_ldg_owner.ips_relparblock relparblock_2
    INNER JOIN edw_ldg_owner.ips_relparblock relparblock_1 ON
        relparblock_2.relparblockid = relparblock_1.parentrowid
    RIGHT OUTER JOIN (
        edw_ldg_owner.ips_relayparam relayparam
        INNER JOIN edw_ldg_owner.ips_relayparamset relayparamset ON
            relayparam.relayparamsetid = relayparamset.relayparamsetid
        INNER JOIN edw_ldg_owner.ips_relaysetting relaysetting ON
            relayparamset.relaysettingid = relaysetting.relaysettingid
        INNER JOIN edw_ldg_owner.ips_relparmodel relparmodel ON
            relayparam.relparmodelid = relparmodel.relparmodelid
        INNER JOIN edw_ldg_owner.ips_relparblock relparblock ON
            relparmodel.relparblockid = relparblock.relparblockid
        INNER JOIN edw_ldg_owner.ips_mntasset mntasset ON
            relaysetting.assetid = mntasset.assetid
        LEFT OUTER JOIN edw_ldg_owner.ips_relparenumitem relparenumitem ON
            relayparam.actual = relparenumitem.relparenumitemid
        LEFT OUTER JOIN edw_ldg_owner.ips_relparenum relparenum ON
            relparmodel.relparenumid = relparenum.relparenumid
    ) ON relparblock_1.relparblockid = relparblock.parentrowid
WHERE relaysetting.relaysettingid IN ({in_clause})
    AND relayparam.actual IS NOT NULL
"""

ID_COLS = ["RELAYPARAMSETID", "RELPARMODELID", "RELAYPARAMID", "RELPARBLOCKID"]


def query(connection, sql, binds=None):
    with closing(connection.cursor()) as cursor:
        cursor.execute(sql, binds or {})
        cols = [d[0].upper() for d in cursor.description]
        return pd.DataFrame(cursor.fetchall(), columns=cols)


def in_query(connection, sql_template, values):
    """Run sql_template with {in_clause} bound to values (<1000)."""
    values = list(values)
    if not values:
        return pd.DataFrame()
    binds = {f"id{i}": v for i, v in enumerate(values)}
    in_clause = ", ".join(f":{k}" for k in binds)
    return query(connection, sql_template.replace("{in_clause}", in_clause), binds)


def classify(group):
    """Which ID differs between the copies of one (setting, block, param)."""
    if group["RELAYPARAMSETID"].nunique() > 1:
        return "H1 multiple param sets"
    if group["RELPARMODELID"].nunique() > 1:
        return "H2 multiple param models"
    if group["RELAYPARAMID"].nunique() > 1:
        return "H3 multiple param rows"
    return "H4 join fan-out"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    label_of = {v: k for k, v in SETTING_IDS.items()}

    with closing(ods_connection.connect_to_db(REGION)) as connection:
        schema = query(connection, SCHEMA_SQL)
        schema.to_csv(os.path.join(OUT_DIR, "01_schema.csv"), index=False)

        rows = in_query(connection, ROWS_SQL, SETTING_IDS.values())
        rows["DEVICE"] = rows["RELAYSETTINGID"].map(label_of)
        rows = rows.sort_values(
            ["DEVICE", "BLOCKPATHENU", "PARAMNAMEENU"] + ID_COLS
        )
        rows.to_csv(os.path.join(OUT_DIR, "02_rows.csv"), index=False)

        key = ["DEVICE", "BLOCKPATHENU", "PARAMNAMEENU"]
        dup = rows[rows.duplicated(key, keep=False)]
        conflicts = []
        for (device, block, param), g in dup.groupby(key):
            conflicts.append({
                "DEVICE": device,
                "BLOCKPATHENU": block,
                "PARAMNAMEENU": param,
                "COPIES": len(g),
                "DISTINCT_VALUES": g["PROPOSEDSETTING"].nunique(dropna=False),
                "VALUES": " | ".join(map(str, g["PROPOSEDSETTING"])),
                "CAUSE": classify(g),
                **{f"N_{c}": g[c].nunique() for c in ID_COLS},
                "PARAMSETIDS": " | ".join(map(str, g["RELAYPARAMSETID"])),
                "PARMODELIDS": " | ".join(map(str, g["RELPARMODELID"])),
            })
        conflicts = pd.DataFrame(conflicts)
        conflicts.to_csv(os.path.join(OUT_DIR, "03_conflicts.csv"), index=False)

        paramsets = in_query(
            connection,
            "SELECT * FROM edw_ldg_owner.ips_relayparamset "
            "WHERE relayparamsetid IN ({in_clause})",
            rows["RELAYPARAMSETID"].dropna().unique()[:999],
        )
        paramsets.to_csv(os.path.join(OUT_DIR, "04_paramsets.csv"), index=False)

        model_ids = (
            dup["RELPARMODELID"].dropna().unique()[:999]
            if not dup.empty else []
        )
        parmodels = in_query(
            connection,
            "SELECT * FROM edw_ldg_owner.ips_relparmodel "
            "WHERE relparmodelid IN ({in_clause})",
            model_ids,
        )
        parmodels.to_csv(os.path.join(OUT_DIR, "05_parmodels.csv"), index=False)

    # ---- console verdict -------------------------------------------------
    print(f"\nOutput folder: {OUT_DIR}\n")
    for label, sid in SETTING_IDS.items():
        r = rows[rows["RELAYSETTINGID"] == sid]
        c = conflicts[conflicts["DEVICE"] == label] if not conflicts.empty else conflicts
        print(f"{label}")
        print(f"  rows {len(r)}, param sets {r['RELAYPARAMSETID'].nunique()}, "
              f"duplicated (block, param) {len(c)}, "
              f"of which values differ {int((c['DISTINCT_VALUES'] > 1).sum()) if len(c) else 0}")
        if len(c):
            print("  causes:", c["CAUSE"].value_counts().to_dict())
    print("\nParam sets per setting (columns of IPS_RELAYPARAMSET):")
    print(paramsets.to_string(max_colwidth=40))


if __name__ == "__main__":
    main()