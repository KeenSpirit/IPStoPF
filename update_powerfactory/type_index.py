"""
Type indexing for PowerFactory relay and fuse types.

This module provides indexed lookups for relay and fuse types,
replacing O(n) linear scans with O(1) dictionary lookups.

The TypeIndex classes are designed to be built once at the start of
processing and reused for all device updates, providing significant
performance improvements when processing large numbers of devices.

The relay index keeps its own name -> path cache for DIgSILENT library
types (dig_lib_paths.json). The cache needs no upkeep: a mapped model it
cannot supply triggers one folder walk of the DIgSILENT library, after
which the cache is rewritten.

Usage:
    # Build indexes once
    relay_index = RelayTypeIndex.build(app)
    fuse_index = FuseTypeIndex.build(app)

    # O(1) lookups
    relay_type = relay_index.get("Generic SEL351 Relay")
    fuse_type = fuse_index.get_by_curve_and_rating("K", "100A")
"""

import csv
import json
import os
import re
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

from utils.pf_utils import all_relevant_objects
from logging_config import get_logger

logger = get_logger(__name__)

# Progress line interval while the DIgSILENT library is being walked. The
# walk is silent otherwise, and in a headless run silence looks like a hang.
_WALK_HEARTBEAT_S = 30


def _load_path_cache() -> Dict[str, str]:
    """Load the DIgSILENT library path cache; empty dict on any failure."""
    from config.paths import get_dig_lib_path_cache_file
    try:
        with open(get_dig_lib_path_cache_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _save_path_cache(cache: Dict[str, str]) -> None:
    """Persist the path cache; failure is logged, never fatal (batch safety)."""
    from config.paths import get_dig_lib_path_cache_file
    target = str(get_dig_lib_path_cache_file())
    try:
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning(f"Type index: could not save DIgSILENT path cache: {exc}")


def _walk_digsilent_relay_types(global_library) -> Dict[str, List[Any]]:
    """
    Walk the DIgSILENT library's ProtRelay tree folder by folder.

    Returns {type name: [TypRelay, ...]} in the order found; a name held by
    more than one type keeps them all, first found first.

    Folder by folder on purpose. On the execution VM (2026-11-17) this walk
    took 150 s, while one recursive GetContents("*.TypRelay", 1) on the same
    tree had not returned after 30 minutes.

    Raises IndexError / AttributeError if the library is not where expected.
    """
    database = global_library.fold_id
    dig_lib = database.GetContents("Lib")[0]
    prot_lib = dig_lib.GetContents("Prot")[0]
    pending = list(prot_lib.GetContents("ProtRelay") or [])

    found: Dict[str, List[Any]] = {}
    folders = types = 0
    start = last_beat = time.monotonic()
    while pending:
        folder = pending.pop(0)
        for relay_type in folder.GetContents("*.TypRelay", 0) or []:
            found.setdefault(relay_type.loc_name, []).append(relay_type)
            types += 1
        sub_folders = list(folder.GetContents("*.IntFolder", 0) or [])
        sub_folders += list(folder.GetContents("*.IntPrjfolder", 0) or [])
        pending[:0] = sub_folders  # depth first, as all_relevant_objects
        folders += 1
        now = time.monotonic()
        if now - last_beat >= _WALK_HEARTBEAT_S:
            last_beat = now
            logger.info(
                f"Type index: DIgSILENT walk in progress - {folders} folders, "
                f"{types} types, {now - start:.0f} s"
            )
    logger.info(
        f"Type index: DIgSILENT walk done in {time.monotonic() - start:.0f} s "
        f"({folders} folders, {types} types, {len(found)} names)"
    )
    return found


@dataclass
class RelayTypeIndex:
    """
    Indexed collection of PowerFactory relay types for O(1) lookup.

    Provides direct name-based lookup instead of iterating through
    a list of relay types for each device update.

    Attributes:
        _by_name: Dictionary mapping relay type name to PF object
        _all_types: List of all relay type objects (for compatibility)
    """
    _by_name: Dict[str, Any] = field(default_factory=dict)
    _all_types: List[Any] = field(default_factory=list)

    @classmethod
    def build(cls, app) -> 'RelayTypeIndex':
        """
        Build the relay type index from PowerFactory libraries.

        Searches for relay types in:
        1. ErgonLibrary Protection folder
        2. DIgSILENT global library
        3. Current user's Protection folder (local relays)

        Local relay types take precedence if they have the same name
        as library types.

        Args:
            app: PowerFactory application object

        Returns:
            RelayTypeIndex with all relay types indexed by name
        """

        index = cls()

        # ---- 1. ErgonLibrary Protection folder --------------------------
        logger.info("Type index: walking ErgonLibrary Protection folder")
        global_library = app.GetGlobalLibrary()
        protection_lib = global_library.GetContents("Protection")
        ergon_types = all_relevant_objects(app, protection_lib, "*.TypRelay", None)

        for relay_type in ergon_types or []:
            name = relay_type.loc_name
            index._by_name[name] = relay_type
            index._all_types.append(relay_type)
        logger.info(f"Type index: ErgonLibrary walk done ({len(index)} types)")

        # ---- 2. Current user's Protection folder (takes precedence) -----
        # Moved ahead of the DIgSILENT pass so the 'missing' set below is
        # computed against full Ergon+local precedence. Net precedence is
        # unchanged: local > ErgonLibrary > DIgSILENT.
        logger.info("Type index: walking current user's Protection folder")
        current_user = app.GetCurrentUser()
        protection_folder = current_user.GetContents("Protection")
        local_types = all_relevant_objects(app, protection_folder, "*.TypRelay", None)

        for relay_type in local_types or []:
            name = relay_type.loc_name
            if name not in index._by_name:
                index._all_types.append(relay_type)
            index._by_name[name] = relay_type  # local overrides library type
        logger.info(f"Type index: local walk done ({len(index)} types total)")

        # ---- 3. DIgSILENT library: path cache, walk on a miss -------------
        # Only the mapped model names from type_mapping.csv column E can ever
        # be looked up, so resolve just the ones not already supplied by
        # ErgonLibrary/local. Each is fetched by its cached full path
        # (~15 s cold for the whole index on the VM). If any is not in the
        # cache, or its path no longer resolves, the library is walked once
        # (~150 s on the VM) and the cache rewritten, so the next process is
        # fast again. Nobody has to maintain the file.
        #
        # Function-level import: update_powerfactory/__init__ imports this
        # module, so a top-level import of mapping_file would be circular at
        # package-init time.
        from update_powerfactory.mapping_file import mapped_relay_types

        needed = mapped_relay_types()
        missing = sorted(needed - set(index._by_name))
        logger.info(
            f"Type index: {len(missing)} mapped model(s) not in Ergon/local; "
            f"resolving from DIgSILENT library"
        )
        if not missing:
            return index

        path_cache = _load_path_cache()
        valid_paths: Dict[str, str] = {}  # cached paths confirmed this run
        to_find: List[str] = []
        for name in missing:
            cached = path_cache.get(name)
            hit = global_library.SearchObject(cached) if cached else None
            if hit is not None and hit.loc_name == name:
                index._by_name[name] = hit
                index._all_types.append(hit)
                valid_paths[name] = cached
            else:
                to_find.append(name)

        logger.info(
            f"Type index: DIgSILENT resolution done "
            f"({len(valid_paths)} via path cache, {len(to_find)} not supplied "
            f"by it)"
        )
        if to_find:
            index._find_in_digsilent_library(global_library, to_find, valid_paths)

        return index

    def _find_in_digsilent_library(
        self,
        global_library,
        to_find: List[str],
        valid_paths: Dict[str, str],
    ) -> None:
        """
        Walk the DIgSILENT library for the models the path cache could not
        supply, add them to the index and rewrite the cache.

        Never raises: a failed walk is logged and the models stay
        unresolved (their devices report a type matching error, as before).

        Args:
            global_library: app.GetGlobalLibrary()
            to_find: mapped model names with no usable cache entry
            valid_paths: name -> cached path confirmed this run; kept as is
                in the rewritten cache
        """
        logger.info(
            f"Type index: path cache cannot supply {to_find}; walking the "
            f"DIgSILENT library (a few minutes on the VM)"
        )
        try:
            found = _walk_digsilent_relay_types(global_library)
        except Exception as exc:  # PF raises bare runtime errors too
            logger.warning(
                f"Type index: DIgSILENT library walk failed ({exc!r}); "
                f"unresolved mapped model(s): {to_find}"
            )
            return

        for name in to_find:
            candidates = found.get(name)
            if not candidates:
                logger.warning(
                    f"Type index: mapped model '{name}' found in NO library - "
                    f"probable typo or renamed model in type_mapping.csv "
                    f"column E. The library is walked on every run until "
                    f"this is fixed"
                )
                continue
            self._by_name[name] = candidates[0]
            self._all_types.append(candidates[0])
            if len(candidates) > 1:
                paths = [c.GetFullName() for c in candidates]
                logger.warning(
                    f"Type index: '{name}' is held by {len(candidates)} "
                    f"DIgSILENT types; using the first: {paths}"
                )

        # Rewrite the whole cache from the walk, so entries left stale by a
        # library upgrade are refreshed in the same pass. Paths confirmed
        # this run are kept: where a name is held by more than one type, the
        # type already in use must not change under a later run.
        new_cache = {
            name: candidates[0].GetFullName()
            for name, candidates in found.items()
        }
        new_cache.update(valid_paths)
        _save_path_cache(new_cache)
        logger.info(
            f"Type index: DIgSILENT path cache rewritten "
            f"({len(new_cache)} entries)"
        )

    def get(self, name: str) -> Optional[Any]:
        """
        Get a relay type by exact name match.

        Args:
            name: The relay type name (e.g., "Generic SEL351 Relay")

        Returns:
            The PowerFactory TypRelay object, or None if not found
        """
        return self._by_name.get(name)

    def get_all(self) -> List[Any]:
        """
        Get all relay types as a list.

        Provided for backward compatibility with code expecting a list.

        Returns:
            List of all relay type objects
        """
        return self._all_types

    def __len__(self) -> int:
        """Return the number of indexed relay types."""
        return len(self._by_name)

    def __contains__(self, name: str) -> bool:
        """Check if a relay type name exists in the index."""
        return name in self._by_name


# Fuse link names in ErgonLibrary end in the rating and class, e.g.
# "FUSE LINK 11/22/33kV 20A CLASS K", "FUSE LINK 11/22/33kV  6/20A CLASS K"
# (dual rated) or "FUSE LINK 11/22/33kV 8A CLASS T".
_LINK_NAME = re.compile(
    r"(?<![\d/.])(\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)\s*A\s+CLASS\s+([A-Z])\s*$",
    re.IGNORECASE,
)

# IPS / STNW1001 ratings with no link of that size in the library, mapped to
# the link fitted in the field. 63 -> 65: a 63A class K fuse is fitted as
# the 65A class K link (ErgonLibrary has no 63A one). A rating with no link
# and no alias (e.g. K 5A) stays a 'Type Matching Error' by design.
RATING_ALIASES: Dict[Tuple[str, str], str] = {
    ("K", "63"): "65",
}

# Columns of named_fuse_types.csv (config.paths.get_named_fuse_types_file):
# fuses IPS describes by a curve name rather than a link class (HRC, ELF,
# EFEN/HRC, E). A row with a blank typfuse_name is a placeholder: the fuse
# stays a 'Type Matching Error' until the name is filled in.
NAMED_FUSE_COLUMNS = ("ips_curve", "rating", "typfuse_name", "notes")

_RATING_NUMBER = re.compile(r"(\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)")
_CURVE_WITH_RATING = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Z])$")


