"""
IPS ingest: read the IPS protection-setting export into normalised records.

This module is the IPS side of the IPS <-> PowerFactory mapping pipeline. It is
deliberately free of any PowerFactory runtime dependency so it can be run and
unit-tested offline against the CSV export alone.

In-scope path categories
-------------------------
* ``Substations``       - the main subtransmission substations.
* ``Down Line Devices`` - standalone X-sites (line switches) on the network.
                          Only those at subtransmission voltage (>= 33 kV) are
                          retained; 11 kV (and other sub-33 kV) down line
                          devices belong to the distribution model and are
                          dropped.

All other categories (Stores, Mobile Generators, Powerlink, Decommissioned
Sites, test cases, ...) are out of scope.

Pipeline for each CSV row
--------------------------
1.  Parse the location path (column G) into its components:
        <region>/<category>/<site>/<voltage>/<designation>/
2.  Normalise the voltage segment to a numeric kV (or the literal ``"LV"``).
3.  Apply the category + scope rules above.
4.  For substation sites, translate the site code from the IPS form to the
    PowerFactory alpha form via ``config.region_config.get_substation_mapping``
    (a mapped value of ``None`` means the substation is intentionally skipped).
5.  Classify the designation into an :class:`IpsElementType` and, for
    substation elements, apply the 11 kV feeder / feeder-switch / line-switch
    exclusions.
6.  Emit an :class:`IpsDevice` keyed by ``MappingKey``.

Usage
-----
    from process_ips.ips_ingest import ingest_ips_export

    result = ingest_ips_export("ReportCacheProtectionSettingIDsEX.csv")
    print(result.total_in_scope, "in-scope devices")
    devices = result.by_key[MappingKey("ABM", 33, "BB31")]
"""
from __future__ import annotations

import csv
import re
from typing import Iterable, List, Optional

from config.region_config import get_substation_mapping

from domain.mapping_key import VoltageKv, MappingKey

from process_ips.ips_records import (
    ExcludedRow,
    ExclusionReason,
    IpsDevice,
    IpsElementType,
    IpsIngestResult,
)

_RE_NX_TRANSFORMER = re.compile(r"^NX(\d+)$")

# =============================================================================
# CSV column layout (Report-Cache-ProtectionSettingIDs-EX.csv)
# =============================================================================
# A patternname | B nameenu | C relaysettingid | D datesetting
# E deviceid    | F assetname | G locationpathenu
_COL_PATTERN = 0
_COL_SETTING_ID = 2
_COL_DATE = 3
_COL_DEVICE_ID = 4
_COL_ASSET = 5
_COL_PATH = 6

# Database/report field names for each column, in column order. Used to adapt
# the dict rows returned by the IPS query layer (production) to the positional
# row layout above (the same layout the offline CSV export produced). The names
# mirror the Report-Cache-ProtectionSettingIDs-EX report columns.
_DB_FIELDS_BY_COL = (
    "patternname",       # _COL_PATTERN (0)
    "nameenu",           # B - unused by ingest, retained for parity
    "relaysettingid",    # _COL_SETTING_ID (2)
    "datesetting",       # _COL_DATE (3)
    "deviceid",          # _COL_DEVICE_ID (4)
    "assetname",         # _COL_ASSET (5)
    "locationpathenu",   # _COL_PATH (6)
)


# =============================================================================
# Scope constants
# =============================================================================

# Path categories that may contain in-scope devices.
CATEGORY_SUBSTATIONS = "Substations"
CATEGORY_DOWN_LINE_DEVICES = "Down Line Devices"

# Lower voltage bound (kV) for the subtransmission model. Down Line Devices
# below this are distribution assets and are excluded.
SUBTRANSMISSION_MIN_KV = 33


# =============================================================================
# Path parsing
# =============================================================================

class ParsedPath:
    """Components of an IPS location path (column G)."""
    __slots__ = ("region", "category", "site_code", "voltage_raw", "designation")

    def __init__(self, region, category, site_code, voltage_raw, designation):
        self.region = region
        self.category = category
        self.site_code = site_code
        self.voltage_raw = voltage_raw
        self.designation = designation


