"""update_vt must not raise on relay types without VT slots."""

from core.update_result import UpdateResult
from update_powerfactory import vt_settings as vs
from tests.fakes import FakeDevice, FakePF


def _relay(filters):
    blocks = [None if f is None else FakePF(f"s{i}", "IntSlot", attrs={"filtmod": f})
              for i, f in enumerate(filters)]
    rtype = FakePF("T", "TypRelay", attrs={"pblk": blocks})
    relay = FakePF("R", "ElmRelay", attrs={"pdiselm": [None] * len(filters),
                                           "typ_id": rtype,
                                           "r:typ_id:e:pblk": blocks})
    relay.typ_id = rtype
    cub = FakePF("cub", "StaCubic", contents=[relay])
    return relay


def test_no_vt_branch_tolerates_empty_slot_positions():
    relay = _relay([None, "StaCt*", "StaVt*"])
    dev = FakeDevice("P", "R", pf_obj=relay, vt_secondary=1, vt_primary=1,
                     vt_op_id="")
    assert vs.update_vt(None, dev, UpdateResult(), vt_library=object()).vt_result == "No VT Linked"


def test_type_without_vt_slot_reported_not_raised():
    relay = _relay(["StaCt*"])
    dev = FakeDevice("P", "R", pf_obj=relay, vt_secondary=110, vt_primary=11000,
                     vt_op_id="")
    res = vs.update_vt(None, dev, UpdateResult(), vt_library=FakePF("lib", "IntFolder"))
    assert res.vt_result == "No VT slot on relay type"


def test_no_relay_type():
    relay = FakePF("R", "ElmRelay")
    relay.typ_id = None
    dev = FakeDevice("P", "R", pf_obj=relay, vt_secondary=110, vt_primary=11000)
    assert vs.update_vt(None, dev, UpdateResult()).vt_result == "No relay type"
