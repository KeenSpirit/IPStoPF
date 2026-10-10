"""
Tests for the incremental-run fingerprint module.

Offline: no PowerFactory runtime or network access required.
Run with:  pytest incremental/test_fingerprint.py
"""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from incremental.fingerprint import (
    SCHEMA_VERSION,
    AssessmentFingerprint,
    Decision,
    ProjectState,
    TransferFingerprint,
    decide,
    diff_fingerprints,
    digest_records,
    digest_value,
    hash_file,
    hash_files,
    hash_tree,
    load_state,
    lookup_key,
    parse_lookup_key,
    save_state,
    state_path,
)

AEST = timezone(timedelta(hours=10))
NOW = datetime(2026, 10, 12, 2, 0, tzinfo=AEST)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _transfer(**overrides) -> TransferFingerprint:
    base = TransferFingerprint(
        base_version=r"\Publisher\MasterProjects\SEQ Models\Richlands\v42",
        setting_lookups={
            lookup_key("get_by_switch_name", "CB01", "RLD"): "a1",
            lookup_key("get_by_asset_exact", "RC-901946"): "a2",
        },
        skeleton_lookups={},
        ods_params={"SID-1": "p1", "SID-2": "p2"},
        it_rows={"SID-1": "i1"},
        mapping_files={"P123_Energex_to_P12x.csv": "m1"},
        shared_files={"type_mapping": "t1", "curve_mapping": "c1"},
        code="c" * 64,
    )
    return replace(base, **overrides)


def _assessment(**overrides) -> AssessmentFingerprint:
    base = AssessmentFingerprint(
        code="s" * 64,
        data_files={"grid_results_egx.xlsx": "g1"},
    )
    return replace(base, **overrides)


def _state(**overrides) -> ProjectState:
    base = ProjectState(
        project="Richlands",
        region="Energex",
        run_id="20261005_020000",
        finished="2026-10-05T09:00:00+10:00",
        audit_version="20261005 IPS Import",
        transfer=_transfer(),
        assessment=_assessment(),
    )
    return replace(base, **overrides)


