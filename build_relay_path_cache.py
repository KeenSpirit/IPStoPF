"""
build_relay_path_cache.py - offline resolver for DIgSILENT relay type paths.

Populates the name->full-path cache (config.paths.get_dig_lib_path_cache_file,
i.e. dig_lib_paths.json next to type_mapping.csv) that RelayTypeIndex.build
consumes at runtime. Run this whenever type_mapping.csv gains a model name or
the DIgSILENT library is upgraded; the batch pipeline itself never searches
the library tree.

Strategy: ONE bulk enumeration per top-level DIgSILENT ProtRelay folder
(GetContents("*.TypRelay", 1)) instead of one recursive search per model
name. Every discovered type is cached, not just the currently-needed ones,
so future mapping additions resolve without a rebuild. The cache is saved
incrementally after each folder, so a killed run resumes warm. Enumeration
stops early once every needed name is resolved.

Also acts as a type_mapping.csv validator: any mapped model found in no
library (Ergon, local, or DIgSILENT) is reported as a probable typo/rename.

Run on the execution VM (co-located walk ~60 s vs 70+ min over WAN).

Exit codes: 0 = all mapped models resolved; 1 = some unresolved (see log);
2 = could not open PowerFactory / DIgSILENT library.
"""

import logging
import sys
import time
from pathlib import Path

# Make the IPStoPF repo importable when run directly from anywhere.
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --- PowerFactory engine-mode startup -------------------------------------
# Keep these three lines aligned with ips_to_pf_batch.py (single source of
# truth for the working startup incantation on each machine).
PF_PYTHON_DIR = r"C:\Program Files\DIgSILENT\PowerFactory 2025 SP3\Python\3.12"


def open_powerfactory():
    import os
    pf_install_dir = str(Path(PF_PYTHON_DIR).parents[1])
    os.add_dll_directory(pf_install_dir)
    if PF_PYTHON_DIR not in sys.path:
        sys.path.append(PF_PYTHON_DIR)
    import powerfactory as pf
    app = pf.GetApplicationExt()  # align args with ips_to_pf_batch.py if it
                                  # passes a user / "/ini ..." call function
    if app is None:
        raise RuntimeError("GetApplicationExt returned None")
    return app

def get_app():
    """Return a PF app object in either execution context.

    Inside PowerFactory (ComPython script, e.g. run from the Citrix GUI):
    the ``powerfactory`` module is already importable and GetApplication()
    returns the hosting session. From the command line (engine mode):
    that import fails or returns None, and we start our own session.
    Returns (app, hosted) where hosted=True means we are running inside
    an existing PowerFactory GUI session.
    """
    try:
        import powerfactory as pf
        app = pf.GetApplication()
        if app is not None:
            return app, True
    except ImportError:
        pass
    return open_powerfactory(), False


class _PFOutputHandler(logging.Handler):
    """Mirror log records into the PowerFactory output window.

    Only attached in hosted mode - in engine mode the console shows the
    stream handler output, and in both modes the JSON file log is written
    by logging_config regardless.
    """

    def __init__(self, app):
        super().__init__(level=logging.INFO)
        self._app = app
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            self._app.PrintPlain(self.format(record))
        except Exception:
            pass  # never let output plumbing kill the builder


