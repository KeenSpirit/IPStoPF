"""Energex switch-name mapping through CB_ALT_NAME."""

from ips_data import ex_settings as ex
from tests.fakes import FakePF

ROWS = [
    {"SUBSTATION": "BLN", "CB_NAME": "CB12", "NEW_NAME": "BLN12A"},
    {"SUBSTATION": "BLN", "CB_NAME": "CB12", "NEW_NAME": "SHADOWED"},
    {"SUBSTATION": "STT", "CB_NAME": "CB1", "NEW_NAME": "STT1B_X"},
]


def _switch(sub, name, cls="ElmCoup"):
    folder = FakePF(sub, "ElmSubstat")
    sw = FakePF(name, cls)
    folder.contents.append(sw)
    sw.parent = folder
    return sw


def test_dict_and_list_agree_and_first_row_wins():
    index = ex.cb_alt_name_index(ROWS)
    for sub, name in [("BLN", "CB12"), ("STT", "CB1"), ("BLN", "BLN3A_X")]:
        sw = _switch(sub, name)
        assert ex._get_switch_info(sw, index) == ex._get_switch_info(sw, ROWS)
    assert ex._get_switch_info(_switch("BLN", "CB12"), index) == ("BLN12A", "BLN")


def test_staswitch_has_no_sub_code():
    assert ex._get_switch_info(_switch("STT", "CB1", "StaSwitch"),
                               ex.cb_alt_name_index(ROWS)) == ("STT1B", None)
