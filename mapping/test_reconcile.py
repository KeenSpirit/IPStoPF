"""
Tests for the PowerFactory normalisation and the join + reconciliation stage.
Offline; run with:  pytest mapping/test_reconcile.py
"""

from domain.mapping_key import MappingKey
from process_ips.ips_records import IpsDevice, IpsElementType
from process_pf_elements import pf_normalise as pn
from mapping.pf_source import PfElementRef, PfSourceResult
from mapping.reconciliation import (
    TIER_COUPLER_BASE,
    TIER_EXACT,
    TIER_LV_WINDING,
    reconcile,
)


# --------------------------------------------------------------------------- #
# PF normalisation
# --------------------------------------------------------------------------- #

def test_voltage_normalised_to_int():
    assert pn.normalise_voltage_kv(110.0) == 110
    assert pn.normalise_voltage_kv(5.5) == 5.5
    assert pn.normalise_voltage_kv("LV") == "LV"


def test_cap_bank_inserts_voltage_digit():
    assert pn.normalise_cap_bank("CP2", 11) == "CP12"
    assert pn.normalise_cap_bank("CP1", 33) == "CP31"
    assert pn.normalise_cap_bank("CP2", 110) == "CP72"


def test_cap_bank_leaves_correct_form():
    assert pn.normalise_cap_bank("CP32", 33) == "CP32"
    assert pn.normalise_cap_bank("CP12", 11) == "CP12"


def test_cap_bank_strips_dup_and_keeps_variant_prefix():
    assert pn.normalise_cap_bank("CP2(1)", 11) == "CP12"
    assert pn.normalise_cap_bank("CPK12", 11) == "CPK12"   # distinct device, untouched


def test_busbar_strips_duplicate_suffix():
    assert pn.normalise_busbar("BB31_1") == "BB31"
    assert pn.normalise_busbar("BB71") == "BB71"


def test_feeder_voltage_from_name():
    assert pn.feeder_voltage_from_name("F3379") == 33
    assert pn.feeder_voltage_from_name("F7493") == 110
    assert pn.feeder_voltage_from_name("F818") == 132
    assert pn.feeder_voltage_from_name("F471") == 33


def test_make_pf_key_full():
    k = pn.make_pf_key("ABM", pn.CAT_CAP_BANK, "CP2", 110.0)
    assert k == MappingKey("ABM", 110, "CP72")


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #

def _dev(setting_id, key, etype=IpsElementType.BUSBAR, category="Substations"):
    return IpsDevice(
        setting_id=setting_id, key=key, element_type=etype,
        pattern_name="P", date_setting="2020-01-01", device_id="", asset_name="A",
        location_path="x", raw_site_code=key.site_code, raw_designation=key.designation,
        voltage_raw=str(key.voltage_kv), category=category,
    )


def _ref(key, category=pn.CAT_BUSBAR):
    return PfElementRef(key=key, category=category, raw_name=key.designation,
                        voltage_raw=key.voltage_kv, source="grid", cubicle=object())


def test_exact_match():
    k = MappingKey("ABM", 33, "BB31")
    ips = {k: [_dev("S1", k)]}
    pf = PfSourceResult(refs=[_ref(k)])
    rec = reconcile(ips, pf)
    assert rec.matched_keys == 1
    assert rec.matched[0].tier == TIER_EXACT
    assert rec.matched[0].setting_ids == ["S1"]
    assert rec.pf_only_count == 0
    assert rec.ips_only_keys == 0


def test_ips_only_and_pf_only():
    ik = MappingKey("AAA", 33, "F3001")
    pk = MappingKey("AAA", 33, "F3002")
    ips = {ik: [_dev("S1", ik, IpsElementType.FEEDER)]}
    pf = PfSourceResult(refs=[_ref(pk, pn.CAT_FEEDER)])
    rec = reconcile(ips, pf)
    assert rec.ips_only_keys == 1
    assert rec.pf_only_count == 1
    assert rec.matched_keys == 0


