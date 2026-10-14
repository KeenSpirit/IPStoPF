"""
Run recorder: collects a project's fingerprint while IPStoPF runs.

The batch layer creates a RunRecorder per project and passes it to
``main.main(app, True, recorder=rec)``. For the duration of that call the
recorder is "active", and hooks in the data paths report to it:

    SettingIndex lookups      note_lookup()       ips_data/setting_index.py
    Ergon asset registers     note_skeleton()     ips_data/add_relay_skeletons.py
    ODS parameter aggregates  note_ods()          ips_data/query_database.py
    CT/VT report rows         note_it_rows()      ips_data/query_database.py
    Mapping and shared files  file_loaded() /     update_powerfactory/mapping_file.py,
                              file_used()         ips_data/cb_mapping.py

With no active recorder (interactive runs) every hook is a cheap no-op,
except file_loaded(), which always hashes the file it is told about: the
loaders cache file contents for the whole process, so the hash must be
taken when the file is actually read for a later project to report it.

Recording must never break a transfer. Every hook catches its own errors,
logs a warning and marks the recorder incomplete; the batch layer saves
no state for an incomplete recorder, so the project simply runs in full
next time.

Pure module: no PowerFactory, database or network access. The digest
helpers here (records_digest, ods_digests, ...) are the single definition
used both when recording and when the precheck recomputes a fingerprint.
"""

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple,
)

from incremental.fingerprint import (
    TransferFingerprint,
    digest_records,
    digest_value,
    hash_file,
    lookup_key,
)

logger = logging.getLogger(__name__)

# Keyed components hold thousands of entries per project (one per lookup
# or setting ID). 16 hex characters (64 bits) is ample for change
# detection and halves the state file size.
KEYED_DIGEST_LENGTH = 16

# file_loaded() kinds.
RELAY_MAP = "relay_map"
SHARED_FILE = "shared"

SKELETON_LOOKUP = "skeleton"


def _short(digest: str) -> str:
    return digest[:KEYED_DIGEST_LENGTH]


def _as_mapping(obj: Any) -> Mapping[str, Any]:
    """A record as a dict: SettingRecord, report namedtuple, or mapping."""
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    as_dict = getattr(obj, "_asdict", None)
    if callable(as_dict):
        return as_dict()
    return dict(obj)


def records_digest(records: Iterable[Any]) -> str:
    """Short order-independent digest of a lookup's returned records."""
    return _short(digest_records(_as_mapping(r) for r in records))


def skeleton_digest(groups: Mapping[str, Iterable[Any]]) -> str:
    """Short digest of one asset ID's rows across the asset registers."""
    return _short(digest_value({
        name: sorted(digest_records([_as_mapping(r)]) for r in rows)
        for name, rows in groups.items()
    }))


def _norm_number(value: Any) -> Any:
    """Oracle aggregates may arrive as int, float or Decimal: normalise."""
    if value is None or isinstance(value, (int, str)):
        return value
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return str(value)
    return int(as_float) if as_float.is_integer() else as_float


def ods_digests(
    requested_ids: Iterable[str],
    rows: Iterable[Sequence[Any]],
    mode: str,
) -> Dict[str, str]:
    """
    One short digest per requested setting ID from the ODS aggregate rows.

    Args:
        requested_ids: The setting IDs queried.
        rows: (relaysettingid, n_params, content_hash, last_import) tuples.
        mode: The query mode ("content" or "dates"); part of every digest,
            so changing the mode forces one full run of every project.

    A requested ID with no rows (all parameters NULL) gets the "no rows"
    digest, so parameters appearing later count as a change.
    """
    found: Dict[str, Tuple[Any, ...]] = {}
    for sid, n_params, content_hash, last_import in rows:
        found[str(sid)] = (
            _norm_number(n_params),
            _norm_number(content_hash),
            None if last_import is None else str(last_import),
        )
    return {
        str(sid): _short(digest_value([mode, *found.get(str(sid), (None,))]))
        for sid in requested_ids
    }


