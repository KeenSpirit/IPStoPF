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

from domain.mapping_key import MappingKey
from mapping.pf_source import PfElementRef, PfSourceResult

import re
from typing import Dict, List, Optional, Tuple

from process_ips.ips_records import IpsDevice, IpsElementType
from process_pf_elements.pf_normalise import VOLTAGE_DIGIT, CAT_CAP_BANK

TIER_CAP_BANK      = "cap_bank_voltage_digit"   # Rule 1, forms 1 & 2
TIER_CAP_BANK_SOLE = "cap_bank_sole_device"     # Rule 2, last resort

_RE_PLAIN_CP = re.compile(r"^CP(\d+)$")

# Match tiers
TIER_EXACT = "exact"
TIER_LV_WINDING = "lv_to_lowest_winding"
TIER_COUPLER_BASE = "coupler_base"

CATEGORY_SUBSTATIONS = "Substations"

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
    ignored: Dict[MappingKey, List[IpsDevice]] = field(default_factory=dict)

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

    @property
    def ignored_keys(self) -> int:
        return len(self.ignored)

    @property
    def ignored_setting_ids(self) -> int:
        return sum(len(v) for v in self.ignored.values())

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
            f"  ignored (substation not in PF) : {self.ignored_keys} keys, "
            f"{self.ignored_setting_ids} setting IDs",
            f"  match tiers              : {self.tier_counts()}",
        ]
        return "\n".join(lines)


# =============================================================================
# Join
# =============================================================================

def _is_numeric_voltage(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def reconcile(ips_by_key, pf, use_fallbacks=True,
              ignore_substations_absent_from_pf=True):
    result = ReconciliationResult()
    pf_by_key = pf.by_key()
    pf_sites = {r.key.site_code for r in pf.refs}

    # Secondary index for the LV fallback: (site, designation) -> refs (numeric V)
    pf_by_site_desc: Dict[tuple, List[PfElementRef]] = {}
    for r in pf.refs:
        if _is_numeric_voltage(r.key.voltage_kv):
            pf_by_site_desc.setdefault((r.key.site_code, r.key.designation), []).append(r)

    claimed: set = set()  # MappingKeys of PF refs already matched

    def claim(ref: PfElementRef) -> None:
        claimed.add(ref.key)

    for key, devices in ips_by_key.items():
        # ---- Substation-site filter --------------------------------------
        # Only consider substation devices whose site exists in PowerFactory;
        # those at sites PF doesn't model are ignored entirely.
        if (ignore_substations_absent_from_pf
                and devices
                and devices[0].category == CATEGORY_SUBSTATIONS
                and key.site_code not in pf_sites):
            result.ignored[key] = devices
            continue

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

            cap_ref = _cap_bank_match(key, pf_by_key)
            if cap_ref is not None:
                result.matched.append(MatchedElement(key, cap_ref, devices, TIER_CAP_BANK))
                claim(cap_ref); continue

            # ---- Tier 2b: LV -> lowest numeric winding --------------------
            if not _is_numeric_voltage(key.voltage_kv):
                lv_ref = _lowest_winding_match(key, pf_by_site_desc)
                if lv_ref is not None:
                    result.matched.append(MatchedElement(key, lv_ref, devices, TIER_LV_WINDING))
                    claim(lv_ref)
                    continue

        # ---- No match -----------------------------------------------------
        result.ips_only[key] = devices

    if use_fallbacks:
        _sole_cap_bank_sweep(result, pf_by_key, claimed)

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


def _cap_bank_match(key, pf_by_key):
    """Bare IPS 'CPn' -> PF voltage-digit form 'CP{vd}{n}' (Rule 1)."""
    vd = VOLTAGE_DIGIT.get(key.voltage_kv) if isinstance(key.voltage_kv, int) else None
    if vd is None:
        return None
    m = _RE_PLAIN_CP.match(key.designation)
    if not m:
        return None
    digits = m.group(1)
    if digits[0] == vd:          # already carries the voltage digit -> exact tier owns it
        return None
    refs = pf_by_key.get(MappingKey(key.site_code, key.voltage_kv, f"CP{vd}{digits}"))
    return refs[0] if refs else None


def _sole_cap_bank_sweep(result, pf_by_key, claimed):
    """Rule 2 (last resort): where exactly one unmatched IPS cap bank and exactly
    one unmatched PF cap bank remain at the same (site, voltage), pair them
    regardless of name. CP1/CP2 can name the same bank, so with one candidate
    left on each side the pairing is unambiguous. Deliberate risk: a site with a
    genuinely IPS-only and a genuinely PF-only bank at one voltage will be paired."""
    ips_caps: Dict[Tuple, List[MappingKey]] = {}
    for key, devices in result.ips_only.items():
        if devices and devices[0].element_type is IpsElementType.CAPACITOR_BANK:
            ips_caps.setdefault((key.site_code, key.voltage_kv), []).append(key)

    pf_caps: Dict[Tuple, List[MappingKey]] = {}
    for key, refs in pf_by_key.items():
        if key in claimed:
            continue
        if refs and refs[0].category == CAT_CAP_BANK:
            pf_caps.setdefault((key.site_code, key.voltage_kv), []).append(key)

    for sv, ips_keys in ips_caps.items():
        pf_keys = pf_caps.get(sv)
        if len(ips_keys) == 1 and pf_keys and len(pf_keys) == 1:
            ips_key, pf_key = ips_keys[0], pf_keys[0]
            devices = result.ips_only.pop(ips_key)
            ref = pf_by_key[pf_key][0]
            result.matched.append(MatchedElement(ips_key, ref, devices, TIER_CAP_BANK_SOLE))
            claimed.add(ref.key)