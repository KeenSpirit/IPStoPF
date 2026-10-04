"""Diagnostic logging for elements switched off by mapping outserv rows."""

import logging

from update_powerfactory import relay_settings as rs
from tests.fakes import FakeDevice, FakePF


def _relay():
    oc = FakePF("OC1+", "RelToc", attrs={"outserv": 0})
    ef = FakePF("EF1+", "RelToc", attrs={"outserv": 0})
    return FakePF("X1512865_RC01ES", "ElmRelay", contents=[oc, ef]), oc, ef


def test_missing_ips_value_switching_element_off_is_logged(caplog):
    relay, oc, ef = _relay()
    dev = FakeDevice("RC01ES_Energex", relay.loc_name, pf_obj=relay)
    mapping = [["X1512865_RC01ES", "OC1+", "outserv", "blk", "par", "x", "None", "OFF"],
               ["X1512865_RC01ES", "EF1+", "outserv", "blk", "par2", "x", "None", "OFF"]]
    with caplog.at_level(logging.INFO):
        rs.apply_settings(None, dev, mapping, {}, False)
    assert oc.attrs["outserv"] == 1 and ef.attrs["outserv"] == 1
    text = caplog.text
    assert "switched OFF 2 element(s)" in text and "IPS value MISSING" in text
    assert "all 2 overcurrent element(s) are out of service" in text


def test_nothing_logged_when_elements_stay_in_service(caplog):
    relay, oc, ef = _relay()
    dev = FakeDevice("RC01ES_Energex", relay.loc_name, pf_obj=relay)
    mapping = [["X1512865_RC01ES", "OC1+", "outserv", "blk", "par", "x", "None", "ON"]]
    with caplog.at_level(logging.INFO):
        rs.apply_settings(None, dev, mapping, {}, False)
    assert oc.attrs["outserv"] == 0
    assert "switched OFF" not in caplog.text and "out of service after" not in caplog.text