def main(app) -> int:
    from logging_config import get_logger
    from update_powerfactory.mapping_file import mapped_relay_types
    from update_powerfactory.type_index import (
        _load_path_cache,
        _save_path_cache,
    )
    from utils.pf_utils import all_relevant_objects

    logger = get_logger("update_powerfactory.build_relay_path_cache")
    t_run = time.perf_counter()
    logger.info(f"Path cache builder: running from {__file__} ({sys.executable})")

    needed = set(mapped_relay_types())
    logger.info(f"Path cache builder: {len(needed)} mapped model name(s)")

    # ---- Names already supplied by ErgonLibrary / local (no cache needed) --
    global_library = app.GetGlobalLibrary()
    covered = set()
    t0 = time.perf_counter()
    protection_lib = global_library.GetContents("Protection")
    for obj in all_relevant_objects(app, protection_lib, "*.TypRelay", None) or []:
        covered.add(obj.loc_name)
    current_user = app.GetCurrentUser()
    protection_folder = current_user.GetContents("Protection")
    for obj in all_relevant_objects(app, protection_folder, "*.TypRelay", None) or []:
        covered.add(obj.loc_name)
    logger.info(
        f"Path cache builder: Ergon+local walk done in "
        f"{time.perf_counter() - t0:.1f} s ({len(covered)} types)"
    )

    to_resolve = sorted(needed - covered)
    logger.info(
        f"Path cache builder: {len(to_resolve)} name(s) must come from the "
        f"DIgSILENT library"
    )

    # ---- Validate existing cache entries; prune stale ones -----------------
    cache = _load_path_cache()
    stale = []
    resolved = set()
    for name in to_resolve:
        cached = cache.get(name)
        if not cached:
            continue
        hit = global_library.SearchObject(cached)
        if hit is not None and hit.loc_name == name:
            resolved.add(name)
        else:
            stale.append(name)
            cache.pop(name, None)
    if stale:
        logger.warning(
            f"Path cache builder: pruned {len(stale)} stale entrie(s): {stale}"
        )
    remaining = [n for n in to_resolve if n not in resolved]
    logger.info(
        f"Path cache builder: {len(resolved)} already cached and valid, "
        f"{len(remaining)} to discover"
    )

    # ---- Bulk enumeration of DIgSILENT ProtRelay folders -------------------
    if remaining:
        try:
            database = global_library.fold_id
            dig_lib = database.GetContents("Lib")[0]
            prot_lib = dig_lib.GetContents("Prot")[0]
            relay_lib = prot_lib.GetContents("ProtRelay")
        except (IndexError, AttributeError):
            logger.error("Path cache builder: DIgSILENT library not found")
            return 2

        remaining_set = set(remaining)
        for i, folder in enumerate(relay_lib or [], start=1):
            t0 = time.perf_counter()
            logger.info(
                f"Path cache builder: [{i}/{len(relay_lib)}] enumerating "
                f"'{folder.loc_name}' ({len(remaining_set)} name(s) still "
                f"unresolved)"
            )
            types = folder.GetContents("*.TypRelay", 1) or []
            found_here = 0
            for t in types:
                name = t.loc_name
                if name not in cache:
                    cache[name] = t.GetFullName()
                if name in remaining_set:
                    remaining_set.discard(name)
                    resolved.add(name)
                    found_here += 1
            _save_path_cache(cache)  # incremental: survive a killed run
            logger.info(
                f"Path cache builder: [{i}/{len(relay_lib)}] "
                f"'{folder.loc_name}' done in {time.perf_counter() - t0:.1f} s "
                f"({len(types)} types, {found_here} needed name(s) resolved, "
                f"cache saved)"
            )
            if not remaining_set:
                logger.info(
                    "Path cache builder: all needed names resolved; stopping "
                    "enumeration early"
                )
                break
        remaining = sorted(remaining_set)

    # ---- Report ------------------------------------------------------------
    elapsed = time.perf_counter() - t_run
    logger.info(
        f"Path cache builder: done in {elapsed:.1f} s - "
        f"{len(needed)} mapped, {len(needed - covered) - len(remaining)} "
        f"resolved from DIgSILENT, {len(covered & needed)} from Ergon/local, "
        f"{len(remaining)} unresolved, cache now {len(cache)} entrie(s)"
    )
    for name in remaining:
        logger.warning(
            f"Path cache builder: '{name}' found in NO library - probable "
            f"typo or renamed model in type_mapping.csv column E"
        )
    return 1 if remaining else 0


if __name__ == "__main__":
    try:
        pf_app, hosted = get_app()
    except Exception as exc:
        print(f"Could not open PowerFactory: {exc}")
        sys.exit(2)

    pf_handler = None
    if hosted:
        pf_handler = _PFOutputHandler(pf_app)
        logging.getLogger().addHandler(pf_handler)

    try:
        rc = main(pf_app)
    finally:
        if pf_handler is not None:
            logging.getLogger().removeHandler(pf_handler)

    if hosted:
        # sys.exit inside a hosted script surfaces as a script error in the
        # GUI; report the outcome in the output window instead.
        pf_app.PrintPlain(f"build_relay_path_cache finished with code {rc}")
    else:
        sys.exit(rc)