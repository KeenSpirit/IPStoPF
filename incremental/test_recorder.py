"""
Tests for the incremental-run recorder and its hooks in the pure modules
(SettingIndex, mapping_file, cb_mapping).

Offline: no PowerFactory runtime or network access required.
Run with:  pytest incremental/test_recorder.py
"""
from collections import defaultdict, namedtuple
from decimal import Decimal

import pytest

from incremental import recorder as rr
from incremental.fingerprint import lookup_key
from incremental.recorder import (
    RunRecorder,
    it_digests,
    ods_digests,
    records_digest,
    recording,
)


@pytest.fixture(autouse=True)
def _fresh_process_state():
    """Each test starts with no active recorder and no loaded-file hashes."""
    rr._loaded_digests.clear()
    yield
    rr._loaded_digests.clear()


def _row(sid, asset, date="2026-01-01", pattern="P123", **extra):
    return {
        "relaysettingid": sid,
        "assetname": asset,
        "patternname": pattern,
        "datesetting": date,
        **extra,
    }


# --------------------------------------------------------------------------- #
# Digest helpers
# --------------------------------------------------------------------------- #

def test_records_digest_ignores_order():
    a, b = _row("1", "RC-1"), _row("2", "RC-2")
    assert records_digest([a, b]) == records_digest([b, a])


def test_records_digest_uses_setting_record_fields_only():
    # SettingRecord.to_dict drops raw report columns the code never reads,
    # so an unused volatile column cannot force a re-run.
    from core import SettingRecord
    plain = SettingRecord.from_dict(_row("1", "RC-1"))
    extra = SettingRecord.from_dict(_row("1", "RC-1", cache_written="today"))
    assert records_digest([plain]) == records_digest([extra])


def test_ods_digests_cover_every_requested_id():
    digests = ods_digests(["A", "B"], [("A", 10, 123, "2026-01-01")], "content")
    assert set(digests) == {"A", "B"}
    assert digests["A"] != digests["B"]


def test_ods_digests_normalise_oracle_number_types():
    as_int = ods_digests(["A"], [("A", 10, 123, None)], "content")
    as_float = ods_digests(["A"], [("A", 10.0, 123.0, None)], "content")
    as_decimal = ods_digests(["A"], [("A", Decimal(10), Decimal(123), None)],
                             "content")
    assert as_int == as_float == as_decimal


def test_ods_digests_see_content_and_import_date():
    base = ods_digests(["A"], [("A", 10, 123, "2026-01-01")], "content")
    edited = ods_digests(["A"], [("A", 10, 124, "2026-01-01")], "content")
    imported = ods_digests(["A"], [("A", 10, 123, "2026-02-01")], "content")
    assert base != edited
    assert base != imported


def test_ods_digests_depend_on_mode():
    rows = [("A", 10, 0, "2026-01-01")]
    assert ods_digests(["A"], rows, "content") != ods_digests(["A"], rows, "dates")


def test_it_digests_group_by_setting_id():
    It = namedtuple("It", "relaysettingid ct_ratio")
    rows = [It("A", "600/5"), It("A", "300/5"), It("B", "200/1")]
    digests = it_digests(["A", "B", "C"], rows)
    assert set(digests) == {"A", "B", "C"}
    assert digests["A"] == it_digests(["A"], list(reversed(rows)))["A"]
    assert digests["C"] == it_digests(["C"], [])["C"]


def test_keyed_digests_are_short():
    assert len(records_digest([_row("1", "RC-1")])) == rr.KEYED_DIGEST_LENGTH


# --------------------------------------------------------------------------- #
# RunRecorder
# --------------------------------------------------------------------------- #

def test_recorder_complete_only_after_normal_end():
    rec = RunRecorder("Richlands")
    assert not rec.is_complete
    rec.mark_completed()
    assert rec.is_complete
    rec.mark_incomplete("ODS unavailable")
    assert not rec.is_complete


