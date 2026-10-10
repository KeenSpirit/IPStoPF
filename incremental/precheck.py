"""
Incremental-run precheck: recompute a project's fingerprint from its saved
state and decide SKIP / SPA_ONLY / FULL before the project is activated.

How each part of the transfer fingerprint is recomputed:

    setting_lookups   replay every recorded SettingIndex call on this run's
                      index, so a new setting ID, a new asset in a recorded
                      cubicle, or a changed date shows up as a changed digest
    skeleton_lookups  replay every recorded asset ID against this run's
                      Ellipse/GISEP registers (Ergon)
    ods_params        re-query the parameter aggregates for the stored IDs
    it_rows           re-read the CT/VT rows for the stored IDs
    mapping_files,    re-hash the stored files on disk
    shared_files
    base_version,     supplied by the batch layer (project object, import-time
    code              code hash); the assessment part comes from the same
                      import-time snapshot

The replay only reproduces what the run would see because the derived copy
is unchanged since its last run: decide() refuses to skip unless the base
version and the audit version say so.

Data access is passed in (PrecheckSources) and fetched lazily, so an
Energex project never builds the Ergon registers and a project that is
going to run anyway costs nothing extra when short_circuit is set. This
module itself is pure; precheck_sources.py builds the real sources.
Offline tests: test_precheck.py.
"""

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import (
    Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple,
)

from incremental.fingerprint import (
    DEFAULT_MAX_AGE_DAYS,
    AssessmentFingerprint,
    Decision,
    DecisionResult,
    ProjectState,
    TransferFingerprint,
    decide,
    hash_file,
    parse_lookup_key,
)
from incremental.recorder import (
    SKELETON_LOOKUP,
    it_digests,
    records_digest,
    recording,
    skeleton_digest,
)
from incremental.run_state import InputSnapshot

logger = logging.getLogger(__name__)

# The SettingIndex methods the run records (setting_index.py). A stored key
# naming anything else is not replayed: the precheck reports it instead.
REPLAYABLE_METHODS = frozenset({
    "get_by_asset_exact",
    "get_by_asset_contains",
    "get_by_switch_name",
})

# Register names exactly as add_relay_skeletons passes them to
# note_skeleton(); the digest covers the names, so they must match.
SKELETON_REGISTERS = ("relay", "recloser", "fuse", "gas_switch")


