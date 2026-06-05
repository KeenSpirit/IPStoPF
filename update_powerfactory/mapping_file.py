"""
Mapping file handling for IPS to PowerFactory settings transfer.

This module manages the CSV mapping files that define how IPS settings
are mapped to PowerFactory relay attributes. It provides:
- Type mapping lookup (IPS pattern -> PF relay type + mapping file)
- Detailed mapping file parsing
- Curve mapping for IDMT characteristics

Performance optimizations:
- Type mapping is loaded once and cached
- Individual mapping files are cached after first read
- Curve mapping is loaded once and cached
- Cache can be cleared if files are updated during runtime

Cache Statistics:
    Call get_cache_stats() to see cache hit/miss statistics for
    performance monitoring and debugging.

File Locations:
    - Type mapping: {project_root}/mapping_files/type_mapping/type_mapping.csv
    - Curve mapping: {project_root}/mapping_files/curve_mapping/curve_mapping.csv
    - Relay maps: {project_root}/mapping_files/relay_maps/*.csv
"""

import csv
import os
from typing import Dict, List, Optional, Set, Tuple, Any

# Import paths from config
from config.paths import (
    get_type_mapping_file,
    get_curve_mapping_file,
    get_relay_map_file,
    RELAY_MAPS_DIR,
)


# =============================================================================
# Cache Storage
# =============================================================================

# Type mapping cache.
# Each pattern maps to a dict of CT-secondary variants:
#   {pattern_name: {ct_key: (mapping_filename, relay_type)}}
# ct_key is None for patterns whose mapping does not depend on CT secondary
# (column C blank), or the normalised CT secondary (e.g. "1", "5") for
# CT-dependent patterns that have a separate row per secondary current.
_type_mapping_cache: Optional[Dict[str, Dict[Optional[str], Tuple[str, str]]]] = None

# Excluded patterns cache: set of IPS pattern names flagged for exclusion
# (column B == "Yes") in type_mapping.csv. Populated as a side-effect of
# _load_type_mapping() so the file is only read once.
_excluded_patterns_cache: Optional[Set[str]] = None

# Individual mapping file cache: {filename: list_of_rows}
_mapping_file_cache: Dict[str, List[List[str]]] = {}

# Curve mapping cache: list of [ips_name, code, pf_name] rows
_curve_mapping_cache: Optional[List[List[str]]] = None

# Cache statistics for monitoring
_cache_stats = {
    "type_mapping_hits": 0,
    "type_mapping_misses": 0,
    "mapping_file_hits": 0,
    "mapping_file_misses": 0,
    "curve_mapping_hits": 0,
    "curve_mapping_misses": 0,
}


# =============================================================================
# Cache Management
# =============================================================================

def clear_cache() -> None:
    """
    Clear all cached mapping data.

    Call this if mapping files have been updated during runtime
    and you need to reload them.
    """
    global _type_mapping_cache, _mapping_file_cache, _curve_mapping_cache
    global _excluded_patterns_cache
    _type_mapping_cache = None
    _excluded_patterns_cache = None
    _mapping_file_cache.clear()
    _curve_mapping_cache = None


def get_cache_stats() -> Dict[str, Any]:
    """
    Get cache statistics for monitoring and debugging.

    Returns:
        Dictionary with cache hit/miss counts and current cache sizes
    """
    return {
        **_cache_stats,
        "type_mapping_loaded": _type_mapping_cache is not None,
        "excluded_patterns_count": (
            len(_excluded_patterns_cache)
            if _excluded_patterns_cache is not None
            else 0
        ),
        "mapping_files_cached": len(_mapping_file_cache),
        "curve_mapping_loaded": _curve_mapping_cache is not None,
    }


def preload_cache() -> None:
    """
    Preload all caches at startup.

    Call this during initialization to front-load all file I/O
    rather than incurring it during device processing.
    """
    _load_type_mapping()
    _load_curve_mapping()
    # Note: Individual mapping files are loaded on-demand since
    # we may not need all of them for a given run


