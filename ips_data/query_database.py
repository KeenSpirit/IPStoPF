"""
Database query functions for retrieving IPS protection device settings.

This module handles all interactions with the IPS database through the
NetDash API and corporate data caching layer. It provides functions for:
- Retrieving setting IDs for all devices in a region
- Fetching detailed settings for specific relay setting IDs
- Getting instrument transformer (CT/VT) details

The module uses the SettingIndex class for efficient O(1) lookups of
setting data instead of linear scans through lists.
"""

import sys
import time
from contextlib import closing
from typing import Dict, List, Tuple

# Import paths from config and add to sys.path
from config.paths import NETDASH_READER_PATH, ASSET_CLASSES_PATH

sys.path.append(NETDASH_READER_PATH)
from netdashread import get_json_data
from tenacity import retry, stop_after_attempt, wait_random_exponential

sys.path.append(ASSET_CLASSES_PATH)
import assetclasses
from assetclasses.corporate_data import get_cached_data

from ips_data import ods_connection
from ips_data.setting_index import SettingIndex, create_setting_index
from logging_config import get_logger

logger = get_logger(__name__)

# Cache for setting indexes to avoid rebuilding on repeated calls
_index_cache: Dict[str, SettingIndex] = {}

# Cache for raw setting-ID rows to avoid re-querying on repeated calls
_rows_cache: Dict[str, List[Dict]] = {}


def get_setting_ids(app, region: str) -> SettingIndex:
    """
    Retrieve all protection device setting IDs for a region.

    This function fetches the setting ID data from the corporate cache
    and returns an indexed structure for efficient lookups. The index
    is cached to avoid repeated processing.

    Args:
        app: PowerFactory application object
        region: "Energex" or "Ergon"

    Returns:
        SettingIndex object providing O(1) lookups by various keys

    Raises:
        TransferError: If unable to retrieve data after multiple attempts
    """

    # Check cache first
    cache_key = f"setting_index_{region}"
    if cache_key in _index_cache:
        return _index_cache[cache_key]

    # Fetch raw data with retry logic
    ids_dict_list = _fetch_setting_ids_with_retry(app, region)

    # Build indexed structure
    index = create_setting_index(ids_dict_list, region)

    # Cache for future use
    _index_cache[cache_key] = index

    return index


def get_setting_id_records(app, region: str) -> List[Dict]:
    """
    Retrieve the raw protection-device setting-ID rows for a region.

    This is the subtransmission analogue of :func:`get_setting_ids`. Where
    :func:`get_setting_ids` builds a :class:`SettingIndex` for switch-name
    matching (the distribution flow), the subtransmission pipeline matches on
    the canonical ``MappingKey`` parsed from each row's location path, so it
    needs the rows themselves rather than the index.

    The rows are sourced from the corporate cache (the same
    ``Report-Cache-ProtectionSettingIDs-EX`` / ``-EE`` report that
    :func:`get_setting_ids` consumes) with the existing retry logic, and are
    cached to avoid re-querying on repeated calls. Each row is a dict whose
    keys mirror the report columns (``patternname``, ``nameenu``,
    ``relaysettingid``, ``datesetting``, ``deviceid``, ``assetname``,
    ``locationpathenu``) - i.e. the same content the offline CSV export
    provided to ``process_ips.ingest_ips_export``.

    Args:
        app: PowerFactory application object
        region: "Energex" or "Ergon"

    Returns:
        List of setting-ID dictionaries (one per protection-device setting).

    Raises:
        TransferError: If unable to retrieve data after multiple attempts.
    """
    cache_key = f"setting_id_rows_{region}"
    if cache_key in _rows_cache:
        return _rows_cache[cache_key]

    rows = _fetch_setting_ids_with_retry(app, region)

    _rows_cache[cache_key] = rows

    return rows


