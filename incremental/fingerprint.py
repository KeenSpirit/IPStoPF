"""
Run fingerprints for incremental (skip-if-unchanged) batch runs.

A fingerprint records everything that decided a project's result on its
last successful run: the master base version, the IPS records its devices
matched, the IPS parameter content, the CT/VT rows, the mapping files it
loaded, and the code and data of IPStoPF and SystemProtectionAssessment.
Before the next run, the batch layer recomputes the same fingerprint from
the same sources and calls :func:`decide`:

    SKIP      nothing changed - the project is not activated at all
    SPA_ONLY  the transfer inputs are unchanged but SPA's code or data
              changed - run the assessment only
    FULL      anything else

This module is pure: no PowerFactory, database or network access, and no
``logging_config`` (whose get_logger opens the network log on first use).
The runtime collectors (stage 2b) and the batch layer's state handling
(stage 2c) build on it. Offline tests: ``pytest incremental``.

Safety rule the callers must keep: a stored fingerprint must describe data
the same age as, or older than, what the run applied. A false re-run is
harmless; a false skip silently loses an update.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import (
    Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple,
)

logger = logging.getLogger(__name__)

# Bump when the fingerprint layout or any digest recipe changes. A stored
# state with a different version is ignored, so every project runs once.
SCHEMA_VERSION = 1

# A project runs regardless once its last successful run is this old.
DEFAULT_MAX_AGE_DAYS = 28

# Changed keys named in a decision reason before "and N more".
MAX_EXAMPLES = 5

# hash_tree defaults: folders that never hold pipeline code or inputs.
DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git", ".idea", ".vscode", "__pycache__", ".pytest_cache",
    "results_log", "dashboard_data", "run_state", "mapping_files",
})


class Decision(str, Enum):
    """What the batch layer should do with a project this run."""
    SKIP = "SKIP"
    SPA_ONLY = "SPA_ONLY"
    FULL = "FULL"


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #

def _canonical(obj: Any) -> str:
    """Stable JSON text: sorted keys, no whitespace, str() for dates etc."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_value(obj: Any) -> str:
    """Digest of any JSON-serialisable value (dates are taken as str())."""
    return _sha256(_canonical(obj))


def digest_records(records: Iterable[Mapping[str, Any]]) -> str:
    """
    Order-independent digest of a set of records (e.g. IPS report rows).

    Each record is reduced to canonical JSON and the texts are sorted, so
    the digest depends on the rows' content only, not on the order the
    report or index returned them in. Duplicate rows still count.
    """
    texts = sorted(_canonical(dict(r)) for r in records)
    return _sha256("\n".join(texts))


def lookup_key(method: str, *args: Any) -> str:
    """
    Encode one recorded lookup (method name plus arguments) as a string key.

    Example: ``lookup_key("get_by_switch_name", "CB01", "BHL")`` gives
    ``'["get_by_switch_name","CB01","BHL"]'``. The precheck decodes it with
    :func:`parse_lookup_key` and repeats the same call on a fresh index.
    """
    return json.dumps([method, *args], separators=(",", ":"), default=str)


def parse_lookup_key(key: str) -> Optional[Tuple[str, Tuple[Any, ...]]]:
    """Decode a :func:`lookup_key` string; None if it is not one."""
    try:
        decoded = json.loads(key)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, list) or not decoded:
        return None
    if not isinstance(decoded[0], str):
        return None
    return decoded[0], tuple(decoded[1:])


