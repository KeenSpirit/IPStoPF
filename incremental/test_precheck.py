"""
Tests for the incremental-run precheck.

The central test records a "run" with the real recorder and a real
SettingIndex, saves its state, and checks that the precheck reproduces
every digest from the same data - then changes one input at a time.

Offline: no PowerFactory runtime or network access required.
Run with:  pytest incremental/test_precheck.py
"""
from collections import defaultdict, namedtuple
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from incremental import recorder as rr
from incremental.fingerprint import Decision, lookup_key
from incremental.precheck import (
    SKELETON_REGISTERS,
    PrecheckSources,
    audit_version_present,
    run_precheck,
)
from incremental.recorder import RunRecorder, recording
from incremental.run_state import InputSnapshot, build_state

AEST = timezone(timedelta(hours=10))
RUN_TIME = datetime(2026, 10, 12, 5, 30, tzinfo=AEST)
NEXT_WEEK = RUN_TIME + timedelta(days=7)
BASE = r"\Publisher.IntUser\MasterProjects\Northern\Gladstone.IntPrj\v42.IntVersion"
AUDIT = "20261012 IPS Import"
It = namedtuple("It", "relaysettingid ct_ratio")
Reg = namedtuple("Reg", "asset_id plant_no ellipse_equip_no")


@pytest.fixture(autouse=True)
def _fresh_process_state():
    rr._loaded_digests.clear()
    yield
    rr._loaded_digests.clear()


def _row(sid, asset, date="2026-01-01", pattern="P123"):
    return {"relaysettingid": sid, "assetname": asset, "patternname": pattern,
            "datesetting": date, "active": True}


class World:
    """Everything the run and the precheck read, in one mutable place."""

    def __init__(self, tmp_path):
        self.report = [
            _row("S1", "DO-589200", pattern="Ergon_Fuse"),
            _row("S2", "GLFSSS-FB59-J01-EF"),
            _row("S3", "GLFSSS-FB59-J01-OC"),
        ]
        self.registers = {
            "relay": defaultdict(list, {"501": [Reg("501", "GLFSSS-FB59-J01", "E1")]}),
            "recloser": defaultdict(list),
            "fuse": defaultdict(list, {"777": [Reg("777", "DO-589200", "E2")]}),
            "gas_switch": defaultdict(list),
        }
        self.params = {"S1": (5, 111, "2026-01-01"), "S2": (40, 222, "2026-01-01"),
                       "S3": (40, 333, "2026-01-01")}
        self.it = [It("S2", "600/5"), It("S3", "600/5")]
        self.relay_map = tmp_path / "SPAJ_Ergon.csv"
        self.relay_map.write_text("FOLDER,ELEMENT,ATTR\nI>,Ipset,1\n")
        self.type_mapping = tmp_path / "type_mapping.csv"
        self.type_mapping.write_text("IPS,Exclude\nP123,\n")
        self.calls = defaultdict(int)

    # -- shared readers ----------------------------------------------------- #

    def index(self):
        from ips_data.setting_index import create_setting_index
        return create_setting_index(self.report, "Ergon")

    def ods(self, ids):
        rows = [(sid, *self.params[sid]) for sid in ids if sid in self.params]
        return rr.ods_digests(ids, rows, "content")

    # -- the "run" ---------------------------------------------------------- #

    def run(self) -> RunRecorder:
        rec = RunRecorder("Gladstone")
        with recording(rec):
            index = self.index()
            index.get_by_asset_exact("DO-589200")
            index.get_by_asset_contains("GLFSSS-FB59-J01")
            for asset_id in ("501", "777", "999"):
                rr.note_skeleton(asset_id, **self.registers)
            ids = ["S1", "S2", "S3"]
            rr.note_ods(self.ods(ids))
            rr.note_it_rows(ids, [r for r in self.it if r.relaysettingid in ids])
            rr.file_loaded(rr.RELAY_MAP, "SPAJ_Ergon", self.relay_map)
            rr.file_loaded(rr.SHARED_FILE, "type_mapping.csv", self.type_mapping)
            rr.mark_completed()
        return rec

    # -- the precheck's sources -------------------------------------------- #

    def sources(self, fail=None) -> PrecheckSources:
        def counted(name, func):
            def wrapper(*args):
                self.calls[name] += 1
                if fail == name:
                    raise RuntimeError(f"{name} unavailable")
                return func(*args)
            return wrapper
        return PrecheckSources(
            setting_index=counted("index", self.index),
            skeleton_registers=counted("registers", lambda: self.registers),
            it_rows=counted("it", lambda ids: [r for r in self.it
                                                if r.relaysettingid in ids]),
            ods_digests=counted("ods", self.ods),
            relay_map_path=lambda name: self.relay_map,
            shared_file_paths={"type_mapping.csv": self.type_mapping},
        )


