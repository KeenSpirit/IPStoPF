"""
Type indexing for PowerFactory relay and fuse types.

This module provides indexed lookups for relay and fuse types,
replacing O(n) linear scans with O(1) dictionary lookups.

The TypeIndex classes are designed to be built once at the start of
processing and reused for all device updates, providing significant
performance improvements when processing large numbers of devices.

Usage:
    # Build indexes once
    relay_index = RelayTypeIndex.build(app)
    fuse_index = FuseTypeIndex.build(app)
    
    # O(1) lookups
    relay_type = relay_index.get("Generic SEL351 Relay")
    fuse_type = fuse_index.get_by_curve_and_rating("K", "100A")
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

from utils.pf_utils import all_relevant_objects
from logging_config import get_logger

logger = get_logger(__name__)


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

        # ---- 3. DIgSILENT library: targeted resolution only --------------
        # Full crawls of this tree took ~60 s co-located and 70+ min over WAN
        # latency (Tablelands, 2026-07-15). Only the mapped model names from
        # type_mapping.csv column E can ever be looked up, so resolve just the
        # ones not already supplied by ErgonLibrary/local, via a persisted
        # name->path cache with a server-side filtered search as fallback.
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
        cache_dirty = False
        cache_hits = 0
        unresolved = []

        for name in missing:
            obj = None

            # Fast path: direct fetch via cached full path, no tree walk.
            cached = path_cache.get(name)
            if cached:
                hit = global_library.SearchObject(cached)
                if hit is not None and hit.loc_name == name:
                    obj = hit
                    cache_hits += 1
                else:
                    # Stale entry (library moved/renamed) - drop and re-resolve.
                    path_cache.pop(name, None)
                    cache_dirty = True

            # No live tree search at runtime: discovery is the offline
            # builder's job (build_relay_path_cache.py). A cache miss means
            # the builder has not run since the mapping/library changed.
            # 55 misses cost 6+ h of recursive WAN searches without ever
            # finishing (Stanthorpe, 2026-07-18) - never pay that mid-batch.
            if obj is not None:
                index._by_name[name] = obj
                index._all_types.append(obj)
            else:
                unresolved.append(name)
                logger.warning(
                    f"Type index: mapped model '{name}' not in path cache; "
                    f"run build_relay_path_cache.py to resolve it"
                )

        logger.info(
            f"Type index: DIgSILENT resolution done "
            f"({cache_hits} via path cache, {len(unresolved)} unresolved)"
        )
        if cache_dirty:
            _save_path_cache(path_cache)

        return index

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


@dataclass
class FuseTypeIndex:
    """
    Indexed collection of PowerFactory fuse types for O(1) lookup.

    Fuse types are matched by curve type (K, T, etc.) and rating (e.g., "100A").
    This class provides multiple lookup strategies:
    - Exact name match
    - Curve + rating match
    - Fuse size match (for Tx fuses)

    Attributes:
        _by_name: Dictionary mapping fuse type name to PF object
        _by_curve: Dictionary mapping curve letter to list of fuse types
        _all_types: List of all fuse type objects (for compatibility)
    """
    _by_name: Dict[str, Any] = field(default_factory=dict)
    _by_curve: Dict[str, List[Any]] = field(default_factory=dict)
    _all_types: List[Any] = field(default_factory=list)

    # Pattern to extract rating from fuse name (e.g., " 100A" from "HRC 100A K")
    _RATING_PATTERN = re.compile(r'\s+(\d+)A')

    @classmethod
    def build(cls, app) -> 'FuseTypeIndex':
        """
        Build the fuse type index from PowerFactory ErgonLibrary.

        Args:
            app: PowerFactory application object

        Returns:
            FuseTypeIndex with all fuse types indexed
        """
        index = cls()

        try:
            ergon_lib = app.GetGlobalLibrary()
            fuse_folder = ergon_lib.SearchObject(
                r"\ErgonLibrary\Protection\Fuses.IntFolder"
            )

            if not fuse_folder:
                app.PrintWarn("Fuse folder not found in ErgonLibrary")
                return index

            fuse_types = fuse_folder.GetContents("*.TypFuse", 0)

            for fuse_type in fuse_types or []:
                name = fuse_type.loc_name
                index._by_name[name] = fuse_type
                index._all_types.append(fuse_type)

                # Index by curve type (last character of name)
                curve = name[-1].upper()
                if curve not in index._by_curve:
                    index._by_curve[curve] = []
                index._by_curve[curve].append(fuse_type)

        except AttributeError:
            pass

        return index

    def get(self, name: str) -> Optional[Any]:
        """
        Get a fuse type by exact name match.

        Args:
            name: The fuse type name

        Returns:
            The PowerFactory TypFuse object, or None if not found
        """
        return self._by_name.get(name)

    def get_by_curve_and_rating(
        self,
        curve_type: str,
        rating: str
    ) -> Optional[Any]:
        """
        Find a fuse type matching the curve type and rating.

        This is the primary lookup method for line fuses, matching
        the logic previously in fuse_settings.fuse_setting().

        Args:
            curve_type: The curve type letter (e.g., "K", "T")
            rating: The rating string (e.g., "100A", " 100/")

        Returns:
            The matching PowerFactory TypFuse object, or None if not found
        """
        curve_upper = curve_type.upper()

        # Get all fuses with this curve type
        candidates = self._by_curve.get(curve_upper, [])

        for fuse in candidates:
            if rating in fuse.loc_name:
                return fuse

        return None

    def get_by_fuse_size(self, fuse_size: str) -> Optional[Any]:
        """
        Find a fuse type matching the fuse size specification.

        Used for Tx fuses where the size is determined by transformer rating.

        Args:
            fuse_size: The fuse size string (e.g., "100K", "50T")
                      Last character is curve type, rest is rating

        Returns:
            The matching PowerFactory TypFuse object, or None if not found
        """
        if not fuse_size or len(fuse_size) < 2:
            return None

        curve = fuse_size[-1].upper()
        rating_prefix = fuse_size[:-1]

        candidates = self._by_curve.get(curve, [])

        for fuse in candidates:
            if rating_prefix in fuse.loc_name:
                return fuse

        return None

    def find_matching_fuse(
        self,
        curve_type: Optional[str] = None,
        rating: Optional[str] = None,
        fuse_size: Optional[str] = None
    ) -> Optional[Any]:
        """
        Find a matching fuse type using available criteria.

        This is a convenience method that tries multiple matching strategies:
        1. If curve_type and rating are provided, use get_by_curve_and_rating
        2. If fuse_size is provided, use get_by_fuse_size

        Args:
            curve_type: The curve type letter (optional)
            rating: The rating string (optional)
            fuse_size: The fuse size specification (optional)

        Returns:
            The matching PowerFactory TypFuse object, or None if not found
        """
        # Try curve + rating first
        if curve_type and rating:
            result = self.get_by_curve_and_rating(curve_type, rating)
            if result:
                return result

        # Fall back to fuse size
        if fuse_size:
            return self.get_by_fuse_size(fuse_size)

        return None

    def get_all(self) -> List[Any]:
        """
        Get all fuse types as a list.

        Provided for backward compatibility with code expecting a list.

        Returns:
            List of all fuse type objects
        """
        return self._all_types

    def __len__(self) -> int:
        """Return the number of indexed fuse types."""
        return len(self._by_name)

    def __contains__(self, name: str) -> bool:
        """Check if a fuse type name exists in the index."""
        return name in self._by_name


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