def hash_file(path: Path) -> Optional[str]:
    """
    SHA-256 of a file's bytes, or None if it is missing or unreadable.

    None compares unequal to any digest, so a mapping file that disappears
    between runs forces a full run rather than being silently ignored.
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                h.update(chunk)
    except OSError as exc:
        logger.warning(f"Fingerprint: cannot read {path}: {exc}")
        return None
    return h.hexdigest()


def hash_files(paths: Mapping[str, Path]) -> Dict[str, Optional[str]]:
    """Hash each file under its label: {label: digest or None}."""
    return {label: hash_file(Path(p)) for label, p in paths.items()}


def hash_tree(
    root: Path,
    patterns: Sequence[str] = ("*.py",),
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
    exclude_file_prefixes: Sequence[str] = ("test_",),
) -> Optional[str]:
    """
    One digest over every matching file under ``root``.

    Each file contributes its path relative to ``root`` and its content
    hash, so editing, adding, removing or renaming a file all change the
    digest. Test modules (``test_*.py``) and the folders in
    ``exclude_dirs`` are skipped: they do not change what a run produces.
    Returns None if ``root`` is not a folder or any matched file cannot be
    read, which forces a full run.
    """
    root = Path(root)
    if not root.is_dir():
        logger.warning(f"Fingerprint: code folder not found: {root}")
        return None
    excluded = set(exclude_dirs)
    entries: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in excluded)
        for name in sorted(filenames):
            if name.startswith(tuple(exclude_file_prefixes)):
                continue
            if not any(Path(name).match(p) for p in patterns):
                continue
            path = Path(dirpath) / name
            digest = hash_file(path)
            if digest is None:
                return None
            entries.append(f"{path.relative_to(root).as_posix()}:{digest}")
    return _sha256("\n".join(sorted(entries)))


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #

# Component name -> label used in decision reasons.
_LABELS = {
    "base_version": "master base version",
    "setting_lookups": "IPS setting-ID matches",
    "skeleton_lookups": "Ergon asset register matches",
    "ods_params": "IPS parameters",
    "it_rows": "IPS CT/VT details",
    "mapping_files": "relay mapping files",
    "shared_files": "shared mapping files",
    "code": "code",
    "data_files": "data files",
}


@dataclass(frozen=True)
class TransferFingerprint:
    """
    Everything that decides the IPStoPF transfer for one project.

    Keyed components map a key to a digest so a change can be reported
    by key (which setting IDs, which mapping file):

        setting_lookups   lookup_key(...) -> digest of the records returned
        skeleton_lookups  lookup_key(...) -> digest (Ergon; empty for Energex)
        ods_params        relay setting ID -> digest of its ODS aggregate
        it_rows           relay setting ID -> digest of its CT/VT rows
        mapping_files     relay map file name -> SHA-256 (None if missing)
        shared_files      label (type_mapping, ...) -> SHA-256 (None if missing)
    """
    base_version: Optional[str]
    setting_lookups: Dict[str, str] = field(default_factory=dict)
    skeleton_lookups: Dict[str, str] = field(default_factory=dict)
    ods_params: Dict[str, str] = field(default_factory=dict)
    it_rows: Dict[str, str] = field(default_factory=dict)
    mapping_files: Dict[str, Optional[str]] = field(default_factory=dict)
    shared_files: Dict[str, Optional[str]] = field(default_factory=dict)
    code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TransferFingerprint":
        return cls(**_known_fields(cls, data))


@dataclass(frozen=True)
class AssessmentFingerprint:
    """
    What SPA adds on top of the transfer: its code and its input data
    (e.g. grid_results_egx.xlsx / grid_results_ee.xlsx).
    """
    code: Optional[str] = None
    data_files: Dict[str, Optional[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AssessmentFingerprint":
        return cls(**_known_fields(cls, data))


@dataclass(frozen=True)
class ProjectState:
    """
    The record of a project's last successful run, saved by the batch
    layer only after both the transfer and the assessment succeed.

    ``audit_version`` is the name of the version that run created
    (``YYYYMMDD IPS Import``). If it is no longer in the derived project,
    the copy was re-derived or reset and has lost that import.
    """
    project: str
    region: str
    run_id: str
    finished: str                      # ISO 8601 with UTC offset
    audit_version: Optional[str]
    transfer: TransferFingerprint
    assessment: AssessmentFingerprint
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "region": self.region,
            "run_id": self.run_id,
            "finished": self.finished,
            "audit_version": self.audit_version,
            "transfer": self.transfer.to_dict(),
            "assessment": self.assessment.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProjectState":
        return cls(
            project=data["project"],
            region=data["region"],
            run_id=data["run_id"],
            finished=data["finished"],
            audit_version=data.get("audit_version"),
            transfer=TransferFingerprint.from_dict(data["transfer"]),
            assessment=AssessmentFingerprint.from_dict(data["assessment"]),
            schema_version=data.get("schema_version", 0),
        )


def _known_fields(cls, data: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep only the keys the dataclass declares (tolerates extras)."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

def _examples(keys: Sequence[str]) -> str:
    shown = ", ".join(str(k) for k in keys[:MAX_EXAMPLES])
    more = len(keys) - MAX_EXAMPLES
    return f"{shown}, and {more} more" if more > 0 else shown


def _diff_keyed(label: str, old: Mapping, new: Mapping) -> Optional[str]:
    """One reason line for a keyed component, or None if identical."""
    added = sorted(k for k in new if k not in old)
    removed = sorted(k for k in old if k not in new)
    changed = sorted(k for k in new if k in old and new[k] != old[k])
    if not (added or removed or changed):
        return None
    parts = []
    if changed:
        parts.append(f"{len(changed)} changed ({_examples(changed)})")
    if added:
        parts.append(f"{len(added)} added ({_examples(added)})")
    if removed:
        parts.append(f"{len(removed)} removed ({_examples(removed)})")
    return f"{label}: " + "; ".join(parts)


def _diff_scalar(label: str, old: Any, new: Any) -> Optional[str]:
    if old == new:
        return None
    if old is None or new is None:
        return f"{label}: {old!r} -> {new!r}"
    # Digests are long; names (base version) are short. Show what helps.
    if isinstance(old, str) and len(old) == 64 and len(str(new)) == 64:
        return f"{label} changed"
    return f"{label}: {old} -> {new}"


def diff_fingerprints(old, new) -> List[str]:
    """
    Reasons the two fingerprints differ, one line per changed component.
    Both arguments must be the same fingerprint class. Empty if identical.
    """
    if type(old) is not type(new):
        raise TypeError(
            f"Cannot compare {type(old).__name__} with {type(new).__name__}"
        )
    reasons = []
    for f in fields(old):
        label = _LABELS.get(f.name, f.name)
        a, b = getattr(old, f.name), getattr(new, f.name)
        if isinstance(a, Mapping) or isinstance(b, Mapping):
            reason = _diff_keyed(label, a or {}, b or {})
        else:
            reason = _diff_scalar(label, a, b)
        if reason:
            reasons.append(reason)
    return reasons


@dataclass(frozen=True)
class DecisionResult:
    """The precheck outcome plus the reasons, for the run log."""
    decision: Decision
    reasons: Tuple[str, ...]

    def summary(self) -> str:
        return f"{self.decision.value}: " + "; ".join(self.reasons)


def decide(
    previous: Optional[ProjectState],
    transfer: TransferFingerprint,
    assessment: AssessmentFingerprint,
    *,
    audit_version_present: bool,
    now: datetime,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    force: bool = False,
) -> DecisionResult:
    """
    Decide SKIP / SPA_ONLY / FULL for one project.

    Args:
        previous: The stored state of the last successful run, or None.
        transfer: The transfer fingerprint recomputed now, by replaying
            ``previous``'s lookups and re-hashing its files.
        assessment: The assessment fingerprint recomputed now.
        audit_version_present: Whether ``previous.audit_version`` is still
            a version of the derived project.
        now: Current time, timezone-aware (injected for testing).
        max_age_days: Run regardless once the last run is this old.
        force: Run everything (operator override).

    The checks run cheapest and most decisive first. When the transfer
    changed, assessment differences are listed too, for information.
    """
    if force:
        return DecisionResult(Decision.FULL, ("forced full run",))
    if previous is None:
        return DecisionResult(
            Decision.FULL, ("no previous successful run recorded",)
        )
    if previous.schema_version != SCHEMA_VERSION:
        return DecisionResult(Decision.FULL, (
            f"stored state is schema {previous.schema_version}, "
            f"current is {SCHEMA_VERSION}",
        ))

    try:
        finished = datetime.fromisoformat(previous.finished)
        age = now - finished
    except (TypeError, ValueError):
        return DecisionResult(Decision.FULL, (
            f"stored finish time {previous.finished!r} is unreadable",
        ))
    if age > timedelta(days=max_age_days):
        return DecisionResult(Decision.FULL, (
            f"last successful run ({previous.run_id}) is {age.days} days "
            f"old; limit is {max_age_days}",
        ))

    if previous.audit_version is None:
        return DecisionResult(Decision.FULL, (
            "previous run created no audit version, so the derived copy "
            "cannot be confirmed unchanged",
        ))
    if not audit_version_present:
        return DecisionResult(Decision.FULL, (
            f"audit version '{previous.audit_version}' is missing: the "
            f"derived copy was re-created or reset",
        ))

    transfer_reasons = diff_fingerprints(previous.transfer, transfer)
    assessment_reasons = diff_fingerprints(previous.assessment, assessment)
    if transfer_reasons:
        return DecisionResult(
            Decision.FULL,
            tuple(transfer_reasons)
            + tuple(f"SPA {r}" for r in assessment_reasons),
        )
    if assessment_reasons:
        return DecisionResult(
            Decision.SPA_ONLY, tuple(f"SPA {r}" for r in assessment_reasons)
        )
    return DecisionResult(
        Decision.SKIP, (f"unchanged since run {previous.run_id}",)
    )


# --------------------------------------------------------------------------- #
# State files
# --------------------------------------------------------------------------- #

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe_name(name: str) -> str:
    """Windows-safe file or folder name (PF names may contain '/')."""
    cleaned = _UNSAFE.sub("_", name).strip(" .")
    return cleaned or "_"


def state_path(state_dir: Path, region: str, project: str) -> Path:
    """``<state_dir>/<region>/<project>.json``, with unsafe characters replaced."""
    return Path(state_dir) / _safe_name(region) / f"{_safe_name(project)}.json"


def save_state(path: Path, state: ProjectState) -> None:
    """
    Write the state atomically: a temp file in the same folder, then
    os.replace. A crash mid-write leaves the previous state intact, never
    a half-written file. Raises OSError on failure, so the caller can log
    it (a missing state only costs a full run next time).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle, sort_keys=True, indent=1,
                      default=str)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_state(path: Path) -> Optional[ProjectState]:
    """
    Read a saved state; None if it is missing, unreadable, malformed or
    from another schema version. Every None means "run the project".
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        state = ProjectState.from_dict(data)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning(f"Fingerprint: ignoring unreadable state {path}: {exc}")
        return None
    if state.schema_version != SCHEMA_VERSION:
        logger.info(
            f"Fingerprint: state {path} is schema {state.schema_version}, "
            f"current is {SCHEMA_VERSION}; ignored"
        )
        return None
    return state