def _fetch_setting_ids_with_retry(app, region: str, max_attempts: int = 5) -> List[Dict]:
    """
    Fetch setting IDs with retry logic for concurrent access issues.

    Args:
        app: PowerFactory application object
        region: "Energex" or "Ergon"
        max_attempts: Maximum number of retry attempts

    Returns:
        List of setting ID dictionaries

    Raises:
        SystemExit: If unable to retrieve data after max_attempts
    """
    ids_dict_list = []
    attempts = 0

    while len(ids_dict_list) == 0 and attempts < max_attempts:
        if attempts > 0:
            # Wait before retry if not first attempt
            time.sleep(10)

        ids_dict_list = _create_ids_dict(region)
        attempts += 1

        if attempts > 1:
            logger.warning(f"Retry attempt {attempts} for setting IDs")

    if len(ids_dict_list) == 0:
        logger.error("Could not create the setting ID dictionary")
        error_message(
            app,
            "Unable to obtain data for Setting IDs, please contact the Protection SME",
        )

    return ids_dict_list


def _create_ids_dict(region: str) -> List[Dict]:
    """
    Fetch raw setting ID data from corporate cache.

    Args:
        region: "Energex" or "Ergon"

    Returns:
        List of dictionaries containing setting ID data
    """
    report_name = (
        "Report-Cache-ProtectionSettingIDs-EX"
        if region == "Energex"
        else "Report-Cache-ProtectionSettingIDs-EE"
    )

    rows = get_cached_data(report_name, max_age=3)

    ids_dict_list = []
    for row in rows:
        try:
            ids_dict_list.append(dict(row._asdict()))
        except AttributeError:
            continue

    return ids_dict_list


class TransferError(Exception):
    """Raised when the settings transfer cannot proceed for this run.

    Replaces the legacy ``sys.exit(0)`` termination. Raising instead of
    exiting means:

    - In batch, the exception propagates out of ``main()`` to the
      per-project handler in IPStoPFMastering's ``batch_relay_update``,
      which records this project as failed and continues with the rest.
      The run exit code then reflects the failure instead of the false
      success that exit code 0 reported to Task Scheduler.
    - Interactively, ``main()`` catches it and stops cleanly with the
      message, mirroring the old behaviour without killing the process.
    """


def error_message(app, message: str) -> None:
    """
    Display an error message and abort the transfer.

    Library code must never call ``sys.exit()`` / ``exit()``: in an
    80-project batch that terminates the entire mastering process, not
    just the current project. The message is shown via ``PrintError``
    (visible even while the echo is suppressed, since ``echo(app)``
    leaves ``iopt_err`` enabled) and logged, then ``TransferError`` is
    raised for the caller to handle.

    Args:
        app: PowerFactory application object
        message: Error message to display

    Raises:
        TransferError: always.
    """
    logger.error(message)
    app.PrintError(message)
    raise TransferError(message)


def batch_settings(
    app,
    region: str,
    batch: bool,
    set_ids: List[str]
) -> Tuple[Dict[str, List[Dict]], List]:
    """
    Retrieve detailed settings for a batch of relay setting IDs.

    In batch mode the relay setting files are fetched from the Oracle ODS in a
    single query per <=1000-id chunk, bypassing NetDash. If a direct ODS
    connection cannot be established (no Oracle client, no credential file, or
    the database is unreachable), the fetch falls back to the NetDash per-ID
    path so callers without ODS access - e.g. an interactive subtransmission
    run - still complete, just more slowly. Errors raised after a connection is
    open (a failed query) are NOT caught here and propagate normally.

    In interactive distribution runs the relay settings are loaded eagerly per
    device elsewhere, so this function returns only the IT settings.

    Args:
        app: PowerFactory application object
        region: "Energex" or "Ergon"
        batch: True if the relay settings should be bulk-loaded here
        set_ids: List of relay setting IDs to fetch

    Returns:
        Tuple of (ips_settings dict, ips_it_settings list)
        - ips_settings: Dict mapping setting ID to list of setting records
        - ips_it_settings: List of instrument transformer setting records
    """
    ips_settings: Dict[str, List[Dict]] = {}

    if batch:
        sql = ENERGEX_BATCH_SQL if region == "Energex" else ERGON_BATCH_SQL
        # Energex filters empty settings in SQL (relayparam.actual IS NOT NULL);
        # Ergon filters them in Python, mirroring the legacy behaviour.
        skip_empty = region != "Energex"
        fetch_func = (
            seq_get_ips_settings if region == "Energex" else reg_get_ips_settings
        )
        try:
            with closing(ods_connection.connect_to_db(region)) as connection:
                ips_settings = batch_get_ips_settings(app,
                    connection, set_ids, sql, skip_empty_setting=skip_empty
                )
        except ods_connection.ODSUnavailable as exc:
            # Connection-level failure only - query errors propagate. Fall back
            # to the NetDash per-ID fetch so the run still completes.
            logger.warning(
                f"ODS bulk fetch unavailable ({exc}); falling back to NetDash "
                f"per-ID fetch for {len(set_ids)} setting IDs"
            )
            ips_settings = _fetch_settings_in_batches(app, set_ids, fetch_func)

    logger.info("Fetching instrument transformer details (cached report)")
    it_start = time.perf_counter()
    if region == "Energex":
        ips_it_settings = seq_get_ips_it_details(app, set_ids)
    else:
        ips_it_settings = reg_get_ips_it_details(app, set_ids)
    logger.info(
        f"IT details fetch returned {len(ips_it_settings)} records in "
        f"{time.perf_counter() - it_start:.1f} s"
    )

    return ips_settings, ips_it_settings