def test_transfer_fingerprint_carries_components():
    rec = RunRecorder()
    rec.note_lookup("get_by_asset_exact", ("RC-1",), [_row("1", "RC-1")])
    rec.note_ods({"1": "abc"})
    rec.note_file(rr.RELAY_MAP, "P123_Energex_to_P12x", "m1")
    rec.note_file(rr.SHARED_FILE, "type_mapping.csv", "t1")
    fp = rec.transfer_fingerprint(base_version="v42", code="c" * 64)
    assert fp.base_version == "v42"
    assert fp.code == "c" * 64
    assert list(fp.setting_lookups) == [lookup_key("get_by_asset_exact", "RC-1")]
    assert fp.ods_params == {"1": "abc"}
    assert fp.mapping_files == {"P123_Energex_to_P12x": "m1"}
    assert fp.shared_files == {"type_mapping.csv": "t1"}


def test_fingerprint_is_a_copy():
    rec = RunRecorder()
    fp = rec.transfer_fingerprint(None, None)
    rec.note_ods({"1": "abc"})
    assert fp.ods_params == {}


def test_summary_states():
    rec = RunRecorder("Richlands")
    rec.note_it_rows(["A", "B"], [{"relaysettingid": "A", "ct": "600/5"}])
    assert "1 of 2 with CT/VT rows" in rec.summary()
    assert "INCOMPLETE: transfer did not reach its normal end" in rec.summary()
    rec.mark_completed()
    assert rec.summary().endswith("; complete")
    rec.mark_incomplete("ODS unavailable")
    assert rec.summary().endswith("INCOMPLETE: ODS unavailable")


# --------------------------------------------------------------------------- #
# Active recorder and hooks
# --------------------------------------------------------------------------- #

def test_hooks_are_no_ops_without_a_recorder():
    assert rr.active() is None
    rr.note_lookup("get_by_asset_exact", ("RC-1",), [])
    rr.note_ods({"1": "x"})
    rr.mark_completed()  # nothing to complete; must not raise


def test_recording_sets_and_restores():
    outer, inner = RunRecorder("outer"), RunRecorder("inner")
    with recording(outer):
        assert rr.active() is outer
        with recording(inner):
            assert rr.active() is inner
        assert rr.active() is outer
    assert rr.active() is None


def test_recording_restores_after_an_exception():
    with pytest.raises(RuntimeError):
        with recording(RunRecorder()):
            raise RuntimeError("transfer failed")
    assert rr.active() is None


def test_failing_hook_marks_incomplete_without_raising():
    rec = RunRecorder()
    with recording(rec):
        rr.note_lookup("get_by_asset_exact", ("RC-1",), 42)  # not iterable
    assert not rec.setting_lookups
    assert rec.problems and rec.problems[0].startswith("setting lookup failed")


def test_note_skeleton_reads_without_adding_keys():
    relay = defaultdict(list, {"101": [{"plant_no": "X1"}]})
    fuse = defaultdict(list)
    rec = RunRecorder()
    with recording(rec):
        rr.note_skeleton("101", relay=relay, fuse=fuse)
        rr.note_skeleton("999", relay=relay, fuse=fuse)
    assert set(rec.skeleton_lookups) == {
        lookup_key("skeleton", "101"), lookup_key("skeleton", "999"),
    }
    assert set(relay) == {"101"} and not fuse


def test_skeleton_digest_sees_a_new_register_row():
    rec_a, rec_b = RunRecorder(), RunRecorder()
    with recording(rec_a):
        rr.note_skeleton("999", fuse={})
    with recording(rec_b):
        rr.note_skeleton("999", fuse={"999": [{"plant_no": "DO-1"}]})
    assert rec_a.skeleton_lookups != rec_b.skeleton_lookups


# --------------------------------------------------------------------------- #
# Loaded files
# --------------------------------------------------------------------------- #