def test_coupler_base_fallback():
    # IPS carries the first cable box (CB1X12A); PF models the base coupler.
    ips_key = MappingKey("AST", 11, "CB1X12A")
    pf_key = MappingKey("AST", 11, "CB1X12")
    ips = {ips_key: [_dev("S1", ips_key, IpsElementType.BUS_COUPLER)]}
    pf = PfSourceResult(refs=[_ref(pf_key, pn.CAT_SWITCH)])
    rec = reconcile(ips, pf)
    assert rec.matched_keys == 1
    assert rec.matched[0].tier == TIER_COUPLER_BASE


def test_lv_to_lowest_winding_fallback():
    # IPS records the transformer winding voltage as the literal "LV".
    ips_key = MappingKey("XYZ", "LV", "TR1")
    hv = MappingKey("XYZ", 33, "TR1")
    lv = MappingKey("XYZ", 11, "TR1")
    ips = {ips_key: [_dev("S1", ips_key, IpsElementType.TRANSFORMER)]}
    pf = PfSourceResult(refs=[_ref(hv, pn.CAT_TRANSFORMER), _ref(lv, pn.CAT_TRANSFORMER)])
    rec = reconcile(ips, pf)
    assert rec.matched_keys == 1
    assert rec.matched[0].tier == TIER_LV_WINDING
    assert rec.matched[0].pf.key.voltage_kv == 11   # lowest winding


def test_fallbacks_can_be_disabled():
    ips_key = MappingKey("AST", 11, "CB1X12A")
    pf_key = MappingKey("AST", 11, "CB1X12")
    ips = {ips_key: [_dev("S1", ips_key, IpsElementType.BUS_COUPLER)]}
    pf = PfSourceResult(refs=[_ref(pf_key, pn.CAT_SWITCH)])
    rec = reconcile(ips, pf, use_fallbacks=False)
    assert rec.matched_keys == 0
    assert rec.ips_only_keys == 1

# --------------------------------------------------------------------------- #
# Substation-site filter
# --------------------------------------------------------------------------- #

def test_substation_at_non_pf_site_is_ignored():
    # IPS substation device at a site PowerFactory does not model -> ignored.
    ik = MappingKey("ZZZ", 33, "F3001")
    pk = MappingKey("AAA", 33, "F3002")
    ips = {ik: [_dev("S1", ik, IpsElementType.FEEDER)]}
    pf = PfSourceResult(refs=[_ref(pk, pn.CAT_FEEDER)])
    rec = reconcile(ips, pf)
    assert rec.ignored_keys == 1
    assert rec.ignored_setting_ids == 1
    assert rec.ips_only_keys == 0
    assert rec.matched_keys == 0


def test_down_line_device_at_non_pf_site_still_ips_only():
    # Down Line Device handling is unchanged: still surfaced as ips-only.
    ik = MappingKey("X12797-B", 33, "X12797-B")
    pk = MappingKey("AAA", 33, "F3002")
    ips = {ik: [_dev("S1", ik, IpsElementType.LINE_SWITCH, category="Down Line Devices")]}
    pf = PfSourceResult(refs=[_ref(pk, pn.CAT_FEEDER)])
    rec = reconcile(ips, pf)
    assert rec.ips_only_keys == 1
    assert rec.ignored_keys == 0


def test_substation_at_pf_site_with_no_element_is_ips_only():
    # Site exists in PF but this particular element does not -> genuine divergence.
    ik = MappingKey("ABM", 33, "F3001")
    pk = MappingKey("ABM", 33, "F3002")
    ips = {ik: [_dev("S1", ik, IpsElementType.FEEDER)]}
    pf = PfSourceResult(refs=[_ref(pk, pn.CAT_FEEDER)])
    rec = reconcile(ips, pf)
    assert rec.ips_only_keys == 1
    assert rec.ignored_keys == 0


def test_substation_filter_can_be_disabled():
    ik = MappingKey("ZZZ", 33, "F3001")
    pk = MappingKey("AAA", 33, "F3002")
    ips = {ik: [_dev("S1", ik, IpsElementType.FEEDER)]}
    pf = PfSourceResult(refs=[_ref(pk, pn.CAT_FEEDER)])
    rec = reconcile(ips, pf, ignore_substations_absent_from_pf=False)
    assert rec.ips_only_keys == 1
    assert rec.ignored_keys == 0