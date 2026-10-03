"""set_attribute: zero results and on/off text on integer attributes."""

import pytest

from update_powerfactory import relay_settings as rs
from tests.fakes import FakeDevice, FakePF


def _line(attr, adjust="None", operand=None):
    # [folder, element, attribute, use_setting, ref, ref, adjust(6), operand(7)]
    line = ["R1", "I>", attr, "use_setting", "blk", "par", adjust]
    if operand is not None:
        line.append(operand)
    return line


def _call(line, value, element, dictionary=None):
    relay = FakePF("R1", "ElmRelay")
    dev = FakeDevice("P", "R1", pf_obj=relay)
    key = "".join(line[:3])
    return rs.set_attribute(None, line, value, element, f"e:{line[2]}", dev,
                            dictionary if dictionary is not None else {key: value},
                            False)


def test_adjusted_zero_is_written():
    element = FakePF("I>", "RelToc", attrs={"Tset": 0.5})
    line = _line("Tset", adjust="*", operand="0")
    assert _call(line, "0.3", element) is True
    assert element.attrs["Tset"] == 0


def test_adjusted_none_is_skipped(monkeypatch):
    monkeypatch.setattr(rs, "setting_adjustment", lambda *a, **k: None)
    element = FakePF("I>", "RelToc", attrs={"Tset": 0.5})
    assert _call(_line("Tset", adjust="primary"), "x", element) is False
    assert element.writes == []


@pytest.mark.parametrize("value, existing, expected", [
    ("off", 1, 0), ("OFF", 1, 0), ("on", 0, 1), ("Enabled", 0, 1),
])
def test_on_off_text_on_integer_attribute(value, existing, expected):
    element = FakePF("NSPTOC1", "RelToc", attrs={"ModFrame": existing})
    assert _call(_line("ModFrame"), value, element) is True
    assert element.attrs["ModFrame"] == expected
    assert 9999 not in [v for _, v in element.writes]


def test_on_off_already_matching_is_not_rewritten():
    element = FakePF("NSPTOC1", "RelToc", attrs={"ModFrame": 0})
    assert _call(_line("ModFrame"), "off", element) is False
    assert element.writes == []


def test_off_on_float_attribute_keeps_legacy_fallback():
    element = FakePF("I>", "RelToc", attrs={"Tset": 0.5})
    _call(_line("Tset"), "off", element)
    assert element.attrs["Tset"] == 9999


@pytest.mark.parametrize("value, existing, expected", [
    ("on", 1, 1), ("off", 0, 0), ("maybe", 1, None), ("on", 0.5, None),
    ("on", True, None), (1, 1, None),
])
def test_flag_value(value, existing, expected):
    assert rs._flag_value(value, existing) == expected
