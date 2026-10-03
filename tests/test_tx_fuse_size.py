"""Distribution transformer fuse sizes from the STNW1001 table."""

import pytest

from utils import pf_utils as u


@pytest.mark.parametrize("args, expected", [
    # 3-phase 11 kV 315 kVA, 22 kV 500 kVA (unchanged by the fix)
    ((3, 11.0, 0.433, 0.315, False, False), "31K"),
    ((3, 22.0, 0.433, 0.5, False, False), "31K"),
    # SWER isolators: 22/12.7 kV and 11/19.1 kV - previously never matched
    ((2, 22.0, 12.7, 0.1, True, False), "20K"),
    ((2, 12.7, 22.0, 0.1, True, False), "20K"),       # reversed winding order
    ((2, 11.0, 19.1, 0.2, True, False), "50K"),
    # single-phase on a SWER bus: 12.7 kV 25 kVA, 11 kV 25 kVA
    ((2, 12.7, 0.25, 0.025, False, True), "3/10K"),
    ((2, 12.7, 0.25, 0.05, False, True), "10K"),
    ((2, 11.0, 0.25, 0.05, False, True), "10K"),
    ((2, 19.1, 0.25, 0.063, False, True), "6/20K"),
    # two-phase 11 kV 25 kVA
    ((2, 11.0, 0.25, 0.025, False, False), "6/20K"),
])
def test_table_hits(args, expected):
    size, keys, defaulted = u.tx_fuse_size(*args)
    assert (size, defaulted) == (expected, False), keys


def test_missing_entry_defaults_and_says_so():
    size, keys, defaulted = u.tx_fuse_size(3, 11.0, 0.433, 0.4, False, False)
    assert (size, defaulted, keys) == ("3/10K", True, ["311400"])


@pytest.mark.parametrize("kv, text", [(11.0, "11"), (12.7, "12.7"), (19.1, "19.1"),
                                      (22, "22"), (12.700000001, "12.7")])
def test_kv(kv, text):
    assert u._kv(kv) == text


def test_create_fuse_dict_still_returns_a_copy():
    table = u.create_fuse_dict()
    table["x"] = "y"
    assert "x" not in u._FUSE_SIZE_TABLE