def it_digests(
    setting_ids: Iterable[str], rows: Iterable[Any]
) -> Dict[str, str]:
    """
    One short digest per setting ID of its CT/VT report rows. An ID with
    no rows gets the "no rows" digest, so a CT added later is a change.
    """
    by_id: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        mapping = _as_mapping(row)
        by_id.setdefault(str(mapping.get("relaysettingid")), []).append(mapping)
    return {
        str(sid): _short(digest_records(by_id.get(str(sid), [])))
        for sid in setting_ids
    }


class RunRecorder:
    """
    Fingerprint collected during one project's IPStoPF run.

    ``is_complete`` is True only when the run reached its normal end
    (mark_completed) and no hook reported a problem. The batch layer must
    save state only for a complete recorder.
    """

    def __init__(self, project: str = "") -> None:
        self.project = project
        self.setting_lookups: Dict[str, str] = {}
        self.skeleton_lookups: Dict[str, str] = {}
        self.ods_params: Dict[str, str] = {}
        self.it_rows: Dict[str, str] = {}
        self.mapping_files: Dict[str, Optional[str]] = {}
        self.shared_files: Dict[str, Optional[str]] = {}
        self.problems: List[str] = []
        self.completed = False

    # -- hooks ------------------------------------------------------------- #

    def note_lookup(self, method: str, args: Sequence[Any], records) -> None:
        self.setting_lookups[lookup_key(method, *args)] = records_digest(records)

    def note_skeleton(self, asset_id: str, groups: Mapping[str, Iterable]) -> None:
        self.skeleton_lookups[lookup_key(SKELETON_LOOKUP, asset_id)] = (
            skeleton_digest(groups)
        )

    def note_ods(self, digests: Mapping[str, str]) -> None:
        self.ods_params.update(digests)

    def note_it_rows(self, setting_ids: Iterable[str], rows: Iterable) -> None:
        self.it_rows.update(it_digests(setting_ids, rows))

    def note_file(self, kind: str, label: str, digest: Optional[str]) -> None:
        target = self.mapping_files if kind == RELAY_MAP else self.shared_files
        target[label] = digest

    def mark_incomplete(self, reason: str) -> None:
        if reason not in self.problems:
            self.problems.append(reason)

    def mark_completed(self) -> None:
        self.completed = True

    # -- results ----------------------------------------------------------- #

    @property
    def is_complete(self) -> bool:
        return self.completed and not self.problems

    def transfer_fingerprint(
        self, base_version: Optional[str], code: Optional[str]
    ) -> TransferFingerprint:
        """
        The transfer fingerprint. ``base_version`` and ``code`` come from
        the batch layer, which holds the project object and imports the
        code (the code that runs is fixed when it is imported).
        """
        return TransferFingerprint(
            base_version=base_version,
            setting_lookups=dict(self.setting_lookups),
            skeleton_lookups=dict(self.skeleton_lookups),
            ods_params=dict(self.ods_params),
            it_rows=dict(self.it_rows),
            mapping_files=dict(self.mapping_files),
            shared_files=dict(self.shared_files),
            code=code,
        )

    def summary(self) -> str:
        """One log line: what was recorded and whether it is usable."""
        no_rows = _short(digest_records([]))
        with_it = sum(1 for d in self.it_rows.values() if d != no_rows)
        counts = (
            f"{len(self.setting_lookups)} setting-ID lookups, "
            f"{len(self.skeleton_lookups)} asset-register lookups, "
            f"{len(self.ods_params)} setting IDs with ODS aggregates, "
            f"{with_it} of {len(self.it_rows)} with CT/VT rows, "
            f"{len(self.mapping_files)} relay map(s), "
            f"{len(self.shared_files)} shared file(s)"
        )
        if self.is_complete:
            state = "complete"
        elif not self.completed:
            state = "INCOMPLETE: transfer did not reach its normal end"
        else:
            state = "INCOMPLETE: " + "; ".join(self.problems)
        name = f"{self.project}: " if self.project else ""
        return f"Fingerprint {name}{counts}; {state}"