def parse_location_path(path: str) -> Optional[ParsedPath]:
    """Parse a column-G path of the form

        <region>/<category>/<site>/<voltage>/<designation>/

    Returns a :class:`ParsedPath` for paths with at least five segments, else
    ``None``. Trailing/empty segments are ignored.
    """
    parts = [seg for seg in path.split("/") if seg != ""]
    if len(parts) < 5:
        return None
    region, category, site_code, voltage_raw, designation = parts[0], parts[1], parts[2], parts[3], parts[4]
    return ParsedPath(region, category, site_code, voltage_raw, designation)


# =============================================================================
# Voltage normalisation
# =============================================================================

def normalise_voltage(voltage_raw: str) -> Optional[VoltageKv]:
    """Normalise an IPS voltage segment (e.g. ``"110 kV"``, ``"11kV"``, ``"LV"``)
    to a numeric kV (``int`` when whole, ``float`` otherwise) or the literal
    string ``"LV"``. Returns ``None`` if the segment is empty/unparseable.
    """
    v = voltage_raw.replace("kV", "").replace("KV", "").replace("kv", "").strip()
    v = v.replace(" ", "")
    if v == "":
        return None
    if v.upper() == "LV":
        return "LV"
    try:
        f = float(v)
    except ValueError:
        return v
    return int(f) if f == int(f) else f


def _is_11kv(voltage_kv: Optional[VoltageKv]) -> bool:
    return voltage_kv == 11


def is_subtransmission_voltage(voltage_kv: Optional[VoltageKv]) -> bool:
    """True if the voltage is numeric and at or above the subtransmission floor."""
    return isinstance(voltage_kv, (int, float)) and voltage_kv >= SUBTRANSMISSION_MIN_KV


# =============================================================================
# Designation normalisation
# =============================================================================

def normalise_designation(designation: str) -> str:
    """Collapse a combined-zone designation to a single zone.

    A combined-zone designation joins two or more protection zones with ``+``
    (e.g. ``"BB11+BB12"``, ``"TR1 + TR2"``, ``"F506A+B"``). PowerFactory models
    these zones as distinct elements, so an IPS setting recorded against a
    combination must map to exactly one element. By convention this is the
    first zone - the text before the first ``+`` connector.

    Whitespace around the connector (the IPS data is inconsistent, e.g.
    ``"BB11 + BB12"`` and ``"BB11+ BB12"``) is trimmed. Designations without a
    ``+`` are returned trimmed but otherwise unchanged.

    Examples:
        >>> normalise_designation("BB11+BB12")
        'BB11'
        >>> normalise_designation("BB11 + BB12")
        'BB11'
        >>> normalise_designation("F506A+B")
        'F506A'
        >>> normalise_designation("CB1X12A+B")
        'CB1X12A'
        >>> normalise_designation("TR1")
        'TR1'
    """
    first = designation.split("+", 1)[0].strip()
    first = first if first else designation.strip()
    # IPS names some transformer bays "NX<n>"; PowerFactory models them as
    # "TR<n>". Bridge the synonym so the canonical key matches exactly.
    m = _RE_NX_TRANSFORMER.match(first)
    if m:
        return "TR" + m.group(1)
    return first


# =============================================================================
# Element-type classification
# =============================================================================
# Patterns follow "Network element operating designations.txt". Order matters:
# transformer switches (CBaT..) must be tested before feeder switches, and bus
# couplers (..cX..) before generic feeder switches.

_RE_BUSBAR = re.compile(r"^BB\d")
_RE_CAP_BANK = re.compile(r"^CP")
_RE_TFMR_SWITCH = re.compile(r"^(CB|AB|IS)[1378]T\d")
_RE_BUS_COUPLER = re.compile(r"^(CB|AB|IS)[1378]X")
_RE_LINE_SWITCH = re.compile(r"^X\d")
_RE_FEEDER_SWITCH = re.compile(r"^(CB|AB|IS)\d")
_RE_TRANSFORMER = re.compile(r"^T(R)?\d")          # TR1 / T1
_RE_FEEDER_NUM = re.compile(r"^F\d")               # 33/110/132 kV feeder F####
_RE_ALPHA_LED = re.compile(r"^[A-Za-z]")


