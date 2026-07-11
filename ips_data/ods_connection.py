"""
Direct Oracle ODS connection for batch protection-setting retrieval.

The interactive path queries IPS through the NetDash API, which can only
filter one setting ID per request, costing one round trip per relay. For
unattended batch runs (the ProtectionBatchRunner driver) a privileged user
can connect straight to the Oracle ODS, where the IPS data is mirrored, and
filter on a list of setting IDs in a single query. This module owns that
connection: credential loading, host/service resolution, thick-mode
initialisation, and the oracledb connection itself.

Nothing here touches the PowerFactory ``app`` object, so it is importable and
unit-testable offline. ``oracledb`` is imported lazily inside the connection
functions so that merely importing this module does not require the driver to
be installed. The EDW server is pre-12.1, so oracledb runs in THICK mode and
requires Oracle Instant Client 19c at ``INSTANT_CLIENT_DIR`` on the execution
machine; without it, connect_to_db raises ODSUnavailable and the caller falls
back to NetDash.

When a connection cannot be established (no Oracle client, no credential file,
or the database is unreachable) ``connect_to_db`` raises :class:`ODSUnavailable`
so the caller can fall back to the slower NetDash path instead of failing.
"""

import yaml
from tenacity import retry, stop_after_attempt, wait_random_exponential

from logging_config import get_logger

logger = get_logger(__name__)


class ODSUnavailable(Exception):
    """Raised when a direct ODS connection cannot be established.

    Signals the caller (``query_database.batch_settings``) that the bulk ODS
    fetch is not possible in this environment and the NetDash per-ID path
    should be used instead. Covers a missing Oracle client, an absent or
    malformed credential file, and an unreachable/unauthenticated database. It
    deliberately does NOT cover errors raised once a connection is open (e.g. a
    failed query) - those propagate so genuine bugs are not silently masked.
    """


# Primary and Citrix-fallback locations of the ODS credential file.
SQL_LOGIN_PATHS = [
    r"C:\LocalData\ProtectionBatchRunner\sql_login.yaml",
    r"\\Client\C$\localdata\ProtectionBatchRunner\sql_login.yaml",
]

# ODS [scan host, service name] per region. Move to config/ if you prefer central path management.
ODS_TARGETS = {
    # "Energex": [
    #     "cbnf1c02vm01-vip.au1.ocm.s7130879.oraclecloudatcustomer.com",
    #     "XEDWHPS1.au1.ocm.s7130879.oraclecloudatcustomer.com",
    # ],
    "Energex": [
        "sbnsaxpa-scan1",
        "XEDWHPR1.SRV",
    ],
    "Ergon": [
        "cbns1c-scan1",
        "ERG_EDW_PROD.au2.ocm.s7134658.oraclecloudatcustomer.com",
    ],
}

ODS_PORT = 1521

# The EDW server predates Oracle 12.1, which python-oracledb's thin mode
# requires (DPY-3010 on connect). Thick mode via Instant Client 19c
# (supports 11.2+ servers) is therefore mandatory, not optional.
INSTANT_CLIENT_DIR = r"C:\LocalData\ProtectionBatchRunner\instantclient_19_25"

_thick_mode_ready = False


def _init_thick_mode(oracledb):
    """Switch oracledb to thick mode, once per process."""
    global _thick_mode_ready
    if not _thick_mode_ready:
        oracledb.init_oracle_client(lib_dir=INSTANT_CLIENT_DIR)
        _thick_mode_ready = True


def _load_sql_login():
    """Load the ODS credential yaml from the first path that opens."""
    last_error = None
    for path in SQL_LOGIN_PATHS:
        try:
            with open(path) as yaml_f:
                return yaml.safe_load(yaml_f)
        except OSError as exc:
            last_error = exc
            continue
    raise FileNotFoundError(
        f"Could not open ODS credential file at any of {SQL_LOGIN_PATHS}"
    ) from last_error


def user_name_password(region):
    """Return [username, password] for the region's ODS account."""
    d = _load_sql_login()
    if region == "Energex":
        return [d["seq_user"], d["seq_password"]]
    return [d["reg_user"], d["reg_password"]]


def determine_ips_db(region):
    """Return the [scan host, service name] pair for the region's ODS."""
    try:
        return ODS_TARGETS[region]
    except KeyError:
        raise ValueError(f"No ODS target configured for region '{region}'")


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_random_exponential(multiplier=1, max=5),
)
def _connect_with_retry(username, password, ips_db):
    """Open the oracledb connection, retrying transient failures only.

    Separated from connect_to_db so the retry budget is spent on the
    (potentially transient) connection attempt, not on deterministic failures
    like a missing client or credential file.
    """
    import oracledb

    tns = oracledb.makedsn(ips_db[0], ODS_PORT, service_name=ips_db[1])
    logger.info(f"Opening ODS connection to {ips_db[1]}")
    return oracledb.connect(user=username, password=password, dsn=tns)


def connect_to_db(region):
    """Open a direct oracledb connection to the region's IPS ODS.

    Raises:
        ODSUnavailable: if the Oracle client is missing, the credential file is
            absent/malformed, or the database cannot be reached. The caller
            should fall back to the NetDash path on this exception.
    """
    try:
        import oracledb
    except ImportError as exc:
        raise ODSUnavailable("oracledb is not installed") from exc

    try:
        ips_db = determine_ips_db(region)
        username, password = user_name_password(region)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        raise ODSUnavailable(f"ODS credentials/target unavailable: {exc}") from exc

    try:
        _init_thick_mode(oracledb)
        return _connect_with_retry(username, password, ips_db)
    except oracledb.Error as exc:
        raise ODSUnavailable(f"Could not connect to ODS: {exc}") from exc