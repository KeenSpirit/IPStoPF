"""
Mapping package: bring the IPS and PowerFactory sides together.

    from process_ips import ingest_ips_export
    from mapping import pf_refs_from_workbook, reconcile

    ips = ingest_ips_export("ReportCacheProtectionSettingIDsEX.csv")
    pf = pf_refs_from_workbook("PowerFactory element data.xlsx")
    result = reconcile(ips.by_key, pf)
    print(result.coverage_summary())
"""
from mapping.pf_source import (
    PfElementRef,
    PfSourceResult,
    pf_refs_from_sites,
    pf_refs_from_workbook,
)
from mapping.reconciliation import (
    MatchedElement,
    ReconciliationResult,
    reconcile,
    TIER_EXACT,
    TIER_LV_WINDING,
    TIER_COUPLER_BASE,
    TIER_CAP_BANK,
    TIER_CAP_BANK_SOLE,
)

__all__ = [
    "PfElementRef", "PfSourceResult", "pf_refs_from_sites", "pf_refs_from_workbook",
    "MatchedElement", "ReconciliationResult", "reconcile",
    "TIER_EXACT", "TIER_LV_WINDING", "TIER_COUPLER_BASE",
]