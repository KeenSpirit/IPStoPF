"""A relay with no CT in IPS must not be left reading secondary amps as primary."""

from core.update_result import UpdateResult
from update_powerfactory import ct_settings as cs
from tests.fakes import FakeDevice, FakePF


def _relay(slot_filters, slot_objs, folder="Relays"):
    blocks = [FakePF(f"slot{i}", "IntSlot", attrs={"filtmod": f})
              if f is not None else None
              for i, f in enumerate(slot_filters)]
    rtype = FakePF("TypeX", "TypRelay", attrs={"pblk": blocks})
    rtype.fold_id_override = FakePF(folder, "IntFolder")
    relay = FakePF("R1_J01", "ElmRelay",
                   attrs={"pdiselm": list(slot_objs), "typ_id": rtype,
                          "outserv": 0})
    relay.typ_id = rtype
    return relay


def test_existing_model_ct_is_kept():
    ct = FakePF("R1_CT", "StaCt", attrs={"ptapset": 400, "stapset": 5})
    relay = _relay(["StaCt*", "StaVt*"], [ct, None])
    dev = FakeDevice("P", "R1", pf_obj=relay, ct_primary=1, ct_secondary=1)
    res = cs._handle_no_ips_ct(dev, UpdateResult())
    assert res.ct_result == "No CT in IPS - model CT kept"
    assert res.ct_name == "R1_CT"
    assert relay.attrs["pdiselm"] == [ct, None]      # slot NOT cleared
    assert relay.attrs["outserv"] == 0


def test_no_ct_anywhere_sets_relay_out_of_service():
    relay = _relay(["StaCt*"], [None])
    dev = FakeDevice("P", "R1", pf_obj=relay, ct_primary=1, ct_secondary=1)
    res = cs._handle_no_ips_ct(dev, UpdateResult())
    assert relay.attrs["outserv"] == 1
    assert res.ct_result == "No CT in IPS or model"
    assert res.result == "No CT - set out of service"


def test_existing_result_not_overwritten():
    relay = _relay(["StaCt*"], [None])
    dev = FakeDevice("P", "R1", pf_obj=relay, ct_primary=1, ct_secondary=1)
    res = cs._handle_no_ips_ct(dev, UpdateResult(result="Type not found: X"))
    assert res.result == "Type not found: X"


def test_type_without_ct_slot_left_alone():
    relay = _relay(["StaVt*"], [None])
    dev = FakeDevice("P", "R1", pf_obj=relay, ct_primary=1, ct_secondary=1)
    res = cs._handle_no_ips_ct(dev, UpdateResult())
    assert res.ct_result == "No CT slot on relay type"
    assert relay.attrs["outserv"] == 0


def test_remote_ct_slot_is_not_treated_as_model_ct():
    remote_ct = FakePF("R1_CT", "StaCt")
    relay = _relay(["StaCt*"], [remote_ct])
    relay.attrs["typ_id"].attrs["pblk"][0].loc_name = "Ct-3P(remote)"
    has_slot, ct_obj = cs._model_ct(relay)
    assert (has_slot, ct_obj) == (False, None)


def test_update_ct_without_relay_type_does_not_raise():
    relay = FakePF("R1_J01", "ElmRelay", attrs={"outserv": 1})
    relay.typ_id = None
    dev = FakeDevice("P", "R1", pf_obj=relay)
    res = cs.update_ct(None, dev, UpdateResult(), ct_library=object())
    assert res.ct_result == "No relay type"