# --------------------------------------------------------------------------- #
# Active recorder and module-level hooks
# --------------------------------------------------------------------------- #

_active: Optional[RunRecorder] = None

# Hash of each loaded file at the moment it was read, for the process
# lifetime (the loaders cache file contents just as long).
_loaded_digests: Dict[Tuple[str, str], Optional[str]] = {}


def active() -> Optional[RunRecorder]:
    """The recorder for the current transfer, or None."""
    return _active


@contextmanager
def recording(recorder: Optional[RunRecorder]) -> Iterator[Optional[RunRecorder]]:
    """Make ``recorder`` active for the block (None records nothing)."""
    global _active
    previous = _active
    _active = recorder
    try:
        yield recorder
    finally:
        _active = previous


def _guarded(hook_name: str, func, *args) -> None:
    """Run a hook on the active recorder; never let it raise."""
    rec = _active
    if rec is None:
        return
    try:
        func(rec, *args)
    except Exception as exc:  # noqa: BLE001 - recording must not break a run
        reason = f"{hook_name} failed: {exc!r}"
        if reason not in rec.problems:
            logger.warning(f"Fingerprint: {reason}")
        rec.mark_incomplete(reason)


def note_lookup(method: str, args: Sequence[Any], records) -> None:
    """SettingIndex hook: one lookup and the records it returned."""
    _guarded("setting lookup", RunRecorder.note_lookup, method, args, records)


def note_skeleton(asset_id: str, **groups: Mapping[str, Iterable]) -> None:
    """
    Asset-register hook: the rows held for ``asset_id`` in each register.
    Pass the dictionaries themselves (relay=relay_dict, ...); rows are read
    with .get(), so a defaultdict gains no keys.
    """
    def _note(rec: RunRecorder) -> None:
        rec.note_skeleton(
            asset_id, {name: d.get(asset_id, []) for name, d in groups.items()}
        )
    _guarded("asset-register lookup", _note)


def note_ods(digests: Mapping[str, str]) -> None:
    """ODS hook: per-setting-ID digests from ods_digests()."""
    _guarded("ODS aggregate", RunRecorder.note_ods, digests)


def note_it_rows(setting_ids: Iterable[str], rows: Iterable) -> None:
    """IT-report hook: the CT/VT rows matched for this run's setting IDs."""
    _guarded("CT/VT rows", RunRecorder.note_it_rows, list(setting_ids), rows)


def mark_incomplete(reason: str) -> None:
    """Report that part of the fingerprint could not be recorded."""
    _guarded("mark incomplete", RunRecorder.mark_incomplete, reason)


def mark_completed() -> None:
    """Call where the transfer reaches its normal end."""
    _guarded("mark completed", RunRecorder.mark_completed)


def file_loaded(kind: str, label: str, path: Path) -> None:
    """
    Loader hook, called on a cache miss just before the file is read.
    Always hashes the file (whether or not a recorder is active) and
    reports its use to the active recorder.
    """
    try:
        _loaded_digests[(kind, label)] = hash_file(Path(path))
    except Exception as exc:  # noqa: BLE001 - recording must not break a run
        _loaded_digests[(kind, label)] = None
        logger.warning(f"Fingerprint: could not hash {path}: {exc!r}")
    file_used(kind, label)


def file_used(kind: str, label: str) -> None:
    """
    Loader hook, called on a cache hit: report the file's hash as it was
    when the process read it. A file never seen by file_loaded() reports
    None, which forces a full run rather than a wrong skip.
    """
    _guarded(
        "file use", RunRecorder.note_file,
        kind, label, _loaded_digests.get((kind, label)),
    )