def normalise_rating(rating: Any) -> str:
    """' 63A' / '63' / ' 6/20/' / '63.0' -> '63' / '6/20' ('' if none)."""
    match = _RATING_NUMBER.search(str(rating or ""))
    if not match:
        return ""
    value = match.group(1)
    # '63.0' -> '63', but keep '31.5'
    parts = [p[:-2] if p.endswith(".0") else p for p in value.split("/")]
    return "/".join(parts)


def normalise_curve(curve: Any) -> Tuple[str, str]:
    """
    IPS curve field -> (curve, rating found in the curve field).

    ' K' -> ('K', ''); 'K Speed ' / 'K (Kmate)' -> ('K', ''); '40 K' ->
    ('K', '40'); 'EFEN/HRC' -> ('EFEN/HRC', ''); '' -> ('', '').
    """
    text = " ".join(str(curve or "").upper().split())
    if not text:
        return "", ""
    match = _CURVE_WITH_RATING.match(text)
    if match:
        return match.group(2), normalise_rating(match.group(1))
    first = text.split(" ")[0]
    # A single class letter followed by a description ('K SPEED',
    # 'K (KMATE)') is that class.
    if len(first) == 1 and first.isalpha():
        return first, ""
    return text, ""


def load_named_fuse_types(path: Any) -> Dict[Tuple[str, str], str]:
    """
    Read named_fuse_types.csv into {(curve, rating): TypFuse name}.

    Curve and rating are normalised as the IPS values are ('hrc' and
    ' HRC' are the same curve, '20A' and '20' the same rating). Rows with a
    blank typfuse_name are placeholders and skipped. Never raises: a
    missing or unreadable file gives an empty table, with a log line.
    """
    named: Dict[Tuple[str, str], str] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except FileNotFoundError:
        logger.info(f"Fuse type index: no named fuse types file at {path}")
        return named
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        logger.warning(f"Fuse type index: could not read {path}: {exc}")
        return named

    for line_no, row in enumerate(rows, start=2):
        row = {
            str(k).strip().lower(): (v or "").strip()
            for k, v in row.items() if k is not None
        }
        name = row.get("typfuse_name", "")
        if not name:
            continue
        curve, curve_rating = normalise_curve(row.get("ips_curve", ""))
        rating = normalise_rating(row.get("rating", "")) or curve_rating
        if not curve or not rating:
            logger.warning(
                f"Fuse type index: {Path(str(path)).name} line {line_no}: "
                f"needs ips_curve and rating; row ignored"
            )
            continue
        key = (curve, rating)
        if key in named and named[key] != name:
            logger.warning(
                f"Fuse type index: {Path(str(path)).name} line {line_no}: "
                f"{key} is listed twice; using '{name}'"
            )
        named[key] = name
    return named


