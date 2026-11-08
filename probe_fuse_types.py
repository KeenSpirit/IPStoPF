"""
List every fuse type in ErgonLibrary\\Protection\\Fuses (subfolders
included) and check named_fuse_types.csv against it, so the CSV can be
filled in with exact TypFuse names.

READ-ONLY. No project is activated; nothing is written in PowerFactory.

Run from the IPStoPF root, externally (like ips_to_pf_batch.py) by default,
or from inside PowerFactory as a ComPython script (set
RUN_INSIDE_POWERFACTORY).

Output, in OUTPUT_DIR:
  fuse_types_<stamp>.csv     one row per TypFuse: folder, name, the
                             (class, rating) key IPStoPF matches links on
                             (blank = matched by name only, i.e. through
                             named_fuse_types.csv), and a few type
                             attributes where the type has them
  named_fuse_check_<stamp>.csv  one row per named_fuse_types.csv row: the
                             name's status (found / NOT IN LIBRARY / blank)
                             and, for blank rows, unkeyed library types
                             whose name carries the same rating number,
                             as candidates to choose from
"""

import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.paths import get_named_fuse_types_file  # noqa: E402
from update_powerfactory.type_index import (  # noqa: E402
    _LINK_NAME, load_named_fuse_types, normalise_curve, normalise_rating,
)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
PF_INSTALL_DIR = r"C:\Program Files\DIgSILENT\PowerFactory 2025 SP3"
PF_PYTHON_DIR = rf"{PF_INSTALL_DIR}\Python\3.12"
PF_INI_FILE = "PowerFactory_PRD.ini"
CONFIG_YAML = r"C:\LocalData\ProtectionBatchRunner\config.yaml"

OUTPUT_DIR = r"C:\LocalData\PF fuse type probe"

# CSV to check; None uses the one IPStoPF reads (next to type_mapping.csv).
NAMED_FUSE_TYPES_CSV = None

RUN_INSIDE_POWERFACTORY = False

FUSE_FOLDER = r"\ErgonLibrary\Protection\Fuses.IntFolder"

# Type attributes reported when the TypFuse has them (blank otherwise).
ATTRIBUTES = ("irat", "urat", "itype", "desc")


def _attr(obj, name):
    try:
        value = obj.GetAttribute(name)
    except Exception:
        return ""
    if isinstance(value, (list, tuple)):
        value = " | ".join(str(v) for v in value if str(v).strip())
    return "" if value is None else str(value)


def _folder_path(obj, top):
    """Folder of obj relative to the Fuses folder ('' = Fuses itself)."""
    parts = []
    top_name = top.GetFullName()
    parent = obj.GetParent()
    while parent is not None and parent.GetFullName() != top_name:
        parts.append(parent.loc_name)
        parent = parent.GetParent()
    return "\\".join(reversed(parts))


def list_fuse_types(app):
    top = app.GetGlobalLibrary().SearchObject(FUSE_FOLDER)
    if not top:
        raise RuntimeError(f"{FUSE_FOLDER} not found in the global library")
    rows = []
    for fuse in top.GetContents("*.TypFuse", 1) or []:
        folder = _folder_path(fuse, top)
        name = fuse.loc_name
        key = ""
        if folder == "":
            match = _LINK_NAME.search(" ".join(name.split()))
            if match:
                key = (f"{match.group(2).upper()} "
                       f"{normalise_rating(match.group(1))}")
        row = {"folder": folder or "(Fuses)", "typfuse_name": name,
               "link_key": key}
        row.update({a: _attr(fuse, a) for a in ATTRIBUTES})
        rows.append(row)
    rows.sort(key=lambda r: (r["folder"], r["typfuse_name"]))
    return rows


def _rating_numbers(name):
    """The last number in the name: the rating on SIBA/Bussmann-style names
    ('... SIBA 30 144 36  31.5', '... Bussmann 12OEFMA63'), where the
    numbers before it are catalogue codes."""
    numbers = re.findall(r"\d+(?:\.\d+)?", name)
    return {normalise_rating(numbers[-1])} if numbers else set()


def check_named(csv_path, types):
    by_name = {t["typfuse_name"] for t in types}
    unkeyed = [t["typfuse_name"] for t in types if not t["link_key"]]
    out = []
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except FileNotFoundError:
        print(f"No named fuse types file at {csv_path}")
        return out
    for row in rows:
        row = {str(k).strip().lower(): (v or "").strip()
               for k, v in row.items() if k is not None}
        curve, curve_rating = normalise_curve(row.get("ips_curve", ""))
        rating = normalise_rating(row.get("rating", "")) or curve_rating
        name = row.get("typfuse_name", "")
        if not name:
            status = "blank"
            # whole-amp match: '31' also offers 31.5 A types
            candidates = [
                n for n in unkeyed
                if rating and any(
                    num == rating or num.split(".")[0] == rating
                    for num in _rating_numbers(n)
                )
            ]
        else:
            status = "found" if name in by_name else "NOT IN LIBRARY"
            candidates = []
        out.append({
            "ips_curve": curve, "rating": rating, "typfuse_name": name,
            "status": status, "candidates": " | ".join(candidates),
        })
    # Rows IPStoPF will actually use, as a cross-check of the loader.
    used = load_named_fuse_types(csv_path)
    print(f"{len(used)} named rows with a TypFuse name; "
          f"{sum(1 for r in out if r['status'] == 'NOT IN LIBRARY')} "
          f"not in the library")
    return out


def _write(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]),
                                quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {path}")


def run(app):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    types = list_fuse_types(app)
    _write(out_dir / f"fuse_types_{stamp}.csv", types)
    keyed = sum(1 for t in types if t["link_key"])
    print(f"{len(types)} fuse types, {keyed} keyed as class links")
    csv_path = NAMED_FUSE_TYPES_CSV or get_named_fuse_types_file()
    _write(out_dir / f"named_fuse_check_{stamp}.csv",
           check_named(csv_path, types))


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
    if RUN_INSIDE_POWERFACTORY:
        import powerfactory as pf
        app = pf.GetApplication()
    else:
        app = _external_app()
    if app is None:
        print("Could not obtain the PowerFactory application")
        return 1
    run(app)
    return 0


if __name__ == "__main__":
    sys.exit(main())
