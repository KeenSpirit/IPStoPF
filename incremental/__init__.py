"""
Incremental batch runs: fingerprint a project's inputs so an unchanged
project can be skipped on the next scheduled run.

fingerprint.py is pure (no PowerFactory, database or network access) and
is covered offline by test_fingerprint.py.
"""

from incremental.fingerprint import (
    SCHEMA_VERSION,
    DEFAULT_MAX_AGE_DAYS,
    Decision,
    DecisionResult,
    TransferFingerprint,
    AssessmentFingerprint,
    ProjectState,
    decide,
    diff_fingerprints,
    digest_records,
    digest_value,
    lookup_key,
    parse_lookup_key,
    hash_file,
    hash_files,
    hash_tree,
    state_path,
    save_state,
    load_state,
)

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_MAX_AGE_DAYS",
    "Decision",
    "DecisionResult",
    "TransferFingerprint",
    "AssessmentFingerprint",
    "ProjectState",
    "decide",
    "diff_fingerprints",
    "digest_records",
    "digest_value",
    "lookup_key",
    "parse_lookup_key",
    "hash_file",
    "hash_files",
    "hash_tree",
    "state_path",
    "save_state",
    "load_state",
]