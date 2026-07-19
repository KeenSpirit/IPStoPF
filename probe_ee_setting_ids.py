"""Probe the Ergon setting-ID report: schema + per-asset multiplicity."""
import sys
from collections import Counter
from pathlib import Path

# Import paths from config and add to sys.path
from config.paths import ASSET_CLASSES_PATH

sys.path.append(ASSET_CLASSES_PATH)
import assetclasses
from assetclasses.corporate_data import get_cached_data

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ips_data.query_database import _create_ids_dict

rows = _create_ids_dict("Ergon")
print(f"rows: {len(rows)}")
if not rows:
    sys.exit("no rows returned")

print(f"keys in first row: {sorted(rows[0].keys())}")

per_asset = Counter(r.get("assetname") for r in rows)
sizes = Counter(per_asset.values())
print(f"distinct assets: {len(per_asset)}, "
      f"mean records/asset: {len(rows)/len(per_asset):.1f}")
print("records-per-asset histogram (count: n_assets):",
      dict(sorted(sizes.items())))
print("\nworst 5 assets:")
for asset, n in per_asset.most_common(5):
    print(f"  {asset!r}: {n} records")
    for r in [x for x in rows if x.get("assetname") == asset][:10]:
        print(f"    id={r.get('relaysettingid')}  date={r.get('datesetting')}  "
              f"pattern={r.get('patternname')}")

from ips_data.setting_index import create_setting_index

idx = create_setting_index(rows, "Ergon")
print(f"\nindex records after filtering: {len(idx)}")
print("active values in report:", dict(Counter(r.get("active") for r in rows)))

# How promiscuous is the prefix index?
bucket_sizes = Counter(len(v) for v in idx._by_asset_prefix.values())
print("prefix bucket size histogram (bucket_size: n_prefixes):",
      dict(sorted(bucket_sizes.items())[-10:]))
print("\n10 largest prefix buckets:")
for n, prefix in sorted(
    ((len(v), k) for k, v in idx._by_asset_prefix.items()), reverse=True
)[:10]:
    print(f"  {prefix!r}: {n} records")

from ips_data.query_database import get_cached_data

it_rows = list(get_cached_data("Report-Cache-ProtectionITSettings-EE", max_age=3) or [])
print(f"\nIT report rows via cache layer: {len(it_rows)}")
if it_rows:
    it_ids = {r.relaysettingid for r in it_rows}
    sid_ids = {r.get("relaysettingid") for r in rows}
    print(f"distinct IT ids: {len(it_ids)}; "
          f"overlap with setting-ID report: {len(it_ids & sid_ids)}")