# =============================================================================
# Type Mapping (pattern -> mapping file + relay type)
# =============================================================================

def _read_mapping_csv_lines(filepath: str) -> List[str]:
    """
    Read a mapping CSV file as a list of lines, tolerating encoding.

    type_mapping.csv can contain non-ASCII characters (e.g. the degree sign
    in some pattern names). Files exported from Excel on Windows are commonly
    saved as CP1252 rather than UTF-8, so fall back to CP1252 if UTF-8
    decoding fails. Without this, a single non-ASCII character would raise
    UnicodeDecodeError and cause the entire mapping to load as empty.

    Args:
        filepath: Path to the CSV file

    Returns:
        List of text lines (newline characters stripped)

    Raises:
        FileNotFoundError, PermissionError, OSError: propagated to the caller
    """
    try:
        with open(filepath, "r", encoding="utf-8", newline="") as f:
            return f.read().splitlines()
    except UnicodeDecodeError:
        with open(filepath, "r", encoding="cp1252", newline="") as f:
            return f.read().splitlines()


def _normalise_ct_key(value: Any) -> Optional[str]:
    """
    Normalise a CT secondary value into a cache/lookup key.

    Accepts the CT Sec cell from type_mapping.csv (e.g. "", "1", "5") or a
    device's ct_secondary (e.g. 1, 5, 5.0, "5"). Blank/None values normalise
    to None, meaning "not CT dependent". Numeric values normalise to their
    integer string form so that "5", "5.0" and 5 all match.

    Args:
        value: The raw CT secondary value

    Returns:
        Normalised key string, or None if the value is blank
    """
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        return str(int(float(text)))
    except (ValueError, TypeError):
        return text


def _load_type_mapping() -> Dict[str, Dict[Optional[str], Tuple[str, str]]]:
    """
    Load and cache the type mapping from type_mapping.csv.

    The type mapping file has the following column layout (1-indexed):
        A: IPS pattern name
        B: Exclude flag ("Yes"/"No", case-insensitive)
        C: CT secondary (blank, or e.g. "1"/"5" for CT-dependent patterns)
        D: Mapping file name
        E: PowerFactory relay model (relay type)
        F-G: Notes (not used here)

    Some patterns map to different PowerFactory models depending on the
    secondary current of the associated CT. These patterns appear on more
    than one row, distinguished by the CT Sec value in column C (for example
    SEL311C_Energex -> "SEL 311C-1A" for a 1 A CT and "SEL 311C-5A" for 5 A).
    Patterns whose mapping does not depend on the CT secondary have a blank
    column C and a single row.

    A single pass populates two caches:
        _type_mapping_cache:      {pattern_name: {ct_key: (mapping_filename,
                                  relay_type)}} where ct_key is None for
                                  non-CT-dependent patterns.
        _excluded_patterns_cache: {pattern_name, ...} for every row whose
                                  Exclude flag (column B) is "Yes".

    The excluded set replaces the former EXCLUDED_PATTERNS constant: column B
    of type_mapping.csv is the single source of truth for which patterns are
    filtered out during lookups (see is_excluded_pattern).

    Returns:
        Dictionary mapping pattern names to their CT-secondary variants.
    """
    global _type_mapping_cache, _excluded_patterns_cache, _cache_stats

    if _type_mapping_cache is not None:
        _cache_stats["type_mapping_hits"] += 1
        return _type_mapping_cache

    _cache_stats["type_mapping_misses"] += 1
    _type_mapping_cache = {}
    _excluded_patterns_cache = set()

    filepath = get_type_mapping_file()

    try:
        lines = _read_mapping_csv_lines(filepath)
    except FileNotFoundError:
        return _type_mapping_cache  # Return empty caches if file not found
    except (PermissionError, OSError):
        return _type_mapping_cache  # File access issues - return empty caches

    try:
        for line in csv.reader(lines):
            # Need at least up to the PF_MODEL column (index 4)
            if len(line) < 5:
                continue

            pattern_name = line[0].strip()

            # Skip the header row and blank pattern names
            if not pattern_name or pattern_name == "IPS":
                continue

            exclude_flag = line[1].strip().lower()
            ct_key = _normalise_ct_key(line[2])  # Column C: CT secondary
            mapping_filename = line[3].strip()
            relay_type = line[4].strip()

            variants = _type_mapping_cache.setdefault(pattern_name, {})

            # Don't let a blank/empty duplicate row overwrite a populated one.
            # (Otherwise last-wins could replace a real mapping with an empty
            # row that shares the same pattern name and CT key.)
            existing = variants.get(ct_key)
            new_is_empty = not mapping_filename and not relay_type
            if existing is not None and new_is_empty:
                pass  # keep the existing populated variant
            else:
                variants[ct_key] = (mapping_filename, relay_type)

            # Column B is the source of truth for exclusion (case-insensitive)
            if exclude_flag == "yes":
                _excluded_patterns_cache.add(pattern_name)
    except csv.Error:
        pass  # Malformed CSV - keep whatever was parsed so far

    return _type_mapping_cache


