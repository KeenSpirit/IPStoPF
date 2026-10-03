"""Results CSV: stable header, SETTING_ID, blank RESULT on success."""

import csv

from core.update_result import CSV_COLUMNS, UpdateResult
from utils.file_utils import write_dict_list_to_csv
from tests.fakes import FakeDevice, FakePF


def test_header_is_fixed_and_complete(tmp_path):
    path = tmp_path / "out.csv"
    rows = [UpdateResult(substation="BLN", plant_number="X", result="Not in IPS").to_dict()]
    write_dict_list_to_csv(rows, str(path), fieldnames=CSV_COLUMNS)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == CSV_COLUMNS
        row = next(reader)
    assert row["RESULT"] == "Not in IPS" and row["CB_NAME"] == ""


def test_setting_id_from_device_and_success_row_has_blank_result():
    relay = FakePF("RIS1_J08", "ElmRelay", attrs={"r:cpGrid:e:loc_name": "RIS"})
    dev = FakeDevice("P123", "RIS1", pf_obj=relay, setting_id="6A24", date="2021-10-21")
    d = UpdateResult.from_device(dev).to_dict()
    assert d["SETTING_ID"] == "6A24" and "RESULT" not in d and d["DATE_SETTING"]


def test_setting_id_is_the_last_column():
    assert CSV_COLUMNS[-1] == "SETTING_ID"
    assert CSV_COLUMNS[:3] == ["SUBSTATION", "PLANT_NUMBER", "RELAY_PATTERN"]
