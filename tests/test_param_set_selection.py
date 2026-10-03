"""Parameter-set choice when IPS holds several sets for one setting."""

from core.protection_device import ProtectionDevice, _set_name_rank


def _rows(set_id, name, date, reader="Nu-Lec WSOS5 (export CSV)"):
    return [{"relayparamsetid": set_id, "setname": name, "dateimportrelay": date,
             "readername": reader, "blockpathenu": "b", "paramnameenu": "p",
             "proposedsetting": name}]


def _choose(*sets):
    dev = ProtectionDevice(None, "X", "X8662-B", "S", None, None, None)
    rows = [r for s in sets for r in s]
    return dev._select_param_set(rows)[0]["setname"]


def test_undated_tie_prefers_applied_over_issued():
    # X8662-B, Beenleigh 2026-10-03: both sets undated.
    assert _choose(_rows(1, "As Issued_Parameter Set", None),
                   _rows(2, "As Applied_Parameter Set", None)) == "As Applied_Parameter Set"


def test_latest_import_still_wins_when_dated():
    assert _choose(_rows(1, "As Applied", "2025-04-15 09:50:47"),
                   _rows(2, "As Found 2026", "2026-04-01 10:53:05")) == "As Found 2026"


def test_untrusted_reader_still_excluded():
    assert _choose(_rows(1, "As Issued", "2021-10-21 15:17:50", "ALSTOM CAPE"),
                   _rows(2, "As Applied", "2025-06-16 12:00:01",
                         "ALSTOM Modbus (native SET)")) == "As Issued"


def test_rank():
    assert _set_name_rank("AsLeft_STNW1099") == 2
    assert _set_name_rank("as_appliedParameter Set") == 2
    assert _set_name_rank("As_issuedParameter Set") == 0
    assert _set_name_rank("Excel Import") == 1
