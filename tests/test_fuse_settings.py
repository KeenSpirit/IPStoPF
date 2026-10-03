"""Fuse parameter extraction and solid links."""

import pytest

from update_powerfactory import fuse_settings as fs
from tests.fakes import FakeDevice, FakePF


@pytest.mark.parametrize("rows, expected", [
    ([["b", "Curve", "K", ""], ["b", "MAX", "63", ""]], ("K", " 63A")),
    ([["b", "Curve", "K", ""], ["b", "In", "63.0", ""]], ("K", " 63A")),
    ([["b", "Curve", "T", ""], ["b", "MAX", "100A", ""]], ("T", " 100A")),
    ([["b", "Curve", "SOLID LINK", ""], ["b", "MAX", "SOLID LINK", ""]], ("SOLID LINK", "")),
])
def test_extract(rows, expected):
    curve, rating, failed = fs._extract_fuse_parameters(rows)
    assert (curve, rating) == expected and not failed


@pytest.mark.parametrize("curve, rows, expected", [
    ("SOLID LINK", [], True),
    ("SOLID LINK", [["b", "MAX", "125", ""]], True),     # DO-1611923
    ("", [["b", "MAX", "SOLID LINK", ""]], True),
    ("K", [["b", "MAX", "63", ""]], False),
])
def test_is_solid_link(curve, rows, expected):
    assert fs.is_solid_link(curve, rows) is expected


def test_solid_link_fuse_taken_out_of_service():
    fuse = FakePF("DO-1718168", "RelFuse",
                  attrs={"outserv": 0, "chr_name": "", "r:cpGrid:e:loc_name": "GLSO"})
    dev = FakeDevice("Ergon_Fuse", "DO-1718168", pf_obj=fuse,
                     settings=[["b", "Curve", "SOLID LINK", ""],
                               ["b", "MAX", "SOLID LINK", ""]],
                     fuse_type="Line Fuse", fuse_size=None)
    result = fs.fuse_setting(None, dev, fs.FuseTypeIndex())
    assert result.result == fs.SOLID_LINK_RESULT
    assert fuse.attrs["outserv"] == 1


def test_malformed_ips_rows_not_reported_as_not_in_ips():
    fuse = FakePF("DO-1", "RelFuse", attrs={"r:cpGrid:e:loc_name": "X"})
    dev = FakeDevice("Ergon_Fuse", "DO-1", pf_obj=fuse, settings=[["b", "Curve"]],
                     fuse_type="Line Fuse", fuse_size=None)
    assert fs.fuse_setting(None, dev, fs.FuseTypeIndex()).result == "IPS fuse setting incomplete"