def test_file_hash_is_taken_when_read(tmp_path):
    f = tmp_path / "type_mapping.csv"
    f.write_text("v1")
    rec_1, rec_2 = RunRecorder(), RunRecorder()
    with recording(rec_1):
        rr.file_loaded(rr.SHARED_FILE, "type_mapping.csv", f)
    # Edited mid-run: the process keeps using its cached v1 contents, so
    # a later project must report v1's hash, not the file's new one.
    f.write_text("v2")
    with recording(rec_2):
        rr.file_used(rr.SHARED_FILE, "type_mapping.csv")
    assert rec_2.shared_files == rec_1.shared_files
    assert rec_2.shared_files["type_mapping.csv"] is not None


def test_file_loaded_hashes_without_a_recorder(tmp_path):
    f = tmp_path / "map.csv"
    f.write_text("x")
    rr.file_loaded(rr.RELAY_MAP, "map", f)  # interactive run, no recorder
    rec = RunRecorder()
    with recording(rec):
        rr.file_used(rr.RELAY_MAP, "map")
    assert rec.mapping_files["map"] is not None


def test_unknown_or_missing_file_reports_none(tmp_path):
    rec = RunRecorder()
    with recording(rec):
        rr.file_used(rr.RELAY_MAP, "never_loaded")
        rr.file_loaded(rr.RELAY_MAP, "absent", tmp_path / "absent.csv")
    assert rec.mapping_files == {"never_loaded": None, "absent": None}


# --------------------------------------------------------------------------- #
# Hooks in the pure IPStoPF modules
# --------------------------------------------------------------------------- #

def _index(rows, region="Ergon"):
    from ips_data.setting_index import create_setting_index
    return create_setting_index(rows, region)


ERGON_ROWS = [
    _row("S1", "DO-589200", pattern="Ergon_Fuse", active=True),
    _row("S2", "GLFSSS-FB59-J01-EF", active=True),
    _row("S3", "GLFSSS-FB59-J01-OC", active=True),
]


def _replay(index, keys):
    """What the precheck will do: repeat each recorded call on an index."""
    from incremental.fingerprint import parse_lookup_key
    rec = RunRecorder()
    with recording(rec):
        for key in keys:
            method, args = parse_lookup_key(key)
            getattr(index, method)(*args)
    return rec.setting_lookups


def test_setting_index_records_each_lookup():
    index = _index(ERGON_ROWS)
    rec = RunRecorder()
    with recording(rec):
        index.get_by_asset_exact("DO-589200")
        index.get_by_asset_contains("GLFSSS-FB59-J01")
        index.get_by_asset_exact("DO-000000")   # misses are recorded too
    assert set(rec.setting_lookups) == {
        lookup_key("get_by_asset_exact", "DO-589200"),
        lookup_key("get_by_asset_contains", "GLFSSS-FB59-J01"),
        lookup_key("get_by_asset_exact", "DO-000000"),
    }


def test_setting_index_results_unchanged_when_recorded():
    index = _index(ERGON_ROWS)
    plain = index.get_by_asset_contains("GLFSSS-FB59-J01")
    with recording(RunRecorder()):
        recorded = index.get_by_asset_contains("GLFSSS-FB59-J01")
    assert plain == recorded and len(plain) == 2


def test_replay_matches_on_unchanged_ips():
    rec = RunRecorder()
    with recording(rec):
        _index(ERGON_ROWS).get_by_asset_contains("GLFSSS-FB59-J01")
    assert _replay(_index(ERGON_ROWS), rec.setting_lookups) == rec.setting_lookups


