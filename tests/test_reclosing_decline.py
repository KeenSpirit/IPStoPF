"""A reclose table that disables every block must never be written."""

from update_powerfactory import relay_reclosing as rr
from tests.fakes import FakeDevice, FakePF


def _relay_with_recl(oplockout=3):
    recl = FakePF("Recloser", "RelRecl", attrs={
        "oplockout": oplockout,
        "ilogic": [[1.0, 1.0, 2.0]],
        "r:typ_id:e:blockid": ["OC1+"],
        "reclnotactive": 0,
    })
    relay = FakePF("X1512865_RC01ES", "ElmRelay", contents=[recl])
    return relay, recl


def _logic_row(trip):
    # folder, element, blockid, blockpath, param, -, adjust, trip, on/off, recl
    return ["X1512865_RC01ES", "Recloser_logic", "OC1+", "AR map",
            f"Trip {trip}", "x", "None", str(trip), "on", "R"]


def test_no_logic_rows_is_silent_and_writes_nothing():
    relay, recl = _relay_with_recl()
    dev = FakeDevice("RC01ES_Energex", relay.loc_name, pf_obj=relay)
    status = rr.update_reclosing_logic(None, dev, [["F", "OC1+", "Ipset", "x"]], {})
    assert status is None
    assert recl.writes == []


def test_unresolved_noja_table_is_declined():
    relay, recl = _relay_with_recl()
    dev = FakeDevice("RC01ES_Energex", relay.loc_name, pf_obj=relay, settings=[])
    status = rr.update_reclosing_logic(None, dev, [_logic_row(2)], {})
    assert status == rr.RECLOSE_UNRESOLVED
    assert recl.writes == []                      # oplockout and ilogic untouched
    assert recl.attrs["oplockout"] == 3


def test_resolvable_noja_table_is_written():
    relay, recl = _relay_with_recl()
    dev = FakeDevice("RC01ES_Energex", relay.loc_name, pf_obj=relay, settings=[])
    status = rr.update_reclosing_logic(None, dev, [_logic_row(1)], {})
    assert status is None
    assert ("oplockout", 1) in recl.writes
    assert recl.attrs["ilogic"] == [[2.0]]


def test_logic_rows_without_relrecl_reported():
    relay = FakePF("R", "ElmRelay")
    dev = FakeDevice("RC01ES_Energex", "R", pf_obj=relay)
    assert rr.update_reclosing_logic(None, dev, [_logic_row(1)], {}) == rr.RECLOSE_UNRESOLVED