@dataclass
class FuseTypeIndex:
    """
    Indexed collection of PowerFactory fuse types for O(1) lookup.

    Fuse links are keyed by (class, rating) parsed from the type name
    ("FUSE LINK 11/22/33kV 20A CLASS K" -> ('K', '20')), so a lookup is an
    exact match. The earlier rule keyed by the last character of the name
    and matched the rating as a substring, which could never match a
    multi-letter curve (HRC, ELF) and could pick a dual-rated link for a
    single rating ('20' is in '6/20A').

    Attributes:
        _by_name: type name -> PF object (top folder and subfolders)
        _by_key: (class, rating) -> PF object (top-folder fuse links)
        _all_types: all fuse type objects (for compatibility)
    """
    _by_name: Dict[str, Any] = field(default_factory=dict)
    _by_key: Dict[Tuple[str, str], Any] = field(default_factory=dict)
    _all_types: List[Any] = field(default_factory=list)
    _named: Dict[Tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def build(cls, app, named_path: Optional[Any] = None) -> 'FuseTypeIndex':
        """Build the fuse type index from ErgonLibrary\\Protection\\Fuses."""
        index = cls()
        try:
            ergon_lib = app.GetGlobalLibrary()
            fuse_folder = ergon_lib.SearchObject(
                r"\ErgonLibrary\Protection\Fuses.IntFolder"
            )
            if not fuse_folder:
                app.PrintWarn("Fuse folder not found in ErgonLibrary")
                return index
            top_level = fuse_folder.GetContents("*.TypFuse", 0) or []
            nested = fuse_folder.GetContents("*.TypFuse", 1) or []
        except AttributeError:
            return index

        top_names = {f.loc_name for f in top_level}
        unparsed = []
        for fuse_type in list(top_level) + [
            f for f in nested if f.loc_name not in top_names
        ]:
            index.add(fuse_type, keyed=fuse_type.loc_name in top_names,
                      unparsed=unparsed)

        logger.info(
            f"Fuse type index: {len(index._by_name)} types "
            f"({len(top_names)} in the Fuses folder, {len(index._by_key)} "
            f"keyed by class and rating)"
        )
        logger.info(f"Fuse type index: keyed {sorted(index._by_key)}")
        if unparsed:
            logger.info(
                f"Fuse type index: Fuses-folder types with no class/rating "
                f"in the name (name lookup only): {sorted(unparsed)}"
            )

        if named_path is None:
            from config.paths import get_named_fuse_types_file
            named_path = get_named_fuse_types_file()
        index.set_named_types(load_named_fuse_types(named_path))
        return index

    def set_named_types(self, named: Dict[Tuple[str, str], str]) -> None:
        """Use a named-fuse table; names not in the library are dropped."""
        missing = sorted(
            {name for name in named.values() if name not in self._by_name}
        )
        if missing:
            logger.warning(
                f"Fuse type index: named_fuse_types.csv names TypFuses that "
                f"are not in ErgonLibrary\\Protection\\Fuses (rows ignored): "
                f"{missing}"
            )
        self._named = {
            key: name for key, name in named.items() if name in self._by_name
        }
        if named:
            logger.info(
                f"Fuse type index: {len(self._named)} named fuse types in use"
            )

    def add(self, fuse_type: Any, keyed: bool = True,
            unparsed: Optional[List[str]] = None) -> None:
        """Index one TypFuse (separate from build so it can be tested)."""
        name = fuse_type.loc_name
        if name in self._by_name:
            return
        self._by_name[name] = fuse_type
        self._all_types.append(fuse_type)
        if not keyed:
            return
        match = _LINK_NAME.search(" ".join(name.split()))
        if not match:
            if unparsed is not None:
                unparsed.append(name)
            return
        key = (match.group(2).upper(), normalise_rating(match.group(1)))
        if key in self._by_key:
            logger.warning(
                f"Fuse type index: '{name}' and "
                f"'{self._by_key[key].loc_name}' are both {key}; using the "
                f"second"
            )
            return
        self._by_key[key] = fuse_type

    def get(self, name: str) -> Optional[Any]:
        """Get a fuse type by exact name."""
        return self._by_name.get(name)

    def get_by_key(self, curve: str, rating: str) -> Optional[Any]:
        """Exact (class, rating) lookup, then the rating alias, then the
        named-type table."""
        key = (curve, rating)
        fuse = self._by_key.get(key)
        if fuse is None and key in RATING_ALIASES:
            fuse = self._by_key.get((curve, RATING_ALIASES[key]))
        if fuse is None and key in self._named:
            fuse = self._by_name.get(self._named[key])
        return fuse

    def get_by_curve_and_rating(
        self,
        curve_type: str,
        rating: str
    ) -> Optional[Any]:
        """
        Find the fuse type for an IPS curve and rating.

        Args:
            curve_type: IPS curve field ('K', ' K', '40 K', 'HRC', ...)
            rating: IPS rating (' 63A', ' 6/20/', ...); may be empty when
                the rating is in the curve field

        Returns:
            The matching PowerFactory TypFuse object, or None if not found
        """
        curve, curve_rating = normalise_curve(curve_type)
        rating_key = normalise_rating(rating) or curve_rating
        if not curve or not rating_key:
            return None
        return self.get_by_key(curve, rating_key)

    def get_by_fuse_size(self, fuse_size: str) -> Optional[Any]:
        """
        Find the fuse type for a Tx fuse size ("100K", "6/20K", "50T").

        Last character is the class, the rest is the rating.
        """
        if not fuse_size or len(fuse_size) < 2:
            return None
        return self.get_by_key(
            fuse_size[-1].upper(), normalise_rating(fuse_size[:-1])
        )

    def find_matching_fuse(
        self,
        curve_type: Optional[str] = None,
        rating: Optional[str] = None,
        fuse_size: Optional[str] = None
    ) -> Optional[Any]:
        """Curve + rating first, then the Tx fuse size."""
        if curve_type:
            result = self.get_by_curve_and_rating(curve_type, rating or "")
            if result:
                return result
        if fuse_size:
            return self.get_by_fuse_size(fuse_size)
        return None

    def get_all(self) -> List[Any]:
        """All fuse types as a list (backward compatibility)."""
        return self._all_types

    def __len__(self) -> int:
        """Return the number of indexed fuse types."""
        return len(self._by_name)

    def __contains__(self, name: str) -> bool:
        """Check if a fuse type name exists in the index."""
        return name in self._by_name


# =============================================================================
# Process-level reuse across projects
# =============================================================================
#
# Every type the indexes hold lives OUTSIDE the project: ErgonLibrary and
# the DIgSILENT library (global) and the user's own Protection folder. The
# indexes are therefore the same for every project of a fleet run, yet
# were rebuilt per project: 14.0 min for the first project on 2026-10-03
# (DIgSILENT path-cache resolution 11.5 min) and 1.4 min for each one
# after, i.e. ~1.8 h per 80-project run. They are now built once per
# process and reused while their objects are still readable.

_PROCESS_INDEXES: Dict[str, Any] = {}


def _still_readable(objects: List[Any], sample: int = 3) -> bool:
    """True if a few indexed PF objects still answer (not deleted/stale)."""
    try:
        for obj in objects[:sample]:
            if obj is None or not obj.loc_name:
                return False
            if hasattr(obj, "IsDeleted") and obj.IsDeleted():
                return False
    except Exception:  # PF raises bare runtime errors on dead handles
        return False
    return True


def cached_relay_index(app) -> RelayTypeIndex:
    """RelayTypeIndex.build, reused across projects of one process."""
    index = _PROCESS_INDEXES.get("relay")
    if index is not None and _still_readable(index.get_all()):
        logger.info(f"Type index: reusing the relay type index ({len(index)} types)")
        return index
    index = RelayTypeIndex.build(app)
    _PROCESS_INDEXES["relay"] = index
    return index


def cached_fuse_index(app) -> FuseTypeIndex:
    """FuseTypeIndex.build, reused across projects of one process."""
    index = _PROCESS_INDEXES.get("fuse")
    if index is not None and len(index) and _still_readable(index.get_all()):
        logger.info(f"Type index: reusing the fuse type index ({len(index)} types)")
        return index
    index = FuseTypeIndex.build(app)
    _PROCESS_INDEXES["fuse"] = index
    return index


def clear_process_indexes() -> None:
    """Force the next update_pf to rebuild both indexes."""
    _PROCESS_INDEXES.clear()


# =============================================================================
# Factory functions for convenience
# =============================================================================

def build_type_indexes(app) -> Tuple[RelayTypeIndex, FuseTypeIndex]:
    """
    Build both relay and fuse type indexes.

    Convenience function to build both indexes at once.

    Args:
        app: PowerFactory application object

    Returns:
        Tuple of (RelayTypeIndex, FuseTypeIndex)
    """
    relay_index = RelayTypeIndex.build(app)
    fuse_index = FuseTypeIndex.build(app)
    return relay_index, fuse_index