def _select_type_variant(
    variants: Dict[Optional[str], Tuple[str, str]],
    ct_secondary: Any = None
) -> Optional[Tuple[str, str]]:
    """
    Pick the appropriate (mapping_filename, relay_type) variant for a pattern.

    Selection rules:
    - Non-CT-dependent pattern (only a None-keyed variant): return it,
      regardless of ct_secondary.
    - CT-dependent pattern: return the variant matching the supplied CT
      secondary. If no CT secondary is given or it doesn't match a known
      variant, fall back deterministically to a non-CT-dependent variant if
      present, then to the 1 A variant, then to the lowest available key.

    Args:
        variants: Mapping of ct_key -> (mapping_filename, relay_type)
        ct_secondary: The device's CT secondary current (e.g. 1 or 5)

    Returns:
        The selected (mapping_filename, relay_type) tuple, or None if empty.
    """
    if not variants:
        return None

    # Non-CT-dependent: a single variant stored under the None key.
    if len(variants) == 1 and None in variants:
        return variants[None]

    # CT-dependent: try to match the device's CT secondary.
    key = _normalise_ct_key(ct_secondary)
    if key is not None and key in variants:
        return variants[key]

    # Fallbacks when the CT secondary is unknown or unmatched.
    if None in variants:
        return variants[None]
    if "1" in variants:
        return variants["1"]

    # Deterministic last resort: numeric keys first (ascending), then any.
    def _sort_key(k: Optional[str]):
        if k is None:
            return (2, "")
        return (0, int(k)) if k.isdigit() else (1, k)

    first_key = sorted(variants.keys(), key=_sort_key)[0]
    return variants[first_key]


def is_excluded_pattern(pattern_name: str) -> bool:
    """
    Check if a pattern should be excluded from processing.

    A pattern is excluded when it is flagged "Yes" in column B (Exclude) of
    type_mapping.csv. Matching is a substring check (an excluded pattern name
    appearing within the supplied pattern name), preserving the behaviour of
    the former EXCLUDED_PATTERNS-based filter.

    Args:
        pattern_name: The IPS relay pattern name

    Returns:
        True if the pattern should be excluded
    """
    if not pattern_name:
        return False

    # Ensure the excluded set is populated (loaded once, then cached)
    _load_type_mapping()

    for excluded in (_excluded_patterns_cache or set()):
        if excluded in pattern_name:
            return True

    return False


def get_type_mapping(
    pattern_name: str,
    ct_secondary: Any = None
) -> Optional[Tuple[str, str]]:
    """
    Get the mapping file name and relay type for a pattern.

    For patterns whose PowerFactory model depends on the CT secondary current
    (e.g. SEL311C_Energex), pass the device's ct_secondary so the correct
    variant is selected. For patterns that are not CT dependent, ct_secondary
    is ignored.

    Args:
        pattern_name: The IPS relay pattern name
        ct_secondary: The associated CT secondary current (e.g. 1 or 5).
            Optional; defaults to None (selects the non-CT-dependent variant,
            falling back deterministically for CT-dependent patterns).

    Returns:
        Tuple of (mapping_filename, relay_type) or None if not found
    """
    variants = _load_type_mapping().get(pattern_name)
    if not variants:
        return None
    return _select_type_variant(variants, ct_secondary)