def _decide(previous, transfer=None, assessment=None, **kwargs):
    kwargs.setdefault("audit_version_present", True)
    kwargs.setdefault("now", NOW)
    return decide(
        previous,
        transfer if transfer is not None else _transfer(),
        assessment if assessment is not None else _assessment(),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #

def test_digest_records_ignores_row_and_key_order():
    rows = [
        {"relaysettingid": "1", "datesetting": "2026-01-01"},
        {"relaysettingid": "2", "datesetting": "2026-02-01"},
    ]
    reordered = [
        {"datesetting": "2026-02-01", "relaysettingid": "2"},
        {"datesetting": "2026-01-01", "relaysettingid": "1"},
    ]
    assert digest_records(rows) == digest_records(reordered)


def test_digest_records_sees_a_changed_value():
    rows = [{"relaysettingid": "1", "datesetting": "2026-01-01"}]
    newer = [{"relaysettingid": "1", "datesetting": "2026-03-01"}]
    assert digest_records(rows) != digest_records(newer)


def test_digest_records_counts_duplicates():
    row = {"relaysettingid": "1"}
    assert digest_records([row]) != digest_records([row, row])


def test_digest_records_empty_differs_from_one_row():
    assert digest_records([]) != digest_records([{"relaysettingid": "1"}])


def test_digest_handles_dates():
    assert digest_value({"d": date(2026, 1, 1)}) == \
        digest_value({"d": "2026-01-01"})


def test_lookup_key_round_trip():
    key = lookup_key("get_by_switch_name", "CB01", None)
    assert parse_lookup_key(key) == ("get_by_switch_name", ("CB01", None))


@pytest.mark.parametrize("bad", ["", "not json", "[]", "[1, 2]", '{"a": 1}'])
def test_parse_lookup_key_rejects_non_keys(bad):
    assert parse_lookup_key(bad) is None


# --------------------------------------------------------------------------- #
# File hashing
# --------------------------------------------------------------------------- #

def test_hash_file_missing_is_none(tmp_path):
    assert hash_file(tmp_path / "absent.csv") is None


def test_hash_file_tracks_content(tmp_path):
    f = tmp_path / "map.csv"
    f.write_text("a,b\n")
    first = hash_file(f)
    f.write_text("a,c\n")
    assert hash_file(f) != first


def test_hash_files_labels_and_missing(tmp_path):
    (tmp_path / "type_mapping.csv").write_text("x")
    result = hash_files({
        "type_mapping": tmp_path / "type_mapping.csv",
        "curve_mapping": tmp_path / "curve_mapping.csv",
    })
    assert result["type_mapping"] is not None
    assert result["curve_mapping"] is None


def _make_repo(root):
    (root / "pkg").mkdir(parents=True)
    (root / "main.py").write_text("print('main')\n")
    (root / "pkg" / "mod.py").write_text("X = 1\n")
    return root


def test_hash_tree_stable(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    assert hash_tree(repo) == hash_tree(repo)


def test_hash_tree_sees_edit_add_and_rename(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    original = hash_tree(repo)

    (repo / "pkg" / "mod.py").write_text("X = 2\n")
    edited = hash_tree(repo)
    assert edited != original

    (repo / "pkg" / "new.py").write_text("Y = 1\n")
    added = hash_tree(repo)
    assert added != edited

    (repo / "pkg" / "new.py").rename(repo / "pkg" / "renamed.py")
    assert hash_tree(repo) != added


def test_hash_tree_ignores_tests_caches_and_non_code(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    original = hash_tree(repo)
    (repo / "pkg" / "test_mod.py").write_text("def test(): pass\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\0")
    (repo / "results_log").mkdir()
    (repo / "results_log" / "stray.py").write_text("x")
    (repo / "notes.md").write_text("docs")
    assert hash_tree(repo) == original


def test_hash_tree_missing_root_is_none(tmp_path):
    assert hash_tree(tmp_path / "nowhere") is None


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

def test_identical_fingerprints_have_no_differences():
    assert diff_fingerprints(_transfer(), _transfer()) == []


def test_keyed_diff_reports_changed_added_removed():
    new = _transfer(ods_params={"SID-1": "p1-new", "SID-3": "p3"})
    reasons = diff_fingerprints(_transfer(), new)
    assert reasons == [
        "IPS parameters: 1 changed (SID-1); 1 added (SID-3); "
        "1 removed (SID-2)"
    ]


def test_keyed_diff_caps_examples():
    old = _transfer(ods_params={f"SID-{i:02d}": "x" for i in range(8)})
    new = _transfer(ods_params={f"SID-{i:02d}": "y" for i in range(8)})
    (reason,) = diff_fingerprints(old, new)
    assert reason.startswith("IPS parameters: 8 changed (SID-00,")
    assert reason.endswith("and 3 more)")


def test_base_version_reason_names_both_versions():
    new = _transfer(base_version=r"\...\Richlands\v43")
    (reason,) = diff_fingerprints(_transfer(), new)
    assert "v42" in reason and "v43" in reason


def test_code_digest_reason_is_short():
    (reason,) = diff_fingerprints(_transfer(), _transfer(code="d" * 64))
    assert reason == "code changed"


def test_missing_mapping_file_counts_as_changed():
    new = _transfer(mapping_files={"P123_Energex_to_P12x.csv": None})
    (reason,) = diff_fingerprints(_transfer(), new)
    assert reason.startswith("relay mapping files: 1 changed")


def test_unreadable_code_counts_as_changed():
    (reason,) = diff_fingerprints(_transfer(), _transfer(code=None))
    assert reason.startswith("code:")


def test_diff_rejects_mixed_types():
    with pytest.raises(TypeError):
        diff_fingerprints(_transfer(), _assessment())


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #

def test_unchanged_project_is_skipped():
    result = _decide(_state())
    assert result.decision is Decision.SKIP
    assert result.reasons == ("unchanged since run 20261005_020000",)


def test_no_previous_state_runs_full():
    assert _decide(None).decision is Decision.FULL


def test_force_runs_full_even_if_unchanged():
    result = _decide(_state(), force=True)
    assert result.decision is Decision.FULL
    assert result.reasons == ("forced full run",)


def test_old_state_runs_full():
    result = _decide(_state(), now=NOW + timedelta(days=30))
    assert result.decision is Decision.FULL
    assert "days old" in result.reasons[0]


def test_unreadable_finish_time_runs_full():
    result = _decide(_state(finished="last Tuesday"))
    assert result.decision is Decision.FULL


def test_naive_finish_time_runs_full():
    # A timestamp without an offset cannot be compared with an aware now.
    result = _decide(_state(finished="2026-10-05T09:00:00"))
    assert result.decision is Decision.FULL


def test_other_schema_runs_full():
    result = _decide(_state(schema_version=SCHEMA_VERSION + 1))
    assert result.decision is Decision.FULL


def test_missing_audit_version_runs_full():
    result = _decide(_state(), audit_version_present=False)
    assert result.decision is Decision.FULL
    assert "20261005 IPS Import" in result.reasons[0]


def test_no_audit_version_recorded_runs_full():
    result = _decide(_state(audit_version=None))
    assert result.decision is Decision.FULL


def test_transfer_change_runs_full():
    result = _decide(_state(), transfer=_transfer(it_rows={"SID-1": "i2"}))
    assert result.decision is Decision.FULL
    assert result.reasons == ("IPS CT/VT details: 1 changed (SID-1)",)


def test_assessment_only_change_runs_spa_only():
    result = _decide(_state(), assessment=_assessment(code="t" * 64))
    assert result.decision is Decision.SPA_ONLY
    assert result.reasons == ("SPA code changed",)


def test_both_changed_runs_full_and_lists_both():
    result = _decide(
        _state(),
        transfer=_transfer(base_version="v43"),
        assessment=_assessment(data_files={"grid_results_egx.xlsx": "g2"}),
    )
    assert result.decision is Decision.FULL
    assert len(result.reasons) == 2
    assert result.reasons[1].startswith("SPA data files")


def test_summary_line():
    result = _decide(_state(), assessment=_assessment(code="t" * 64))
    assert result.summary() == "SPA_ONLY: SPA code changed"


# --------------------------------------------------------------------------- #
# State files
# --------------------------------------------------------------------------- #

def test_state_path_is_windows_safe(tmp_path):
    path = state_path(tmp_path, "Ergon", 'Gatton/Postmans: "Ridge"?')
    assert path.parent == tmp_path / "Ergon"
    assert path.name == "Gatton_Postmans_ _Ridge_.json"


def test_save_and_load_round_trip(tmp_path):
    path = state_path(tmp_path, "Energex", "Richlands")
    save_state(path, _state())
    assert load_state(path) == _state()


def test_save_leaves_no_temp_files(tmp_path):
    path = state_path(tmp_path, "Energex", "Richlands")
    save_state(path, _state())
    save_state(path, _state(run_id="20261012_020000"))
    assert [p.name for p in path.parent.iterdir()] == ["Richlands.json"]
    assert load_state(path).run_id == "20261012_020000"


def test_load_missing_is_none(tmp_path):
    assert load_state(tmp_path / "none.json") is None


def test_load_corrupt_is_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not json")
    assert load_state(path) is None


def test_load_incomplete_is_none(tmp_path):
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({"project": "Richlands"}))
    assert load_state(path) is None


def test_load_other_schema_is_none(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps(
        {**_state().to_dict(), "schema_version": SCHEMA_VERSION + 1}
    ))
    assert load_state(path) is None


def test_load_tolerates_unknown_fields(tmp_path):
    # A later version may add components; an older reader must not crash.
    data = _state().to_dict()
    data["transfer"]["future_component"] = {"k": "v"}
    path = tmp_path / "future.json"
    path.write_text(json.dumps(data))
    assert load_state(path) == _state()