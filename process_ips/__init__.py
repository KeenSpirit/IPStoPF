"""
IPS ingest package.

Turns the IPS protection-setting export (Report-Cache-ProtectionSettingIDs-EX.csv)
into normalised, scope-filtered records keyed by a canonical ``MappingKey`` for
later joining against PowerFactory elements.

Quick start:
    from process_ips import ingest_ips_export, MappingKey

    result = ingest_ips_export("ReportCacheProtectionSettingIDsEX.csv")
    devices = result.by_key[MappingKey("ABM", 33, "BB31")]
"""

from domain.mapping_key import VoltageKv, MappingKey

from process_ips.ips_records import (
    IpsDevice,
    IpsElementType,
    ExcludedRow,
    ExclusionReason,
    IpsIngestResult,
)
from process_ips.ips_ingest import (
    ingest_ips_export,
    ingest_rows,
    parse_location_path,
    normalise_voltage,
    normalise_designation,
    classify_designation,
    scope_exclusion,
    resolve_site_code,
)

__all__ = [
    "MappingKey", "IpsDevice", "IpsElementType", "ExcludedRow",
    "ExclusionReason", "IpsIngestResult", "VoltageKv",
    "ingest_ips_export", "ingest_rows", "parse_location_path",
    "normalise_voltage", "normalise_designation", "classify_designation", "scope_exclusion",
    "resolve_site_code",
]