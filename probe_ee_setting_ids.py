"""Probe the Ergon setting-ID report: schema + per-asset multiplicity."""
import sys
from collections import Counter
from pathlib import Path

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