@dataclass(frozen=True)
class PrecheckSources:
    """
    Data access for the precheck. Every callable is called at most once
    per project and only if the saved state needs it; any of them may
    raise, which makes the precheck return FULL with the reason.

        setting_index()       the region's SettingIndex (the run's cached one)
        skeleton_registers()  {"relay": dict, "recloser": ..., "fuse": ...,
                              "gas_switch": ...} (Ergon)
        it_rows(ids)          CT/VT report rows for those setting IDs, from
                              the same function the run uses
        ods_digests(ids)      {setting ID: digest} from the ODS aggregate
        relay_map_path(name)  path of a relay mapping file
        shared_file_paths     {label: path} for type_mapping.csv etc.
    """
    setting_index: Callable[[], Any]
    skeleton_registers: Callable[[], Mapping[str, Mapping[str, Sequence]]]
    it_rows: Callable[[List[str]], Iterable[Any]]
    ods_digests: Callable[[List[str]], Dict[str, str]]
    relay_map_path: Callable[[str], Path]
    shared_file_paths: Mapping[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class PrecheckOutcome:
    """The decision plus where the precheck spent its time."""
    result: DecisionResult
    timings: Dict[str, float] = field(default_factory=dict)

    @property
    def decision(self) -> Decision:
        return self.result.decision

    def summary(self) -> str:
        total = sum(self.timings.values())
        parts = ", ".join(
            f"{name} {secs:.1f} s" for name, secs in self.timings.items()
            if secs >= 0.05
        )
        timing = f"precheck {total:.1f} s" + (f" ({parts})" if parts else "")
        return f"{self.result.summary()} [{timing}]"


class _Unrecomputable(Exception):
    """A component could not be recomputed; the project must run."""


def audit_version_present(
    state: Optional[ProjectState], version_names: Iterable[str]
) -> bool:
    """Whether the state's audit version is still among the project's versions."""
    if state is None or not state.audit_version:
        return False
    return state.audit_version in set(version_names)


def _row_setting_id(row: Any) -> Optional[str]:
    value = getattr(row, "relaysettingid", None)
    if value is None and isinstance(row, Mapping):
        value = row.get("relaysettingid")
    return None if value is None else str(value)


def _replay_lookups(index: Any, keys: Iterable[str]) -> Dict[str, str]:
    digests: Dict[str, str] = {}
    for key in keys:
        parsed = parse_lookup_key(key)
        if parsed is None or parsed[0] not in REPLAYABLE_METHODS:
            raise _Unrecomputable(f"setting lookup {key!r} cannot be replayed")
        method, args = parsed
        digests[key] = records_digest(getattr(index, method)(*args))
    return digests


def _replay_skeletons(
    registers: Mapping[str, Mapping[str, Sequence]], keys: Iterable[str]
) -> Dict[str, str]:
    missing = [name for name in SKELETON_REGISTERS if name not in registers]
    if missing:
        raise _Unrecomputable(f"asset registers missing: {', '.join(missing)}")
    digests: Dict[str, str] = {}
    for key in keys:
        parsed = parse_lookup_key(key)
        if parsed is None or parsed[0] != SKELETON_LOOKUP or len(parsed[1]) != 1:
            raise _Unrecomputable(f"asset-register lookup {key!r} cannot be replayed")
        asset_id = parsed[1][0]
        digests[key] = skeleton_digest({
            name: registers[name].get(asset_id, []) for name in SKELETON_REGISTERS
        })
    return digests


def recompute_transfer(
    previous: TransferFingerprint,
    sources: PrecheckSources,
    *,
    base_version: Optional[str],
    code: Optional[str],
    timings: Optional[Dict[str, float]] = None,
) -> TransferFingerprint:
    """
    This run's transfer fingerprint over the same keys as ``previous``.
    Raises _Unrecomputable (with the component named) if any part cannot
    be recomputed.
    """
    timings = {} if timings is None else timings

    def timed(name: str, func: Callable[[], Any]) -> Any:
        start = time.perf_counter()
        try:
            return func()
        except _Unrecomputable:
            raise
        except Exception as exc:  # noqa: BLE001 - becomes a FULL decision
            raise _Unrecomputable(f"{name}: {exc!r}") from exc
        finally:
            timings[name] = timings.get(name, 0.0) + time.perf_counter() - start

    # No recorder may be active while replaying: the index hooks would
    # otherwise record the precheck's calls as if they were the run's.
    with recording(None):
        setting_lookups = {}
        if previous.setting_lookups:
            setting_lookups = timed("setting lookups", lambda: _replay_lookups(
                sources.setting_index(), previous.setting_lookups
            ))
        skeleton_lookups = {}
        if previous.skeleton_lookups:
            skeleton_lookups = timed("asset registers", lambda: _replay_skeletons(
                sources.skeleton_registers(), previous.skeleton_lookups
            ))

    ods_params = {}
    if previous.ods_params:
        ods_params = timed(
            "ODS", lambda: sources.ods_digests(list(previous.ods_params))
        )

    it_rows = {}
    if previous.it_rows:
        def _it() -> Dict[str, str]:
            ids = list(previous.it_rows)
            wanted = set(ids)
            rows = [r for r in sources.it_rows(ids)
                    if _row_setting_id(r) in wanted]
            return it_digests(ids, rows)
        it_rows = timed("CT/VT rows", _it)

    def _files() -> Tuple[Dict[str, Optional[str]], Dict[str, Optional[str]]]:
        mapping = {
            name: hash_file(sources.relay_map_path(name))
            for name in previous.mapping_files
        }
        shared = {
            label: (hash_file(sources.shared_file_paths[label])
                    if label in sources.shared_file_paths else None)
            for label in previous.shared_files
        }
        return mapping, shared
    mapping_files, shared_files = timed("mapping files", _files)

    return TransferFingerprint(
        base_version=base_version,
        setting_lookups=setting_lookups,
        skeleton_lookups=skeleton_lookups,
        ods_params=ods_params,
        it_rows=it_rows,
        mapping_files=mapping_files,
        shared_files=shared_files,
        code=code,
    )


def run_precheck(
    previous: Optional[ProjectState],
    sources: PrecheckSources,
    *,
    base_version: Optional[str],
    version_names: Iterable[str],
    inputs: InputSnapshot,
    now: datetime,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    force: bool = False,
    short_circuit: bool = False,
) -> PrecheckOutcome:
    """
    Decide SKIP / SPA_ONLY / FULL for one project. Never raises.

    Args:
        previous: The project's saved state (load_state), or None.
        sources: Data access (precheck_sources.build_sources).
        base_version: The derived project's current der_baseversion full name.
        version_names: loc_name of every version the project has now.
        inputs: The batch process's import-time code and data snapshot.
        now: Current time, timezone-aware.
        max_age_days, force: As for decide().
        short_circuit: Stop after the cheap checks (age, audit version, base
            version, code, SPA data) when they already give FULL, without
            touching IPS. Leave False while the precheck is diagnostic, so
            every component is exercised and every reason reported.
    """
    timings: Dict[str, float] = {}
    assessment = AssessmentFingerprint(
        code=inputs.spa_code, data_files=dict(inputs.spa_data)
    )
    try:
        present = audit_version_present(previous, version_names)
        if previous is None or force:
            result = decide(
                None, TransferFingerprint(base_version), assessment,
                audit_version_present=present, now=now,
                max_age_days=max_age_days, force=force,
            )
            return PrecheckOutcome(result, timings)

        if short_circuit:
            # Cheap pass: the IPS components are taken as unchanged.
            cheap = replace(
                previous.transfer, base_version=base_version,
                code=inputs.ipstopf_code,
            )
            result = decide(
                previous, cheap, assessment, audit_version_present=present,
                now=now, max_age_days=max_age_days, force=force,
            )
            if result.decision is Decision.FULL:
                return PrecheckOutcome(result, timings)

        transfer = recompute_transfer(
            previous.transfer, sources, base_version=base_version,
            code=inputs.ipstopf_code, timings=timings,
        )
        result = decide(
            previous, transfer, assessment, audit_version_present=present,
            now=now, max_age_days=max_age_days, force=force,
        )
        return PrecheckOutcome(result, timings)
    except _Unrecomputable as exc:
        return PrecheckOutcome(
            DecisionResult(Decision.FULL, (f"precheck could not recompute {exc}",)),
            timings,
        )
    except Exception as exc:  # noqa: BLE001 - the run must go ahead
        logger.warning("Precheck failed", exc_info=True)
        return PrecheckOutcome(
            DecisionResult(Decision.FULL, (f"precheck failed: {exc!r}",)),
            timings,
        )