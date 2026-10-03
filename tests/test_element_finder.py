"""The indexed element finder must agree with find_element."""

from update_powerfactory import relay_settings as rs
from tests.fakes import FakePF


def _relay():
    i1 = FakePF("I>", "RelToc")
    i2 = FakePF("I>>", "RelIoc")
    trip1 = FakePF("Trip 1", "ElmRelay", contents=[FakePF("I>", "RelToc")])
    meas = FakePF("Measure Ph", "RelMeasure")
    return FakePF("R1", "ElmRelay", contents=[i1, i2, trip1, meas])


def test_agrees_with_find_element():
    relay = _relay()
    find = rs.make_element_finder(relay)
    for line in (["R1", "I>"], ["R1", "I>>"], ["Trip 1", "I>"],
                 ["R1", "Measure Ph"], ["Trip 2", "I>"], ["R1", "nope"]):
        assert find(None, relay, line) is rs.find_element(None, relay, line), line


def test_nested_folder_resolves_to_the_nested_element():
    relay = _relay()
    nested = rs.make_element_finder(relay)(None, relay, ["Trip 1", "I>"])
    assert nested is not None and nested.parent.loc_name == "Trip 1"
