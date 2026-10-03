"""Elements with no protection device must not cost further PF calls."""

from ips_data import add_relay_skeletons as ars


class _Elm:
    def __init__(self, for_name):
        self.for_name = for_name
        self.calls = []

    def GetAttribute(self, name):
        self.calls.append(name)
        return self.for_name

    def GetParent(self):
        self.calls.append("GetParent")
        raise AssertionError("feeder-CB check reached for an unprotected element")


def test_unprotected_element_returns_before_feeder_check():
    elm = _Elm("ELMCOUP12345678")
    out = ars.process_switch_for_relay_check(
        None, elm, {}, {}, {}, {}, set(), ars.NETWORK_DISTRIBUTION)
    assert out == [] and elm.calls == ["for_name"]


def test_element_without_foreign_key():
    elm = _Elm(None)
    assert ars.process_switch_for_relay_check(
        None, elm, {"1": []}, {}, {}, {}, set(), ars.NETWORK_DISTRIBUTION) == []