# =============================================================================
# Individual Mapping Files
# =============================================================================

def _load_mapping_file(filename: str) -> Optional[List[List[str]]]:
    """
    Load and cache an individual mapping file.

    Args:
        filename: The mapping file name (without .csv extension)

    Returns:
        List of rows from the mapping file, or None if file not found
    """
    global _mapping_file_cache, _cache_stats

    if filename in _mapping_file_cache:
        _cache_stats["mapping_file_hits"] += 1
        return _mapping_file_cache[filename]

    _cache_stats["mapping_file_misses"] += 1

    filepath = get_relay_map_file(filename)

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            rows = []
            reader = csv.reader(f, skipinitialspace=True)
            for row in reader:
                # Skip header row
                if "FOLDER" in row[0] and "ELEMENT" in row[1]:
                    continue
                rows.append(row)

            _mapping_file_cache[filename] = rows
            return rows

    except FileNotFoundError:
        return None
    except (PermissionError, UnicodeDecodeError, csv.Error, OSError):
        return None


# =============================================================================
# Curve Mapping
# =============================================================================

def _load_curve_mapping() -> List[List[str]]:
    """
    Load and cache the curve mapping from curve_mapping.csv.

    Returns:
        List of [ips_curve_name, code, pf_curve_name] rows
    """
    global _curve_mapping_cache, _cache_stats

    if _curve_mapping_cache is not None:
        _cache_stats["curve_mapping_hits"] += 1
        return _curve_mapping_cache

    _cache_stats["curve_mapping_misses"] += 1
    _curve_mapping_cache = []

    filepath = get_curve_mapping_file()

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for row in f.readlines():
                line = row.strip().split(",")
                if len(line) >= 3:
                    _curve_mapping_cache.append(line)
    except FileNotFoundError:
        pass
    except (PermissionError, UnicodeDecodeError, OSError):
        pass

    return _curve_mapping_cache


def _find_curve_in_mapping(setting_value: str) -> Optional[str]:
    """
    Look up a curve name in the curve mapping.

    Args:
        setting_value: The IPS curve setting value

    Returns:
        The PowerFactory curve name, or None if not found
    """
    curve_mapping = _load_curve_mapping()

    for line in curve_mapping:
        mapping_value = line[1]

        # Handle binary curve codes - pad with leading zeros
        try:
            int(mapping_value)
            while len(mapping_value) < len(setting_value):
                mapping_value = "0" + mapping_value
        except ValueError:
            pass

        if setting_value == mapping_value:
            return line[2]  # Return PF curve name

    return None


# =============================================================================
# Public API - Main Functions
# =============================================================================

def get_pf_curve(app, setting_value: str, element) -> Any:
    """
    Get the PowerFactory curve object for an IPS curve setting.

    Curves require a PowerFactory object to be assigned to the attribute.
    This function finds the matching curve from the relay type's available
    curves.

    Args:
        app: PowerFactory application object
        setting_value: The curve name/code from IPS
        element: The PowerFactory element (must have typ_id with pcharac)

    Returns:
        The PowerFactory curve object
    """
    idmt_type = element.typ_id
    curves = idmt_type.GetAttribute("e:pcharac")

    # Try exact match first
    for curve in curves:
        if curve.loc_name == setting_value:
            return curve

    # Try partial matches
    reduced_curves = []
    for curve in curves:
        if setting_value in curve.loc_name or curve.loc_name in setting_value:
            reduced_curves.append(curve)

    for curve in reduced_curves:
        if setting_value in curve.loc_name:
            return curve

    for curve in reduced_curves:
        if curve.loc_name in setting_value:
            return curve

    # Try curve mapping lookup (cached)
    mapped_curve_name = _find_curve_in_mapping(setting_value)
    if mapped_curve_name:
        for curve in curves:
            if curve.loc_name == mapped_curve_name:
                return curve

    # Try keyword-based matching as fallback
    keyword_matches = [
        ("Extreme", "Extreme"),
        ("Standard", "Standard"),
        ("Very", "Very"),
        ("Definite", "DT"),
        ("Curve A", "Curve A"),
        ("Curve B", "Curve B"),
        ("Curve C", "Curve C"),
        ("Curve D", "Curve D"),
    ]

    for curve in curves:
        for curve_keyword, setting_keyword in keyword_matches:
            if curve_keyword in curve.loc_name and setting_keyword in setting_value:
                return curve

    # Default to Standard Inverse if no match found
    for curve in curves:
        if "Standard" in curve.loc_name:
            return curve

    # Return first available curve as last resort
    return curves[0] if curves else None


