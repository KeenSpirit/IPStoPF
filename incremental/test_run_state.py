"""
Tests for assembling and saving run state after a successful run.

Offline: no PowerFactory runtime or network access required.
Run with:  pytest incremental/test_run_state.py
"""
import pytest

from incremental.fingerprint import load_state, save_state, state_path
from incremental.recorder import RunRecorder
from incremental.run_state import (
    InputSnapshot,
    build_state,
    hash_folder_files,
    invalidate_state,
    region_for_folder,
    snapshot_inputs,
)

INPUTS = InputSnapshot(
    ipstopf_code="i" * 64,
    spa_code="s" * 64,
    spa_data={"data/grid_results_egx.xlsx": "g1"},
)


def _complete_recorder() -> RunRecorder:
    rec = RunRecorder("Richlands")
    rec.note_lookup("get_by_switch_name", ("CB01", "RLD"), [])
    rec.note_ods({"SID-1": "p1"})
    rec.mark_completed()
    return rec


def _build(**overrides):
    kwargs = dict(
        project="Richlands",
        region="Energex",
        run_id="20261012_020000",
        finished="2026-10-12T05:30:00+10:00",
        audit_version="20261012 IPS Import",
        base_version=r"\Publisher.IntUser\...\Richlands.IntPrj\v42.IntVersion",
        recorder=_complete_recorder(),
        inputs=INPUTS,
    )
    kwargs.update(overrides)
    return build_state(**kwargs)


# --------------------------------------------------------------------------- #
# Region
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("folder, region", [
    ("SEQ Models", "Energex"),
    ("Northern", "Ergon"),
    ("Southern", "Ergon"),
])
def test_region_for_folder(folder, region):
    assert region_for_folder(folder) == region


# --------------------------------------------------------------------------- #
# Input snapshot
# --------------------------------------------------------------------------- #

def test_hash_folder_files_skips_lock_files_and_metadata(tmp_path):
    (tmp_path / "grid_results_ee.xlsx").write_bytes(b"wb")
    (tmp_path / "~$grid_results_ee.xlsx").write_bytes(b"lock")
    (tmp_path / "Thumbs.db").write_bytes(b"x")
    (tmp_path / "old").mkdir()
    assert list(hash_folder_files(tmp_path)) == ["grid_results_ee.xlsx"]


def test_hash_folder_files_missing_folder(tmp_path):
    assert hash_folder_files(tmp_path / "absent") == {}


def _repo(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_snapshot_inputs(tmp_path):
    ips = _repo(tmp_path / "IPStoPF", {"main.py": "x = 1\n"})
    spa = _repo(tmp_path / "SPA", {
        "start.py": "y = 1\n",
        "data/grid_results_egx.xlsx": "wb",
    })
    ratings = tmp_path / "ratings_lookup.csv"
    ratings.write_text("Typcon,...\n")
    snap = snapshot_inputs(ips, spa, {"ratings_lookup.csv": ratings})
    assert snap.ipstopf_code and snap.spa_code
    assert snap.ipstopf_code != snap.spa_code
    assert set(snap.spa_data) == {
        "data/grid_results_egx.xlsx", "ratings_lookup.csv",
    }
    assert "2 SPA data file(s)" in snap.summary()


def test_snapshot_missing_extra_file_is_flagged(tmp_path):
    ips = _repo(tmp_path / "IPStoPF", {"main.py": "x = 1\n"})
    spa = _repo(tmp_path / "SPA", {"start.py": "y = 1\n"})
    snap = snapshot_inputs(ips, spa, {"ratings_lookup.csv": tmp_path / "no.csv"})
    assert snap.spa_data == {"ratings_lookup.csv": None}
    assert "1 unreadable: ratings_lookup.csv" in snap.summary()


def test_snapshot_missing_code_folder_is_none(tmp_path):
    spa = _repo(tmp_path / "SPA", {"start.py": "y = 1\n"})
    snap = snapshot_inputs(tmp_path / "nowhere", spa)
    assert snap.ipstopf_code is None
    assert "IPStoPF code UNREADABLE" in snap.summary()


# --------------------------------------------------------------------------- #
# build_state
# --------------------------------------------------------------------------- #

def test_complete_run_builds_state():
    state, reason = _build()
    assert reason == "complete"
    assert state.transfer.code == "i" * 64
    assert state.transfer.base_version.endswith("v42.IntVersion")
    assert state.transfer.ods_params == {"SID-1": "p1"}
    assert state.assessment.code == "s" * 64
    assert state.assessment.data_files == {"data/grid_results_egx.xlsx": "g1"}
    assert state.audit_version == "20261012 IPS Import"


def test_unfinished_transfer_is_not_saved():
    state, reason = _build(recorder=RunRecorder("Richlands"))
    assert state is None
    assert reason == "fingerprint incomplete (transfer did not finish)"


def test_recording_problem_is_not_saved():
    rec = _complete_recorder()
    rec.mark_incomplete("ODS unavailable: no parameter aggregates recorded")
    state, reason = _build(recorder=rec)
    assert state is None and "ODS unavailable" in reason


def test_missing_audit_version_is_not_saved():
    state, reason = _build(audit_version=None)
    assert state is None and reason == "no audit version was created"


@pytest.mark.parametrize("inputs, expected", [
    (InputSnapshot(None, "s" * 64), "IPStoPF code could not be hashed"),
    (InputSnapshot("i" * 64, None), "SPA code could not be hashed"),
])
def test_unreadable_code_is_not_saved(inputs, expected):
    state, reason = _build(inputs=inputs)
    assert state is None and reason == expected


def test_unreadable_spa_data_is_not_saved():
    inputs = InputSnapshot("i" * 64, "s" * 64, {
        "data/grid_results_egx.xlsx": "g1", "ratings_lookup.csv": None,
    })
    state, reason = _build(inputs=inputs)
    assert state is None
    assert reason == "SPA data file(s) unreadable: ratings_lookup.csv"


def test_built_state_round_trips(tmp_path):
    state, _ = _build()
    path = state_path(tmp_path, "SEQ Models", "Richlands")
    save_state(path, state)
    assert load_state(path) == state


# --------------------------------------------------------------------------- #
# invalidate_state
# --------------------------------------------------------------------------- #

def test_invalidate_removes_state(tmp_path):
    state, _ = _build()
    path = state_path(tmp_path, "SEQ Models", "Richlands")
    save_state(path, state)
    assert invalidate_state(path) is True
    assert not path.exists()


def test_invalidate_missing_state(tmp_path):
    assert invalidate_state(tmp_path / "none.json") is False