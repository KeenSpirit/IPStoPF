"""CT/VT pairing. Cases are real rows from the 2026-09-24 cached IT reports."""

from types import SimpleNamespace as Row

import pytest

from core import instrument_selection as isel
from core.protection_device import ProtectionDevice


def _ex(rows):
    return isel.choose_indexed(isel.group_energex_rows(rows)["ct"])


def test_cap_bank_neutral_ct_does_not_steal_the_secondary():
    # BRD CP11 J04 SPAJ160: 400/5 phase CT + 10/1 neutral-unbalance CT.
    # Old code: max primary 400, last secondary 1 -> 400/1.
    choice = _ex([("Ratio I_1", "80"), ("Isec_1", "5"), ("Iprim_1", "400"),
                  ("Isec_2", "1"), ("Ratio I_2", "10"), ("Iprim_2", "10")])
    assert (choice.primary, choice.secondary) == (400, 5)
    assert any("2 different transformers" in n for n in choice.notes)


def test_transformer_differential_pairs_stay_together():
    # BRD TR1 J47 SEL351: 2000/1 and 400/5. Old code could give 2000/5.
    choice = _ex([("Ratio I_1", "2000"), ("Isec_1", "1"), ("Iprim_1", "2000"),
                  ("Iprim_2", "400"), ("Ratio I_2", "80"), ("Isec_2", "5")])
    assert (choice.primary, choice.secondary) == (2000, 1)


def test_relay_programmed_ratio_selects_the_transformer():
    groups = isel.group_energex_rows([
        ("Iprim_1", "2000"), ("Isec_1", "1"), ("Iprim_2", "400"), ("Isec_2", "5")])
    choice = isel.choose_indexed(groups["ct"], preferred_primary=400)
    assert (choice.primary, choice.secondary) == (400, 5)


def test_unsuffixed_rows_fold_into_index_1():
    # BLN1B J02: 'Iprim' unsuffixed + 'Isec_1'
    choice = _ex([("Iprim", "400"), ("Isec_1", "5"), ("Ratio I_1", "80")])
    assert (choice.primary, choice.secondary) == (400, 5)


def test_swapped_entry_is_repaired():
    # STT3A+B J25B P123: Iprim_1 = 5, Isec = 400 in IPS.
    choice = _ex([("Ratio I_1", None), ("Iprim_1", "5"), ("Isec", "400")])
    assert (choice.primary, choice.secondary) == (400, 5)
    assert any("wrong way round" in n for n in choice.notes)


def test_missing_secondary_recovered_from_ratio():
    choice = _ex([("Iprim_1", "400"), ("Ratio I_1", "80")])
    assert (choice.primary, choice.secondary) == (400, 5)


def test_no_ct_rows():
    assert not _ex([("Vprim", "11000"), ("Vsec", "110")]).found


def test_vt_rows_grouped_separately():
    groups = isel.group_energex_rows([("Vprim", "11000"), ("Vsec", "110"),
                                      ("Iprim_1", "800"), ("Isec_1", "1")])
    vt = isel.choose_indexed(groups["vt"])
    assert (vt.primary, vt.secondary) == (11000, 110)


@pytest.mark.parametrize("prims, secs, preferred, expected", [
    (["400", "200"], ["1", "1"], None, (400, 1)),     # CALLSS-FB51-J01-J11
    (["400", "200"], ["1", "1"], 200, (200, 1)),      # relay programmed 200
    (["200", "200", "100"], ["5", "5", "5"], None, (200, 5)),
    (["5"], ["400"], None, (400, 5)),                  # swapped entry
])
def test_ergon_unindexed(prims, secs, preferred, expected):
    choice = isel.choose_unindexed(prims, secs, preferred)
    assert (choice.primary, choice.secondary) == expected


def test_ergon_ambiguous_secondaries_reported():
    choice = isel.choose_unindexed(["600", "100"], ["5", "1", "5"])
    assert (choice.primary, choice.secondary) == (600, 5)
    assert any("cannot be paired" in n for n in choice.notes)


def _device(setting_id="S1"):
    return ProtectionDevice(None, "P123", "BLN1B", setting_id, None, None, None)


def test_device_energex_pairs_and_ignores_other_settings():
    dev = _device()
    rows = [Row(relaysettingid="S1", nameenu="Iprim_1", actualvalue="600"),
            Row(relaysettingid="S1", nameenu="Isec_1", actualvalue="5"),
            Row(relaysettingid="S1", nameenu="Iprim_2", actualvalue="10"),
            Row(relaysettingid="S1", nameenu="Isec_2", actualvalue="1"),
            Row(relaysettingid="S2", nameenu="Isec_1", actualvalue="1")]
    dev.seq_instrument_attributes(rows)
    assert (dev.ct_primary, dev.ct_secondary) == (600, 5)


def test_device_energex_relay_setting_larger_is_kept():
    dev = _device()
    dev._setting_ct_primary, dev.ct_primary = 800, 800
    dev._setting_ct_secondary, dev.ct_secondary = 5, 5
    dev.seq_instrument_attributes([
        Row(relaysettingid="S1", nameenu="Iprim_1", actualvalue="400"),
        Row(relaysettingid="S1", nameenu="Isec_1", actualvalue="1")])
    assert (dev.ct_primary, dev.ct_secondary) == (800, 5)


def test_device_ergon_pairs():
    dev = _device()
    rows = [Row(relaysettingid="S1", setting="300", paramnameenu="CT Primary"),
            Row(relaysettingid="S1", setting="1", paramnameenu="CT Secondary"),
            Row(relaysettingid="S1", setting="600", paramnameenu="CT Primary"),
            Row(relaysettingid="S1", setting="1", paramnameenu="CT Secondary"),
            Row(relaysettingid="S1", setting="", paramnameenu="IT Primary")]
    dev.reg_instrument_attributes(rows)
    assert (dev.ct_primary, dev.ct_secondary) == (600, 1)


def test_device_no_rows_leaves_defaults():
    dev = _device()
    dev.seq_instrument_attributes([])
    dev.reg_instrument_attributes([])
    assert (dev.ct_primary, dev.ct_secondary, dev.vt_primary, dev.vt_secondary) == (1, 1, 1, 1)


def test_unsuffixed_rows_that_disagree_are_a_separate_transformer():
    # 0039547E...: Iprim 2000 / Isec 5 / Ratio I 400 is NOT the _1 CT (800/5).
    choice = _ex([("Ratio I", "400"), ("Iprim", "2000"), ("Isec", "5"),
                  ("Ratio I_1", "160"), ("Isec_1", "5"), ("Iprim_2", "400"),
                  ("Isec_2", "5"), ("Iprim_1", "800"), ("Ratio I_2", "80")])
    assert (choice.primary, choice.secondary) == (2000, 5)


@pytest.mark.parametrize("value, expected", [(5.0, 5), (1.0, 1), (2.89, 3),
                                             (2.8900001, 3), (1.44, 1),
                                             (0.577, 1), (0.333, 1)])
def test_secondary_as_int(value, expected):
    assert isel.secondary_as_int(value) == expected


def test_device_fractional_secondary_rounded_not_truncated():
    dev = _device()
    dev.seq_instrument_attributes([
        Row(relaysettingid="S1", nameenu="Iprim_3", actualvalue="2500.0"),
        Row(relaysettingid="S1", nameenu="Isec_3", actualvalue="2.89")])
    assert (dev.ct_primary, dev.ct_secondary) == (2500, 3)