def read_mapping_file(
    app,
    rel_pattern: str,
    pf_device,
    ct_secondary: Any = None
) -> Tuple[Optional[List[List[str]]], Optional[str]]:
    """
    Read the mapping file for a relay pattern.

    Looks up the relay pattern in the type mapping, then loads and
    processes the corresponding mapping file. When a pattern maps to
    different PowerFactory models depending on the CT secondary current,
    ct_secondary selects the correct variant.

    Args:
        app: PowerFactory application object
        rel_pattern: The IPS relay pattern name
        pf_device: The PowerFactory device object (for name substitution)
        ct_secondary: The device's CT secondary current (e.g. 1 or 5), used
            to disambiguate CT-dependent patterns. Optional.

    Returns:
        Tuple of (mapping_file_rows, relay_type) or (None, None) if not found
    """
    # Look up pattern in type mapping (cached), selecting the CT variant
    type_info = get_type_mapping(rel_pattern, ct_secondary)

    if not type_info:
        return None, None

    mapping_filename, relay_type = type_info

    # Load the mapping file (cached)
    raw_rows = _load_mapping_file(mapping_filename)

    if raw_rows is None:
        return None, None

    # Process the rows for this device
    # Note: We create a new list here because we modify rows based on pf_device
    mapping_file = []
    device_name = pf_device.loc_name

    for row in raw_rows:
        # Skip rows without meaningful data
        if len(row) < 4:
            continue

        if row[3] == "None" and "_dip" not in row[1]:
            if len(row) > 4:
                if not row[4]:
                    continue
            else:
                continue

        # Create a copy of the row to avoid modifying the cache
        processed_row = list(row)

        # Replace placeholder folder names with device name
        if processed_row[0] in ["Relay Model", "Default", "default"]:
            processed_row[0] = device_name

        # Remove trailing empty elements
        while processed_row and processed_row[-1] == "":
            processed_row.pop()

        mapping_file.append(processed_row)

    return mapping_file, relay_type


# =============================================================================
# Utility Functions
# =============================================================================

def get_available_patterns() -> List[str]:
    """
    Get all available relay patterns from the type mapping.

    Returns:
        List of pattern names that have mapping files configured
    """
    type_mapping = _load_type_mapping()
    return list(type_mapping.keys())


def is_pattern_mapped(pattern_name: str) -> bool:
    """
    Check if a relay pattern has a mapping file configured.

    Args:
        pattern_name: The IPS relay pattern name

    Returns:
        True if the pattern has a mapping configuration
    """
    return get_type_mapping(pattern_name) is not None


def get_relay_type_for_pattern(
    pattern_name: str,
    ct_secondary: Any = None
) -> Optional[str]:
    """
    Get the PowerFactory relay type name for a pattern.

    Args:
        pattern_name: The IPS relay pattern name
        ct_secondary: The associated CT secondary current, used to select the
            correct variant for CT-dependent patterns. Optional.

    Returns:
        The relay type name, or None if pattern not mapped
    """
    type_info = get_type_mapping(pattern_name, ct_secondary)
    if type_info:
        return type_info[1]  # relay_type is second element
    return None