def test_replay_sees_new_asset_in_cubicle_and_new_setting_date():
    rec = RunRecorder()
    with recording(rec):
        _index(ERGON_ROWS).get_by_asset_contains("GLFSSS-FB59-J01")
        _index(ERGON_ROWS).get_by_asset_exact("DO-589200")

    new_asset = ERGON_ROWS + [_row("S4", "GLFSSS-FB59-J01-SEF", active=True)]
    replayed = _replay(_index(new_asset), rec.setting_lookups)
    key_cub = lookup_key("get_by_asset_contains", "GLFSSS-FB59-J01")
    assert replayed[key_cub] != rec.setting_lookups[key_cub]

    redated = [dict(r) for r in ERGON_ROWS]
    redated[0]["datesetting"] = "2026-10-01"
    replayed = _replay(_index(redated), rec.setting_lookups)
    key_fuse = lookup_key("get_by_asset_exact", "DO-589200")
    assert replayed[key_fuse] != rec.setting_lookups[key_fuse]
    assert replayed[key_cub] == rec.setting_lookups[key_cub]


def test_switch_name_lookup_records_substation():
    rows = [{
        "relaysettingid": "E1", "assetname": "X1", "patternname": "P123",
        "datesetting": "2026-01-01", "nameenu": "CB01",
        "locationpathenu": "Energex/Substations/RLD/11 kV/CB01/",
    }]
    index = _index(rows, region="Energex")
    rec = RunRecorder()
    with recording(rec):
        index.get_by_switch_name("CB01", "RLD")
        index.get_by_switch_name("CB01")
    assert set(rec.setting_lookups) == {
        lookup_key("get_by_switch_name", "CB01", "RLD"),
        lookup_key("get_by_switch_name", "CB01", None),
    }


def test_mapping_file_loader_reports_on_miss_and_hit(tmp_path, monkeypatch):
    from update_powerfactory import mapping_file as mf
    f = tmp_path / "P123_Energex_to_P12x.csv"
    f.write_text("FOLDER,ELEMENT,ATTR\nI>,Ipset,0200\n")
    monkeypatch.setattr(mf, "get_relay_map_file", lambda name: f)
    mf.clear_cache()
    rec_1, rec_2 = RunRecorder(), RunRecorder()
    try:
        with recording(rec_1):
            mf._load_mapping_file("P123_Energex_to_P12x")   # miss: reads
        with recording(rec_2):
            mf._load_mapping_file("P123_Energex_to_P12x")   # hit: cached
    finally:
        mf.clear_cache()
    assert rec_1.mapping_files == rec_2.mapping_files
    assert rec_1.mapping_files["P123_Energex_to_P12x"] is not None


def test_type_mapping_loader_reports_shared_file(tmp_path, monkeypatch):
    from update_powerfactory import mapping_file as mf
    f = tmp_path / "type_mapping.csv"
    f.write_text("IPS,Exclude,CT Sec,MAPPING_FILE,PF_MODEL\n"
                 "P123,,,P123_Energex_to_P12x,P12x\n")
    monkeypatch.setattr(mf, "get_type_mapping_file", lambda: f)
    mf.clear_cache()
    rec = RunRecorder()
    try:
        with recording(rec):
            mf._load_type_mapping()
            mf._load_type_mapping()
    finally:
        mf.clear_cache()
    assert list(rec.shared_files) == ["type_mapping.csv"]
    assert rec.shared_files["type_mapping.csv"] is not None


def test_cb_alt_name_loader_reports_shared_file(tmp_path, monkeypatch):
    from ips_data import cb_mapping
    f = tmp_path / "CB_ALT_NAME.csv"
    f.write_text("PROJECT,GRID,SUBSTATION,CB_NAME,NEW_NAME\n"
                 "Richlands,G,RLD,CB01,RLD1A\n")
    monkeypatch.setattr(cb_mapping, "get_cb_alt_name_file", lambda: f)
    cb_mapping.clear_cache()
    rec = RunRecorder()
    try:
        with recording(rec):
            cb_mapping.get_cb_alt_name_list()
            cb_mapping.get_cb_alt_name_list()
    finally:
        cb_mapping.clear_cache()
    assert list(rec.shared_files) == ["CB_ALT_NAME.csv"]