def classify_designation(designation: str, voltage_kv: Optional[VoltageKv]) -> IpsElementType:
    """Classify an operating designation into an :class:`IpsElementType`.

    ``voltage_kv`` is used only to disambiguate alpha-led 11 kV feeders from
    other alpha-led names.
    """
    d = designation
    if _RE_BUSBAR.match(d):
        return IpsElementType.BUSBAR
    if _RE_CAP_BANK.match(d):
        return IpsElementType.CAPACITOR_BANK
    if _RE_TFMR_SWITCH.match(d):
        return IpsElementType.TRANSFORMER_SWITCH
    if _RE_BUS_COUPLER.match(d):
        return IpsElementType.BUS_COUPLER
    if _RE_LINE_SWITCH.match(d):
        return IpsElementType.LINE_SWITCH
    if _RE_FEEDER_SWITCH.match(d):
        return IpsElementType.FEEDER_SWITCH
    if _RE_TRANSFORMER.match(d):
        return IpsElementType.TRANSFORMER
    if _RE_FEEDER_NUM.match(d):
        return IpsElementType.FEEDER
    if _is_11kv(voltage_kv) and _RE_ALPHA_LED.match(d):
        return IpsElementType.FEEDER_11KV
    return IpsElementType.OTHER


# =============================================================================
# Scope filter for substation elements
# =============================================================================
# Per project scope, ignore protection devices associated with 11 kV feeders,
# 11 kV feeder switches and 11 kV line switches. Everything else (33/110/132 kV
# feeders & switches, bus couplers, cap banks, transformer bays, and 11 kV
# transformer-LV / cap-bank / bus-coupler devices) is in scope.

def scope_exclusion(element_type: IpsElementType,
                    voltage_kv: Optional[VoltageKv]) -> Optional[ExclusionReason]:
    """Return the :class:`ExclusionReason` for an out-of-scope *substation*
    element, else ``None``.
    """
    if element_type is IpsElementType.FEEDER_11KV:
        return ExclusionReason.OUT_OF_SCOPE_11KV_FEEDER
    if _is_11kv(voltage_kv):
        if element_type is IpsElementType.FEEDER:
            return ExclusionReason.OUT_OF_SCOPE_11KV_FEEDER
        if element_type is IpsElementType.FEEDER_SWITCH:
            return ExclusionReason.OUT_OF_SCOPE_11KV_FEEDER_SWITCH
        if element_type is IpsElementType.LINE_SWITCH:
            return ExclusionReason.OUT_OF_SCOPE_11KV_LINE_SWITCH
    return None


# =============================================================================
# Site-code resolution (substation mapping)
# =============================================================================

def resolve_site_code(raw_site_code: str, mapping: dict) -> Optional[str]:
    """Translate an IPS site code to the PowerFactory form.

    The substation mapping only contains substation codes, so Down Line Device
    X-site codes pass through unchanged.

    - If ``raw_site_code`` is a key in the mapping with a non-None value,
      return that alpha code.
    - If it maps to ``None``, return ``None`` (caller skips the row).
    - Otherwise return ``raw_site_code`` unchanged.
    """
    if raw_site_code in mapping:
        return mapping[raw_site_code]  # may be None -> skip
    return raw_site_code


# =============================================================================
# Top-level ingest
# =============================================================================