# ---------------------------------------------------------------------------
# Direct-ODS batch settings retrieval (bypasses NetDash)
#
# NetDash filters one setting ID per request. With a privileged ODS connection
# we filter on the whole list in a single statement, chunked under Oracle's
# 1000-element IN-list limit. Returns the same {set_id: [records]} shape as the
# per-ID seq_get_ips_settings / reg_get_ips_settings functions, so it drops
# straight into the existing ips_settings structure.
# ---------------------------------------------------------------------------

_BATCH_COLS = [
    "blockpathenu",
    "paramnameenu",
    "proposedsetting",
    "unitenu",
    "relaysettingid",
]

_ORACLE_IN_LIMIT = 1000

ENERGEX_BATCH_SQL = """
SELECT
    relparblock.blockpathenu, relparmodel.paramnameenu,
    CASE
        WHEN relparmodel.datatype = 'Enum'
        THEN CAST(relparenumitem.textenu AS NVARCHAR2(2000))
        ELSE CAST(relayparam.actual AS NVARCHAR2(2000))
    END AS proposedsetting, relparmodel.unitenu, relaysetting.relaysettingid
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
ORDER BY relaysetting.assetid
"""

ERGON_BATCH_SQL = """
SELECT  relparblock.blockpathenu,
        RelParModel.ParamNameENU,
        CASE
            WHEN RelParModel.DataType = 'Enum' THEN RelParEnumItem.TextENU
            ELSE RelayParam.Actual
        END AS ProposedSetting,
        RelParModel.UnitENU,
        RelaySetting.RelaySettingID
FROM    EDW_LDG_OWNER.IPS_RelParBlock RelParBlock_2
        INNER JOIN EDW_LDG_OWNER.IPS_RelParBlock RelParBlock_1 ON
            RelParBlock_2.RelParBlockID = RelParBlock_1.ParentRowID
        RIGHT OUTER JOIN (
            EDW_LDG_OWNER.IPS_RelayParam RelayParam
            INNER JOIN EDW_LDG_OWNER.IPS_RelayParamSet RelayParamSet ON
                RelayParam.RelayParamSetID = RelayParamSet.RelayParamSetID
            INNER JOIN EDW_LDG_OWNER.IPS_RelaySetting RelaySetting ON
                RelayParamSet.RelaySettingID = RelaySetting.RelaySettingID
            INNER JOIN EDW_LDG_OWNER.IPS_RelParModel RelParModel ON
                RelayParam.RelParModelID = RelParModel.RelParModelID
            INNER JOIN EDW_LDG_OWNER.IPS_RelParBlock RelParBlock ON
                RelParModel.RelParBlockID = RelParBlock.RelParBlockID
            INNER JOIN EDW_LDG_OWNER.IPS_MntAsset MntAsset ON
                RelaySetting.AssetID = MntAsset.AssetId
            LEFT OUTER JOIN EDW_LDG_OWNER.IPS_RelParEnumItem RelParEnumItem ON
                RelayParam.Actual = RelParEnumItem.RelParEnumItemID
            LEFT OUTER JOIN EDW_LDG_OWNER.IPS_RelParEnum RelParEnum ON
                RelParModel.RelParEnumID = RelParEnum.RelParEnumID
        ) ON RelParBlock_1.RelParBlockID = RelParBlock.ParentRowID
WHERE RelaySetting.RelaySettingID IN ({in_clause})
ORDER BY RelaySetting.AssetID
"""


