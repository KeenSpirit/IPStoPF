"""pu base finalisation: repair, leave alone, and skip disabled stages."""

from update_powerfactory import pu_base_check as pbc
from tests.fakes import FakeDevice, FakePF


class _Elem(FakePF):
    """Re-deriving Ipset recomputes cpIpset against the (now correct) base."""

    def __init__(self, name, ipset, cpipset, ptap, cls="RelIoc"):
        super().__init__(name, cls, attrs={"Ipset": ipset, "cpIpset": cpipset})
        self.ptap = ptap

    def SetAttribute(self, name, value):
        super().SetAttribute(name, value)
        if name.endswith("Ipset") and not name.endswith("cpIpset"):
            self.attrs["cpIpset"] = value * self.ptap


def _relay(elements, ptap=400, stap=5):
    ct = FakePF("R_CT", "StaCt", attrs={"ptapset": ptap, "stapset": stap})
    return FakePF("BLN13A_J17A", "ElmRelay", attrs={"pdiselm": [ct]},
                  contents=elements)


def _run(relay, times=1):
    devices = [FakeDevice("P", relay.loc_name, pf_obj=relay) for _ in range(times)]
    return pbc.finalise_pu_derivations(None, devices)


def test_stale_base_corrected():
    e = _Elem("I>>", 6.5, 208000.0, 400)          # Beenleigh 2026-10-03
    summary = _run(_relay([e]))
    assert summary["corrected"] == 1 and e.attrs["cpIpset"] == 2600


def test_other_factor_left_alone():
    e = _Elem("Ioc", 6, 2880.0, 600)               # referenced to I> pickup
    summary = _run(_relay([e], ptap=600))
    assert summary["unexpected"] == 1 and e.writes == []


def test_disabled_stage_skipped_quietly():
    a = _Elem("Ioc A", 1e7, 1e7, 200)
    b = _Elem("Ioc", float("inf"), 1e7, 150)
    summary = _run(_relay([a, b], ptap=200))
    assert summary["skipped"] == 2 and summary["unexpected"] == 0


def test_relay_listed_twice_checked_once():
    e = _Elem("I>", 1.0, 400.0, 400, cls="RelToc")
    summary = _run(_relay([e]), times=2)
    assert summary["relays"] == 1 and summary["elements"] == 1