def ingest_rows(rows: Iterable[List[str]],
                mapping: Optional[dict] = None) -> IpsIngestResult:
    """Ingest pre-read CSV rows (each a list of column strings, header already
    removed). Exposed separately from :func:`ingest_ips_export` for testing.
    """
    if mapping is None:
        mapping = get_substation_mapping()

    result = IpsIngestResult()

    for row in rows:
        if len(row) <= _COL_PATH:
            continue
        setting_id = row[_COL_SETTING_ID]
        path = row[_COL_PATH]

        parsed = parse_location_path(path)
        if parsed is None:
            result.excluded.append(ExcludedRow(setting_id, path,
                                               ExclusionReason.MALFORMED_PATH))
            continue

        voltage_kv = normalise_voltage(parsed.voltage_raw)

        # ---- Category gate ------------------------------------------------
        if parsed.category == CATEGORY_DOWN_LINE_DEVICES:
            # Retain only subtransmission-voltage down line devices (X-sites).
            if not is_subtransmission_voltage(voltage_kv):
                result.excluded.append(ExcludedRow(
                    setting_id, path,
                    ExclusionReason.OUT_OF_SCOPE_DOWNLINE_BELOW_33KV,
                    raw_site_code=parsed.site_code,
                    designation=parsed.designation))
                continue
            # In scope: no substation-element exclusions apply to down line
            # devices, but the substation mapping still passes through harmlessly.
            element_reason = None
        elif parsed.category == CATEGORY_SUBSTATIONS:
            # Substation mapping: skip sites explicitly mapped to None.
            if parsed.site_code in mapping and mapping[parsed.site_code] is None:
                result.excluded.append(ExcludedRow(
                    setting_id, path,
                    ExclusionReason.SUBSTATION_SKIPPED,
                    raw_site_code=parsed.site_code,
                    designation=parsed.designation))
                continue
            element_reason = "substation"  # sentinel: apply element-type scope below
        else:
            result.excluded.append(ExcludedRow(
                setting_id, path,
                ExclusionReason.NOT_IN_SCOPE_CATEGORY,
                raw_site_code=parsed.site_code))
            continue

        # ---- Resolve site code + normalise & classify designation --------
        site_code = resolve_site_code(parsed.site_code, mapping)
        designation = normalise_designation(parsed.designation)
        element_type = classify_designation(designation, voltage_kv)

        # ---- Substation element-type scope (11 kV feeders etc.) ----------
        if element_reason == "substation":
            reason = scope_exclusion(element_type, voltage_kv)
            if reason is not None:
                result.excluded.append(ExcludedRow(
                    setting_id, path, reason,
                    raw_site_code=parsed.site_code,
                    designation=parsed.designation))
                continue

        key = MappingKey(site_code, voltage_kv, designation)
        device = IpsDevice(
            setting_id=setting_id,
            key=key,
            element_type=element_type,
            pattern_name=row[_COL_PATTERN],
            date_setting=row[_COL_DATE],
            device_id=row[_COL_DEVICE_ID],
            asset_name=row[_COL_ASSET],
            location_path=path,
            raw_site_code=parsed.site_code,
            raw_designation=parsed.designation,
            voltage_raw=parsed.voltage_raw,
            category=parsed.category,
        )
        result.devices.append(device)
        result.by_key.setdefault(key, []).append(device)

    return result


def ingest_ips_export(csv_path: str,
                      encoding: str = "utf-8",
                      mapping: Optional[dict] = None) -> IpsIngestResult:
    """Read and ingest the IPS export CSV into an :class:`IpsIngestResult`.

    Args:
        csv_path: Path to Report-Cache-ProtectionSettingIDs-EX.csv
        encoding: File encoding (default utf-8)
        mapping:  Substation mapping override (defaults to
                  ``config.region_config.get_substation_mapping``)
    """
    with open(csv_path, newline="", encoding=encoding) as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        return ingest_rows(reader, mapping=mapping)


# =============================================================================
# Database-backed ingest (production)
# =============================================================================

def _record_to_row(record: dict) -> List[str]:
    """Adapt a single IPS setting-ID dict (as returned by the query layer) to
    the positional row layout consumed by :func:`ingest_rows`.

    The query layer yields one dict per setting whose keys mirror the
    Report-Cache-ProtectionSettingIDs-EX report columns. Missing keys default
    to an empty string so a sparse row never raises; ``ingest_rows`` then
    applies the usual parse/scope rules. Values are coerced to ``str`` because
    the offline CSV path delivered every field as text and the downstream
    parsing (voltage/designation normalisation, path splitting) expects strings.
    """
    return [str(record.get(field, "") or "") for field in _DB_FIELDS_BY_COL]


def ingest_ips_records(records: Iterable[dict],
                       mapping: Optional[dict] = None) -> IpsIngestResult:
    """Ingest IPS setting-ID rows obtained from the database query layer.

    This is the production counterpart to :func:`ingest_ips_export`. Instead of
    reading the Report-Cache-ProtectionSettingIDs-EX CSV from disk, it consumes
    the dict rows returned by ``ips_data.query_database.get_setting_id_records``
    (the corporate-cache query). Each dict is adapted to the positional layout
    and handed to the shared :func:`ingest_rows`, so the parse, scope and
    mapping logic is identical to the offline path - only the source differs.

    Args:
        records: Iterable of setting-ID dicts (one per protection-device
                 setting), keyed by the report column names.
        mapping:  Substation mapping override (defaults to
                  ``config.region_config.get_substation_mapping``).
    """
    rows = (_record_to_row(record) for record in records)
    return ingest_rows(rows, mapping=mapping)