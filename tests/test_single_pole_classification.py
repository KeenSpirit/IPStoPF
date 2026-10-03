"""Earth-fault single-pole relays must reach their '_Earth' type mapping."""

import pytest

from update_powerfactory import relay_settings as rs
from tests.fakes import FakeDevice, FakePF


EARTH_ROWS = {"MCGG22_Earth", "MCGG21_Earth", "RXIDF_Earth"}


@pytest.fixture
def earth_rows(monkeypatch):
    monkeypatch.setattr(
        rs.mf, "get_type_mapping",
        lambda pattern, ct=None: ("map", "model") if pattern in EARTH_ROWS else None,
    )


@pytest.mark.parametrize("name, expected_phase", [
    ("FILASS-FB55-J01-S103-3OC-A-A", 0),
    ("FILASS-FB55-J01-S103-3OC-B-B", 1),
    ("FILASS-FB55-J01-S103-3OC-C-C", 2),
    ("BILOSS-FB51-J01-OC-A", 0),
])
def test_phase_relays(earth_rows, name, expected_phase):
    dev = FakeDevice("MCGG22", name)
    assert rs.determine_phase(None, dev) == expected_phase
    assert dev.device == "MCGG22"


@pytest.mark.parametrize("pattern, name", [
    ("MCGG22", "FILASS-FB55-J01-S103-EF-E"),
    ("MCGG21", "MOURSS-FA62-J01-J50/51-N-E"),
    ("MCGG22", "ICIZSS-FB52-J01-S104-EF-E"),
])
def test_earth_relays_get_earth_pattern(earth_rows, pattern, name):
    dev = FakeDevice(pattern, name)
    assert rs.determine_phase(None, dev) is None
    assert dev.device == f"{pattern}_Earth"


def test_earth_relay_without_earth_row_keeps_phase_row(monkeypatch):
    monkeypatch.setattr(rs.mf, "get_type_mapping", lambda p, ct=None: None)
    dev = FakeDevice("MCGG22", "FILASS-FB55-J01-S103-EF-E")
    assert rs.determine_phase(None, dev) is None
    assert dev.device == "MCGG22"


def test_not_single_pole_untouched(earth_rows):
    dev = FakeDevice("P123 V13 V14 HEX", "BLN13A")
    assert rs.determine_phase(None, dev) is None
    assert dev.device == "P123 V13 V14 HEX"


def test_none_seq_name_falls_back_to_name(earth_rows):
    dev = FakeDevice("MCGG22", "X-OC-A", seq_name=None)
    assert rs.determine_phase(None, dev) == 0


def test_relay_settings_looks_up_earth_pattern(earth_rows, monkeypatch):
    """The mapping lookup must see the '_Earth' pattern (ordering fix)."""
    seen = []

    def fake_read_mapping_file(app, pattern, pf_obj, ct_secondary=None):
        seen.append(pattern)
        return None, None

    monkeypatch.setattr(rs.mf, "read_mapping_file", fake_read_mapping_file)
    cubicle = FakePF("cub", "StaCubic", attrs={"nphase": 3})
    relay = FakePF("FILASS-FB55-J01-S103-EF-E", "ElmRelay",
                   attrs={"r:fold_id:e:nphase": 3, "r:cpGrid:e:loc_name": "FILA",
                          "outserv": 0},
                   parent=cubicle)
    relay.typ_id = None
    dev = FakeDevice("MCGG22", relay.loc_name, pf_obj=relay)

    result, _ = rs.relay_settings(None, dev, rs.RelayTypeIndex(), False)

    assert seen == ["MCGG22_Earth"]
    assert result.relay_pattern == "MCGG22"