INPUTS = InputSnapshot("i" * 64, "s" * 64, {"data/grid_results_ee.xlsx": "g1"})


def _saved_state(world, inputs=INPUTS):
    state, reason = build_state(
        project="Gladstone", region="Ergon", run_id="20261012_020000",
        finished=RUN_TIME.isoformat(), audit_version=AUDIT, base_version=BASE,
        recorder=world.run(), inputs=inputs,
    )
    assert reason == "complete"
    return state


def _precheck(world, state, *, sources=None, base=BASE, versions=(AUDIT,),
              inputs=INPUTS, **kwargs):
    return run_precheck(
        state, sources or world.sources(), base_version=base,
        version_names=versions, inputs=inputs, now=NEXT_WEEK, **kwargs,
    )


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# --------------------------------------------------------------------------- #
# Unchanged inputs
# --------------------------------------------------------------------------- #

def test_unchanged_project_is_skipped(world):
    outcome = _precheck(world, _saved_state(world))
    assert outcome.decision is Decision.SKIP, outcome.summary()


def test_skip_exercises_every_component(world):
    state = _saved_state(world)
    _precheck(world, state)
    assert dict(world.calls) == {"index": 1, "registers": 1, "ods": 1, "it": 1}


def test_precheck_does_not_record_into_an_active_recorder(world):
    state = _saved_state(world)
    outer = RunRecorder("outer")
    with recording(outer):
        _precheck(world, state)
        assert rr.active() is outer
    assert not outer.setting_lookups


# --------------------------------------------------------------------------- #
# One change at a time
# --------------------------------------------------------------------------- #

def _full_reasons(world, state, **kwargs):
    outcome = _precheck(world, state, **kwargs)
    assert outcome.decision is Decision.FULL, outcome.summary()
    return outcome.result.reasons


def test_new_setting_date_runs_full(world):
    state = _saved_state(world)
    world.report[0] = _row("S1", "DO-589200", date="2026-10-08", pattern="Ergon_Fuse")
    (reason,) = _full_reasons(world, state)
    assert reason.startswith("IPS setting-ID matches: 1 changed")
    assert "DO-589200" in reason


def test_new_asset_in_cubicle_runs_full(world):
    state = _saved_state(world)
    world.report.append(_row("S4", "GLFSSS-FB59-J01-SEF"))
    (reason,) = _full_reasons(world, state)
    assert "GLFSSS-FB59-J01" in reason


def test_new_register_device_runs_full(world):
    state = _saved_state(world)
    world.registers["fuse"]["999"] = [Reg("999", "DO-1", "E9")]
    (reason,) = _full_reasons(world, state)
    assert reason.startswith("Ergon asset register matches: 1 changed")


def test_parameter_edit_runs_full(world):
    state = _saved_state(world)
    world.params["S2"] = (40, 223, "2026-01-01")
    assert _full_reasons(world, state) == ("IPS parameters: 1 changed (S2)",)


def test_ct_change_runs_full(world):
    state = _saved_state(world)
    world.it = [It("S2", "300/5"), It("S3", "600/5")]
    assert _full_reasons(world, state) == ("IPS CT/VT details: 1 changed (S2)",)


def test_mapping_file_edit_runs_full(world):
    state = _saved_state(world)
    world.relay_map.write_text("FOLDER,ELEMENT,ATTR\nI>,Ipset,2\n")
    assert _full_reasons(world, state) == (
        "relay mapping files: 1 changed (SPAJ_Ergon)",
    )


