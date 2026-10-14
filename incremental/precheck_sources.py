"""
Real data sources for the incremental precheck.

Each source reads through the very function the transfer uses, so the
precheck sees what the run would see, and through the same per-process
caches, so data the precheck loads is reused by the run that follows:

    setting_index       query_database.get_setting_ids (cached per region)
    skeleton_registers  add_relay_skeletons._skeleton_dicts (cached)
    it_rows             query_database.seq/reg_get_ips_it_details (cached report)
    ods_digests         query_database.fetch_ods_digests on a new connection
    relay_map_path,     config.paths
    shared_file_paths

No PowerFactory object is read here; ``app`` is only passed through to
get_setting_ids for its error message. Everything is fetched lazily by
precheck.run_precheck, and any failure there makes that project FULL.
"""

from contextlib import closing
from typing import Any, Dict, List

from incremental.precheck import PrecheckSources


def build_sources(app: Any, region: str) -> PrecheckSources:
    """
    Sources for one IPS region ("Energex" or "Ergon").

    Build once per region per run and reuse it for every project of that
    region: the expensive parts are cached inside the IPStoPF modules.
    """
    from config import paths
    from ips_data import query_database as qd

    def setting_index():
        return qd.get_setting_ids(app, region)

    def skeleton_registers():
        from ips_data import add_relay_skeletons as ars
        relay, recloser, fuse, gas_switch = ars._skeleton_dicts()
        return {
            "relay": relay,
            "recloser": recloser,
            "fuse": fuse,
            "gas_switch": gas_switch,
        }

    def it_rows(ids: List[str]):
        if region == "Energex":
            return qd.seq_get_ips_it_details(app, ids)
        return qd.reg_get_ips_it_details(app, ids)

    def ods_digests(ids: List[str]) -> Dict[str, str]:
        from ips_data import ods_connection
        with closing(ods_connection.connect_to_db(region)) as connection:
            return qd.fetch_ods_digests(connection, ids)

    return PrecheckSources(
        setting_index=setting_index,
        skeleton_registers=skeleton_registers,
        it_rows=it_rows,
        ods_digests=ods_digests,
        relay_map_path=paths.get_relay_map_file,
        shared_file_paths={
            "type_mapping.csv": paths.get_type_mapping_file(),
            "curve_mapping.csv": paths.get_curve_mapping_file(),
            "CB_ALT_NAME.csv": paths.get_cb_alt_name_file(),
        },
    )