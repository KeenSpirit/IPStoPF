"""
Join + reconciliation between IPS protection devices and PowerFactory elements.

Consumes:
    - the IPS ingest result (``process_ips.ingest_ips_export``)
    - the PowerFactory source result (``mapping.pf_source``)

and produces a :class:`ReconciliationResult` describing, for the in-scope
universe:

    - matched    : elements present on both sides (IPS setting IDs -> PF cubicle)
    - ips_only   : IPS devices with no matching PowerFactory element
    - pf_only    : PowerFactory elements with no IPS device

Matching is tiered. Tier 1 is an exact ``MappingKey`` match. Optional fallbacks
handle the two format gaps that are intrinsic to the data rather than to
parsing:

    - ``lv_to_lowest_winding`` : IPS records the voltage of some transformer
      bays as the literal ``"LV"`` rather than a number. Such a key is matched
      to the same-named transformer at the site's lowest numeric voltage.
    - ``coupler_base`` : a cable-box coupler whose IPS designation carries a
      box suffix (``CB1X12A``) is matched to the base coupler (``CB1X12``)
      modelled in PowerFactory, via ``config.region_config.coupler_base_name``.

Fallbacks never override an exact match and never reuse a PF element already
claimed by an exact match; every match records the tier that produced it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from process_ips.ips_records import IpsDevice, MappingKey
from mapping.pf_source import PfElementRef, PfSourceResult


# Match tiers
TIER_EXACT = "exact"
TIER_LV_WINDING = "lv_to_lowest_winding"
TIER_COUPLER_BASE = "coupler_base"


@dataclass
class MatchedElement:
    key: MappingKey                  # the IPS key
    pf: PfElementRef                 # the matched PowerFactory element
    ips_devices: List[IpsDevice]     # all IPS settings at this element
    tier: str

    @property
    def setting_ids(self) -> List[str]:
        return [d.setting_id for d in self.ips_devices]


@dataclass
class ReconciliationResult:
    matched: List[MatchedElement] = field(default_factory=list)
    ips_only: Dict[MappingKey, List[IpsDevice]] = field(default_factory=dict)
    pf_only: List[PfElementRef] = field(default_factory=list)

    # ----------------------------------------------------------- statistics
    @property
    def matched_keys(self) -> int:
        return len(self.matched)

    @property
    def matched_setting_ids(self) -> int:
        return sum(len(m.ips_devices) for m in self.matched)

    @property
    def ips_only_keys(self) -> int:
        return len(self.ips_only)

    @property
    def ips_only_setting_ids(self) -> int:
        return sum(len(v) for v in self.ips_only.values())

    @property
    def pf_only_count(self) -> int:
        return len(self.pf_only)

    def tier_counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for m in self.matched:
            out[m.tier] = out.get(m.tier, 0) + 1
        return out

    def ips_only_by_element_type(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for devs in self.ips_only.values():
            t = devs[0].element_type.value
            out[t] = out.get(t, 0) + 1
        return out

    def pf_only_by_category(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self.pf_only:
            out[r.category] = out.get(r.category, 0) + 1
        return out

    def coverage_summary(self) -> str:
        total_keys = self.matched_keys + self.ips_only_keys
        total_sids = self.matched_setting_ids + self.ips_only_setting_ids
        kpc = (100 * self.matched_keys / total_keys) if total_keys else 0.0
        spc = (100 * self.matched_setting_ids / total_sids) if total_sids else 0.0
        lines = [
            "IPS -> PowerFactory reconciliation",
            f"  IPS elements (keys)      : {total_keys}",
            f"    matched                : {self.matched_keys}  ({kpc:.1f}%)",
            f"    ips-only (no PF element): {self.ips_only_keys}",
            f"  IPS setting IDs          : {total_sids}",
            f"    mapped to a PF cubicle : {self.matched_setting_ids}  ({spc:.1f}%)",
            f"    unmapped               : {self.ips_only_setting_ids}",
            f"  PF elements with no IPS device : {self.pf_only_count}",
            f"  match tiers              : {self.tier_counts()}",
        ]
        return "\n".join(lines)


# =============================================================================
# Join
# =============================================================================

def _is_numeric_voltage(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def reconcile(ips_by_key: Dict[MappingKey, List[IpsDevice]],
              pf: PfSourceResult,
              use_fallbacks: bool = True) -> ReconciliationResult:
    """Join IPS devices (keyed) against PowerFactory element references.

    Args:
        ips_by_key: ``IpsIngestResult.by_key``
        pf:         result from ``mapping.pf_source``
        use_fallbacks: enable the LV-winding and coupler-base fallbacks
    """
    result = ReconciliationResult()

    pf_by_key: Dict[MappingKey, List[PfElementRef]] = pf.by_key()

    # Secondary index for the LV fallback: (site, designation) -> refs (numeric V)
    pf_by_site_desc: Dict[tuple, List[PfElementRef]] = {}
    for r in pf.refs:
        if _is_numeric_voltage(r.key.voltage_kv):
            pf_by_site_desc.setdefault((r.key.site_code, r.key.designation), []).append(r)

    claimed: set = set()  # MappingKeys of PF refs already matched

    def claim(ref: PfElementRef) -> None:
        claimed.add(ref.key)

    for key, devices in ips_by_key.items():
        # ---- Tier 1: exact ------------------------------------------------
        if key in pf_by_key:
            ref = pf_by_key[key][0]
            result.matched.append(MatchedElement(key, ref, devices, TIER_EXACT))
            claim(ref)
            continue

        if use_fallbacks:
            # ---- Tier 2a: coupler cable-box base --------------------------
            base_ref = _coupler_base_match(key, pf_by_key)
            if base_ref is not None:
                result.matched.append(MatchedElement(key, base_ref, devices, TIER_COUPLER_BASE))
                claim(base_ref)
                continue

            # ---- Tier 2b: LV -> lowest numeric winding --------------------
            if not _is_numeric_voltage(key.voltage_kv):
                lv_ref = _lowest_winding_match(key, pf_by_site_desc)
                if lv_ref is not None:
                    result.matched.append(MatchedElement(key, lv_ref, devices, TIER_LV_WINDING))
                    claim(lv_ref)
                    continue

        # ---- No match -----------------------------------------------------
        result.ips_only[key] = devices

    # ---- PF elements never matched ---------------------------------------
    for key, refs in pf_by_key.items():
        if key not in claimed:
            result.pf_only.extend(refs)

    return result


def _coupler_base_match(key: MappingKey,
                        pf_by_key: Dict[MappingKey, List[PfElementRef]]
                        ) -> Optional[PfElementRef]:
    """Match a cable-box coupler designation to the PF base coupler."""
    from config.region_config import coupler_base_name
    base = coupler_base_name(key.designation)
    if not base:
        return None
    base_key = MappingKey(key.site_code, key.voltage_kv, base)
    refs = pf_by_key.get(base_key)
    return refs[0] if refs else None


def _lowest_winding_match(key: MappingKey,
                          pf_by_site_desc: Dict[tuple, List[PfElementRef]]
                          ) -> Optional[PfElementRef]:
    """Match an ``"LV"``-voltage transformer key to the same-named transformer
    at the site's lowest numeric voltage."""
    refs = pf_by_site_desc.get((key.site_code, key.designation))
    if not refs:
        return None
    return min(refs, key=lambda r: r.key.voltage_kv)