def _chunked(items: List, size: int):
    """Yield successive sub-lists of at most ``size`` items."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def batch_get_ips_settings(app,
    connection,
    unique_ids: List[str],
    sql: str,
    *,
    skip_empty_setting: bool,
) -> Dict[str, List[Dict]]:
    """
    Bulk-fetch relay setting files from the ODS, one query per chunk.

    Uses named bind variables rather than string-concatenated literals (the
    legacy approach), which removes the injection/quoting hazard and is the
    correct Oracle idiom. The ID list is chunked under the 1000-element
    IN-list limit.

    Args:
        connection: open oracledb connection (ods_connection.connect_to_db)
        unique_ids: relay setting IDs to fetch
        sql: region SQL template containing a single ``{in_clause}`` token
        skip_empty_setting: drop rows whose proposedsetting is empty (Ergon)

    Returns:
        {setting_id: [ {blockpathenu, paramnameenu, proposedsetting,
                        unitenu, relaysettingid}, ... ], ... }
        Every requested ID is present as a key, even with no rows.
    """
    setting_id_dict: Dict[str, List[Dict]] = {sid: [] for sid in unique_ids}

    n_chunks = (len(unique_ids) + _ORACLE_IN_LIMIT - 1) // _ORACLE_IN_LIMIT
    fetch_start = time.perf_counter()
    n_rows = 0

    cursor = connection.cursor()
    try:
        for chunk_no, chunk in enumerate(
                _chunked(unique_ids, _ORACLE_IN_LIMIT), start=1
        ):
            logger.info(
                f"ODS batch fetch: chunk {chunk_no}/{n_chunks} "
                f"({len(chunk)} IDs) query started"
            )
            chunk_start = time.perf_counter()
            chunk_raw = 0
            chunk_kept = 0
            sample_dropped = None
            binds = {f"id{i}": sid for i, sid in enumerate(chunk)}
            in_clause = ", ".join(f":{name}" for name in binds)
            cursor.execute(sql.replace("{in_clause}", in_clause), binds)
            for row in cursor:
                chunk_raw += 1
                record = dict(zip(_BATCH_COLS, row))
                if skip_empty_setting and not record["proposedsetting"]:
                    if sample_dropped is None:
                        sample_dropped = record
                    continue
                # Assumes the ODS returns relaysettingid in the same form as the
                # IDs in unique_ids (as the legacy code did). If a KeyError ever
                # surfaces here, a str() coercion on both sides is the fix.
                setting_id_dict[record["relaysettingid"]].append(record)
                chunk_kept += 1
            n_rows += chunk_kept
            logger.info(
                f"ODS batch fetch: chunk {chunk_no}/{n_chunks} done in "
                f"{time.perf_counter() - chunk_start:.1f} s "
                f"({chunk_raw} rows fetched, {chunk_kept} kept)"
            )
            if chunk_raw and not chunk_kept and sample_dropped is not None:
                logger.warning(
                    f"ODS batch fetch: chunk {chunk_no} kept 0 of {chunk_raw} "
                    f"rows; sample dropped row: {sample_dropped}"
                )
    finally:
        cursor.close()

    logger.info(
        f"ODS batch fetch: {len(unique_ids)} setting IDs, {n_rows} setting "
        f"rows kept, in {n_chunks} query(ies), "
        f"{time.perf_counter() - fetch_start:.1f} s total"
    )
    return setting_id_dict


def _fetch_settings_in_batches(
    app,
    set_ids: List[str],
    fetch_func,
    batch_size: int = 900
) -> Dict[str, List[Dict]]:
    """
    Fetch settings one ID at a time via NetDash, combining the results.

    This is the per-ID fallback used when a direct ODS connection is not
    available. ``fetch_func`` is the region's single-ID fetch
    (``seq_get_ips_settings`` or ``reg_get_ips_settings``), each of which
    returns a ``{set_id: [records]}`` dict; the results are merged into one
    dict. ``batch_size`` only controls how often progress is logged, not the
    query - NetDash takes one ID per call, so this is one round trip per ID.

    Args:
        app: PowerFactory application object
        set_ids: List of setting IDs to fetch
        fetch_func: Per-ID fetch function returning {set_id: [records]}
        batch_size: How many IDs between progress log lines

    Returns:
        Combined dictionary of all settings, keyed by setting ID
    """
    ips_settings: Dict[str, List[Dict]] = {}

    for i, set_id in enumerate(set_ids):
        if i > 0 and i % batch_size == 0:
            logger.info(f"Processed {i} of {len(set_ids)} settings")

        settings = fetch_func(app, set_id)
        ips_settings.update(settings)

    return ips_settings


def seq_get_ips_it_details(app, devices: List[str]) -> List:
    """
    Get instrument transformer details for Energex (SEQ) devices.

    The CT/VT ratios are configured based on the CT/VT setting nodes
    attached to each relay setting node.

    Args:
        app: PowerFactory application object
        devices: List of relay setting IDs

    Returns:
        List of IT setting records that match the given devices
    """
    device_set = set(devices)  # Convert to set for O(1) lookup

    it_set_db = get_cached_data("Report-Cache-ProtectionITSettings-EX", max_age=3)

    ips_settings = []
    if it_set_db:
        for setting in it_set_db:
            if setting.relaysettingid in device_set:
                ips_settings.append(setting)

    return ips_settings


def reg_get_ips_it_details(app, devices: List[str]) -> List:
    """
    Get instrument transformer details for Ergon (Regional) devices.

    The CT/VT ratios are configured based on the CT/VT setting nodes
    attached to each relay setting node.

    Args:
        app: PowerFactory application object
        devices: List of relay setting IDs

    Returns:
        List of IT setting records that match the given devices
    """
    device_set = set(devices)  # Convert to set for O(1) lookup

    it_set_db = get_cached_data("Report-Cache-ProtectionITSettings-EE", max_age=3)

    ips_settings = []
    if it_set_db:
        for setting in it_set_db:
            if setting.relaysettingid in device_set:
                ips_settings.append(setting)

    return ips_settings


def seq_get_ips_settings(app, set_id: str) -> Dict[str, List[Dict]]:
    """
    Get full setting file for an Energex relay from the database.

    Args:
        app: PowerFactory application object
        set_id: The relay setting ID

    Returns:
        Dictionary mapping setting ID to list of setting records:
        {relaysettingid: [{blockpathenu, paramnameenu, proposedsetting, unitenu}, ...]}
    """
    settings = get_data("Protection-SettingRelay-EX", "setting_id", set_id)

    return {set_id: list(settings)}


def reg_get_ips_settings(app, set_id: str) -> Dict[str, List[Dict]]:
    """
    Get full setting file for an Ergon relay from the database.

    Args:
        app: PowerFactory application object
        set_id: The relay setting ID

    Returns:
        Dictionary mapping setting ID to list of setting records:
        {relaysettingid: [{blockpathenu, paramnameenu, proposedsetting, unitenu}, ...]}

    Note:
        Records with empty proposedsetting are filtered out for Ergon.
    """
    settings = get_data("Protection-SettingRelay-EE", "setting_id", set_id)

    # Filter out records with no proposed setting
    filtered_settings = [s for s in settings if s.get("proposedsetting")]

    return {set_id: filtered_settings}


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_random_exponential(multiplier=1, max=5),
)
def get_data(data_report: str, parameter: str, variable: str) -> List[Dict]:
    """
    Query the IPS database via NetDash API.

    This function includes automatic retry logic with exponential backoff
    to handle transient failures.

    Args:
        data_report: Name of the NetDash report to query
        parameter: Query parameter name
        variable: Query parameter value

    Returns:
        List of dictionaries containing query results
    """
    data = get_json_data(
        report=data_report,
        params={parameter: variable},
        timeout=120
    )

    if len(data) == 0:
        logger.warning(f"Query returned no data for {data_report} with {parameter}={variable}")

    return data