def test_shared_file_edit_runs_full(world):
    state = _saved_state(world)
    world.type_mapping.write_text("IPS,Exclude\nP123,Yes\n")
    assert _full_reasons(world, state) == (
        "shared mapping files: 1 changed (type_mapping.csv)",
    )


def test_new_base_version_runs_full(world):
    state = _saved_state(world)
    (reason,) = _full_reasons(world, state, base=BASE.replace("v42", "v43"))
    assert "v42" in reason and "v43" in reason


def test_missing_audit_version_runs_full(world):
    (reason,) = _full_reasons(world, _saved_state(world), versions=("other",))
    assert AUDIT in reason


def test_ipstopf_code_change_runs_full(world):
    state = _saved_state(world)
    inputs = replace(INPUTS, ipstopf_code="j" * 64)
    assert _full_reasons(world, state, inputs=inputs) == ("code changed",)


def test_spa_only_change_runs_spa_only(world):
    state = _saved_state(world)
    inputs = replace(INPUTS, spa_data={"data/grid_results_ee.xlsx": "g2"})
    outcome = _precheck(world, state, inputs=inputs)
    assert outcome.decision is Decision.SPA_ONLY
    assert outcome.result.reasons == ("SPA data files: 1 changed "
                                      "(data/grid_results_ee.xlsx)",)


# --------------------------------------------------------------------------- #
# Failures and shortcuts
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("source, label", [
    ("index", "setting lookups"),
    ("registers", "asset registers"),
    ("ods", "ODS"),
    ("it", "CT/VT rows"),
])
def test_unavailable_source_runs_full(world, source, label):
    state = _saved_state(world)
    (reason,) = _full_reasons(world, state, sources=world.sources(fail=source))
    assert reason.startswith(f"precheck could not recompute {label}:")
    assert "unavailable" in reason


def test_unreplayable_lookup_runs_full(world):
    state = _saved_state(world)
    bad = dict(state.transfer.setting_lookups)
    bad[lookup_key("get_everything")] = "x"
    state = replace(state, transfer=replace(state.transfer, setting_lookups=bad))
    (reason,) = _full_reasons(world, state)
    assert "cannot be replayed" in reason


def test_no_state_runs_full_without_touching_sources(world):
    reasons = _full_reasons(world, None)
    assert reasons == ("no previous successful run recorded",)
    assert not world.calls


def test_force_runs_full_without_touching_sources(world):
    reasons = _full_reasons(world, _saved_state(world), force=True)
    assert reasons == ("forced full run",)
    assert not world.calls


def test_short_circuit_skips_ips_when_already_full(world):
    state = _saved_state(world)
    _full_reasons(world, state, base=BASE.replace("v42", "v43"),
                  short_circuit=True)
    assert not world.calls


def test_short_circuit_still_checks_ips_when_cheap_checks_pass(world):
    state = _saved_state(world)
    world.params["S2"] = (40, 223, "2026-01-01")
    assert _full_reasons(world, state, short_circuit=True) == (
        "IPS parameters: 1 changed (S2)",
    )


def test_diagnostic_mode_reports_every_reason(world):
    state = _saved_state(world)
    world.params["S2"] = (40, 223, "2026-01-01")
    reasons = _full_reasons(world, state, base=BASE.replace("v42", "v43"))
    assert len(reasons) == 2
    assert world.calls["ods"] == 1


def test_energex_state_never_builds_registers(world):
    state = _saved_state(world)
    state = replace(state, transfer=replace(state.transfer, skeleton_lookups={}))
    _precheck(world, state)
    assert world.calls["registers"] == 0


# --------------------------------------------------------------------------- #
# Small pieces
# --------------------------------------------------------------------------- #

def test_register_names_match_the_run():
    # add_relay_skeletons passes exactly these keyword names.
    assert SKELETON_REGISTERS == ("relay", "recloser", "fuse", "gas_switch")


def test_audit_version_present(world):
    state = _saved_state(world)
    assert audit_version_present(state, ["x", AUDIT])
    assert not audit_version_present(state, ["x"])
    assert not audit_version_present(None, [AUDIT])


def test_summary_includes_timings(world):
    outcome = _precheck(world, _saved_state(world))
    assert outcome.summary().startswith("SKIP: unchanged since run 20261012_020000")
    assert "[precheck " in outcome.summary()