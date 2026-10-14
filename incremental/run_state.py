"""
Assembling and saving a project's run state after a successful run.

The batch layer (ProtectionBatchRunner's batch_relay_update) calls:

    snapshot_inputs(...)   once, right after it imports IPStoPF and SPA:
                           hashes both code trees and SPA's data files.
                           The code a process runs is fixed at import
                           (SPA's reload() calls also run at import), and
                           a hash taken before use can only be older than
                           what was used - the safe direction.
    build_state(...)       after a project's transfer and assessment both
                           succeed: returns the ProjectState to save, or
                           None and the reason it cannot be saved.
    invalidate_state(...)  when a run fails after the project was
                           activated: the model may be half-written, so
                           the saved state must not survive.

Pure module: no PowerFactory, database or network access beyond reading
the files it is pointed at. Offline tests: test_run_state.py.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from incremental.fingerprint import (
    AssessmentFingerprint,
    ProjectState,
    hash_file,
    hash_tree,
)
from incremental.recorder import RunRecorder

logger = logging.getLogger(__name__)

# Folder holding SPA's source-impedance workbooks, relative to the SPA
# repository root (load_source_z_data.SOURCE_Z_DIR_NAME).
SPA_DATA_DIR_NAME = "data"

# Never inputs: Excel lock files and Windows folder metadata.
_SKIP_PREFIXES = ("~$",)
_SKIP_NAMES = frozenset({"thumbs.db", "desktop.ini"})


def region_for_folder(folder_name: str) -> str:
    """
    IPS region of a derived project from its folder ("SEQ Models",
    "Northern", "Southern"), matching utils.pf_utils.determine_region:
    "SEQ Models" is Energex, every other distribution folder is Ergon.
    Taken from the folder so the precheck never needs determine_region
    (41-46 s per project).
    """
    return "Energex" if folder_name.strip() == "SEQ Models" else "Ergon"


def hash_folder_files(folder: Path) -> Dict[str, Optional[str]]:
    """
    {file name: SHA-256} for every file directly in ``folder``, skipping
    Excel lock files (~$...) and Windows metadata. A missing folder gives
    {} (with a warning); its files appearing later count as a change.
    """
    folder = Path(folder)
    if not folder.is_dir():
        logger.warning(f"Fingerprint: data folder not found: {folder}")
        return {}
    digests: Dict[str, Optional[str]] = {}
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith(_SKIP_PREFIXES):
            continue
        if path.name.lower() in _SKIP_NAMES:
            continue
        digests[path.name] = hash_file(path)
    return digests


@dataclass(frozen=True)
class InputSnapshot:
    """Code and data hashes for one batch process."""
    ipstopf_code: Optional[str]
    spa_code: Optional[str]
    spa_data: Dict[str, Optional[str]] = field(default_factory=dict)

    def summary(self) -> str:
        def short(d: Optional[str]) -> str:
            return d[:12] if d else "UNREADABLE"
        unreadable = sorted(k for k, v in self.spa_data.items() if v is None)
        text = (
            f"IPStoPF code {short(self.ipstopf_code)}, "
            f"SPA code {short(self.spa_code)}, "
            f"{len(self.spa_data)} SPA data file(s)"
        )
        if unreadable:
            text += f" ({len(unreadable)} unreadable: {', '.join(unreadable)})"
        return text


def snapshot_inputs(
    ipstopf_root: Path,
    spa_root: Path,
    extra_spa_files: Optional[Mapping[str, Path]] = None,
) -> InputSnapshot:
    """
    Hash both code trees and SPA's data files. Never raises: anything
    unreadable is None, which blocks saving state (so projects run in full).

    Args:
        ipstopf_root: Folder of the imported IPStoPF (main.__file__'s folder).
        spa_root: Folder of the imported SPA (start.__file__'s folder).
        extra_spa_files: SPA inputs outside its data folder, by label
            (e.g. the conductor ratings CSV on ScriptsDEV).
    """
    spa_data: Dict[str, Optional[str]] = {}
    try:
        ipstopf_code = hash_tree(Path(ipstopf_root))
        spa_code = hash_tree(Path(spa_root))
        spa_data.update({
            f"{SPA_DATA_DIR_NAME}/{name}": digest
            for name, digest in hash_folder_files(
                Path(spa_root) / SPA_DATA_DIR_NAME
            ).items()
        })
        for label, path in (extra_spa_files or {}).items():
            spa_data[label] = hash_file(Path(path))
    except Exception as exc:  # noqa: BLE001 - must not break the import
        logger.warning(f"Fingerprint: input snapshot failed: {exc!r}")
        return InputSnapshot(None, None, spa_data)
    return InputSnapshot(ipstopf_code, spa_code, spa_data)


def build_state(
    *,
    project: str,
    region: str,
    run_id: str,
    finished: str,
    audit_version: Optional[str],
    base_version: Optional[str],
    recorder: RunRecorder,
    inputs: InputSnapshot,
) -> Tuple[Optional[ProjectState], str]:
    """
    The state to save for a successful run, or (None, reason).

    A state is only worth saving if a later precheck could trust it: the
    transfer's fingerprint is complete, the run left an audit version to
    find again, and the code and SPA data files were readable. A missing state only costs a
    full run, so every doubt resolves to "do not save".
    """
    if not recorder.is_complete:
        detail = "; ".join(recorder.problems) or "transfer did not finish"
        return None, f"fingerprint incomplete ({detail})"
    if not audit_version:
        return None, "no audit version was created"
    if inputs.ipstopf_code is None:
        return None, "IPStoPF code could not be hashed"
    if inputs.spa_code is None:
        return None, "SPA code could not be hashed"
    unreadable = sorted(k for k, v in inputs.spa_data.items() if v is None)
    if unreadable:
        # SPA read these (it succeeded), so unreadable here means a wrong
        # path or a transient fault: never let them drop out of the check.
        return None, f"SPA data file(s) unreadable: {', '.join(unreadable)}"
    state = ProjectState(
        project=project,
        region=region,
        run_id=run_id,
        finished=finished,
        audit_version=audit_version,
        transfer=recorder.transfer_fingerprint(
            base_version, inputs.ipstopf_code
        ),
        assessment=AssessmentFingerprint(
            code=inputs.spa_code, data_files=dict(inputs.spa_data)
        ),
    )
    return state, "complete"


def invalidate_state(path: Path) -> bool:
    """
    Delete a saved state; True if one was deleted. Never raises: if the
    file cannot be removed the failure is logged as a warning, since the
    next precheck would then compare against a state the failed run made
    stale (delete the file by hand, or run with force).
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(f"Fingerprint: could not remove stale state {path}: {exc}")
        return False
    return True