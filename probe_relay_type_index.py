"""
probe_relay_type_index.py - time the ways of building the relay type dictionary.

Question being answered: on the execution VM, is a live search of the
PowerFactory libraries fast enough to replace dig_lib_paths.json (and
build_relay_path_cache.py that maintains it)?

Methods compared. Each one ends with the same thing: a dict of relay type
name -> TypRelay object, which is what RelayTypeIndex holds.

    json   Current production (RelayTypeIndex.build): walk ErgonLibrary and
           the current user's Protection folder, then fetch each remaining
           mapped model from the DIgSILENT library by its cached full path
           (dig_lib_paths.json + SearchObject). No DIgSILENT tree search.
    walk   The old method (ips_to_pf-master get_relay_types): walk
           ErgonLibrary, the current user's Protection folder and the whole
           DIgSILENT ProtRelay tree folder by folder with all_relevant_objects
           (three GetContents calls per folder). No json file.
    bulk   As walk, but the DIgSILENT tree is fetched with one recursive
           GetContents("*.TypRelay", 1) per ProtRelay folder - the call
           build_relay_path_cache.py already uses. No json file.

How the comparison is kept fair: PowerFactory keeps every object it has
loaded in memory, so whichever method runs second in a session is faster
than it would be on its own. Each method therefore runs in its own fresh
python process with its own PowerFactory session ("cold"), one after the
other. The same process then repeats the build once ("warm") to show how
much of the cold time was database fetch. The whole sequence is run
ROUNDS times, reversing the order each round, so any caching on the
database server shows up as a difference between rounds.

READ-ONLY. Nothing is written to PowerFactory, and dig_lib_paths.json is
read but never rewritten (production prunes stale entries; the probe only
reports them). No project is activated.

Self-contained: imports nothing from the IPStoPF repo, only reads
type_mapping.csv and dig_lib_paths.json from it. The three build functions
replicate update_powerfactory/type_index.py, mapping_file.mapped_relay_types
and utils/pf_utils.all_relevant_objects as of 2026-11-17.

Put this file in the IPStoPF repo root (next to build_relay_path_cache.py)
and run it headless on the VM:

    python probe_relay_type_index.py

Output: a log (which ends with the results table) and a results JSON in
OUTPUT_DIR. Exit codes: 0 = every method timed; 1 = a method failed or
timed out (see log); 2 = configuration problem, nothing was timed.
"""

import csv
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
# PowerFactory startup: same values as export_pf_switches.py /
# ips_to_pf_batch.py. The password comes from Windows Credential Manager
# via keyring, as there.
PF_INSTALL_DIR = r"C:\Program Files\DIgSILENT\PowerFactory 2025 SP3"
PF_PYTHON_DIR = rf"{PF_INSTALL_DIR}\Python\3.12"
PF_INI_FILE = "PowerFactory_PRD.ini"
CONFIG_YAML = r"C:\LocalData\ProtectionBatchRunner\config.yaml"

# The IPStoPF repo whose mapping files are read. Default: the folder this
# script sits in (drop it next to build_relay_path_cache.py).
REPO_ROOT = Path(__file__).resolve().parent
TYPE_MAPPING_CSV = REPO_ROOT / "mapping_files" / "type_mapping" / "type_mapping.csv"
PATH_CACHE_JSON = REPO_ROOT / "mapping_files" / "type_mapping" / "dig_lib_paths.json"

OUTPUT_DIR = r"C:\LocalData\relay type index probe"

# Methods to time, in round-1 order. Remove "bulk" to compare only the
# current and the old method.
METHODS = ("json", "walk", "bulk")

# Even rounds run METHODS in order, odd rounds in reverse.
ROUNDS = 2

# Repeat each build once in the same process for the warm figure.
WARM_REPEAT = True

# A method still running after this long is killed and reported as timed
# out, and is not attempted again in later rounds. The old walk took 70+
# minutes over the WAN; 30 minutes is already far too slow to adopt.
CHILD_TIMEOUT_S = 30 * 60

# Progress line interval while a tree walk is running.
HEARTBEAT_S = 15

# A child that has written its full result but not exited (PowerFactory
# slow to shut down) is killed after this long; its result still counts.
EXIT_GRACE_S = 120

METHOD_LABELS = {
    "json": "json path cache (current)",
    "walk": "live folder walk (old method)",
    "bulk": "live bulk fetch",
}


def log(message):
    """Timestamped line to stdout, flushed (engine mode buffers stdout)."""
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


# --------------------------------------------------------------------------
# Mapping file readers (replicas of the IPStoPF ones; no PowerFactory)
# --------------------------------------------------------------------------
def _normalise_ct_key(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(int(float(text)))
    except (ValueError, TypeError):
        return text


def mapped_relay_types(csv_path=TYPE_MAPPING_CSV):
    """
    PowerFactory model names referenced by type_mapping.csv (column E) for
    every pattern not flagged Exclude = Yes (column B). Replicates
    mapping_file._load_type_mapping + mapped_relay_types.
    """
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            lines = f.read().splitlines()
    except UnicodeDecodeError:
        with open(csv_path, "r", encoding="cp1252", newline="") as f:
            lines = f.read().splitlines()

    variants = {}  # pattern -> {ct_key: relay_type}
    excluded = set()
    for line in csv.reader(lines):
        if len(line) < 5:
            continue
        pattern = line[0].strip()
        if not pattern or pattern == "IPS":
            continue
        ct_key = _normalise_ct_key(line[2])
        mapping_filename = line[3].strip()
        relay_type = line[4].strip()
        by_ct = variants.setdefault(pattern, {})
        # A blank duplicate row does not overwrite a populated one.
        if not (ct_key in by_ct and not mapping_filename and not relay_type):
            by_ct[ct_key] = relay_type
        if line[1].strip().lower() == "yes":
            excluded.add(pattern)

    return {
        relay_type
        for pattern, by_ct in variants.items()
        if pattern not in excluded
        for relay_type in by_ct.values()
        if relay_type
    }


def load_path_cache(json_path=PATH_CACHE_JSON):
    """dig_lib_paths.json as {name: full path}; empty dict on any failure."""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


# --------------------------------------------------------------------------
# PowerFactory library access (child process only)
# --------------------------------------------------------------------------
class _Heartbeat:
    """Progress line every HEARTBEAT_S while a folder walk is running."""

    def __init__(self, label):
        self.label = label
        self.folders = 0
        self._start = self._last = time.perf_counter()

    def folder_done(self, types_so_far):
        self.folders += 1
        now = time.perf_counter()
        if now - self._last >= HEARTBEAT_S:
            self._last = now
            log(
                f"  {self.label}: {self.folders} folders visited, "
                f"{types_so_far} types so far, {now - self._start:.0f} s"
            )


def all_relevant_objects(folders, type_of_obj, objects=None, beat=None):
    """
    The old search function: depth-first, one non-recursive GetContents for
    the objects and two for the subfolders of every folder. Same calls as
    utils/pf_utils.all_relevant_objects.
    """
    if objects is None:
        objects = []
    for folder in folders:
        objects.extend(folder.GetContents(type_of_obj, 0) or [])
        sub_folders = list(folder.GetContents("*.IntFolder", 0) or [])
        sub_folders += list(folder.GetContents("*.IntPrjfolder", 0) or [])
        if beat is not None:
            beat.folder_done(len(objects))
        if sub_folders:
            all_relevant_objects(sub_folders, type_of_obj, objects, beat)
    return objects


def _ergon_and_local(app, stages):
    """
    Walk ErgonLibrary\\Protection and the current user's Protection folder.
    Common to every method. Returns (global_library, ergon, local) where
    ergon/local are lists of (loc_name, object).
    """
    t0 = time.perf_counter()
    global_library = app.GetGlobalLibrary()
    protection_lib = global_library.GetContents("Protection") or []
    ergon = [
        (obj.loc_name, obj)
        for obj in all_relevant_objects(protection_lib, "*.TypRelay")
    ]
    stages["ergon_walk"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    protection_folder = app.GetCurrentUser().GetContents("Protection") or []
    local = [
        (obj.loc_name, obj)
        for obj in all_relevant_objects(protection_folder, "*.TypRelay")
    ]
    stages["local_walk"] = time.perf_counter() - t0
    return global_library, ergon, local


def _protrelay_folders(global_library):
    """The DIgSILENT library's ProtRelay folder(s), found as the old code did."""
    database = global_library.fold_id
    dig_lib = database.GetContents("Lib")[0]
    prot_lib = dig_lib.GetContents("Prot")[0]
    return prot_lib.GetContents("ProtRelay") or []


def build_json(app, needed):
    """Current production method. Replicates RelayTypeIndex.build."""
    stages, by_name, source = {}, {}, {}
    global_library, ergon, local = _ergon_and_local(app, stages)
    for name, obj in ergon:
        by_name[name] = obj
        source[name] = "ergon"
    for name, obj in local:
        by_name[name] = obj  # local overrides the library type
        source[name] = "local"

    t0 = time.perf_counter()
    path_cache = load_path_cache()
    stages["read_json"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    missing = sorted(needed - set(by_name))
    stale, not_cached = [], []
    for name in missing:
        cached = path_cache.get(name)
        if not cached:
            not_cached.append(name)
            continue
        hit = global_library.SearchObject(cached)
        if hit is not None and hit.loc_name == name:
            by_name[name] = hit
            source[name] = "digsilent"
        else:
            stale.append(name)
    stages["dig_resolve"] = time.perf_counter() - t0

    info = {
        "cache_entries": len(path_cache),
        "needed_from_digsilent": len(missing),
        "stale_cache_entries": stale,
        "not_in_cache": not_cached,
    }
    return by_name, source, stages, info, {}


def _build_live(app, fetch_dig):
    """Shared body of the two live-search methods."""
    stages, by_name, source, dig_all = {}, {}, {}, {}
    global_library, ergon, local = _ergon_and_local(app, stages)

    t0 = time.perf_counter()
    dig_types, info = fetch_dig(_protrelay_folders(global_library))
    stages["dig_search"] = time.perf_counter() - t0

    # Reading loc_name is one API call per type, and the dict cannot be
    # built without it, so it is part of the method's cost.
    t0 = time.perf_counter()
    for obj in dig_types:
        name = obj.loc_name
        dig_all.setdefault(name, []).append(obj)
        if name not in by_name:  # first found wins, as the old scan did
            by_name[name] = obj
            source[name] = "digsilent"
    stages["dig_read_names"] = time.perf_counter() - t0

    # Same precedence as production: local > ErgonLibrary > DIgSILENT.
    for name, obj in ergon:
        by_name[name] = obj
        source[name] = "ergon"
    for name, obj in local:
        by_name[name] = obj
        source[name] = "local"

    info["digsilent_types"] = len(dig_types)
    info["digsilent_duplicate_names"] = sum(
        1 for objs in dig_all.values() if len(objs) > 1
    )
    return by_name, source, stages, info, dig_all


def build_walk(app, needed):
    """Old method: folder-by-folder walk of the DIgSILENT ProtRelay tree."""
    def fetch(folders):
        beat = _Heartbeat("DIgSILENT walk")
        types = all_relevant_objects(folders, "*.TypRelay", beat=beat)
        return types, {"digsilent_folders_visited": beat.folders}

    return _build_live(app, fetch)


def build_bulk(app, needed):
    """One recursive GetContents per DIgSILENT ProtRelay folder."""
    def fetch(folders):
        types = []
        for folder in folders:
            types.extend(folder.GetContents("*.TypRelay", 1) or [])
        return types, {"digsilent_bulk_calls": len(folders)}

    return _build_live(app, fetch)


BUILDERS = {"json": build_json, "walk": build_walk, "bulk": build_bulk}


def open_powerfactory():
    """Engine-mode session, started as export_pf_switches.py does."""
    import yaml
    import keyring

    os.add_dll_directory(PF_INSTALL_DIR)
    if PF_PYTHON_DIR not in sys.path:
        sys.path.append(PF_PYTHON_DIR)
    import powerfactory as pf

    with open(CONFIG_YAML) as fh:
        user = yaml.safe_load(fh)["powerfactory"]["user"]
    password = keyring.get_password("PowerFactory", user)
    call = f'/ini "{PF_INSTALL_DIR}\\{PF_INI_FILE}"'
    app = pf.GetApplicationExt(user, password, call)
    if app is None:
        raise RuntimeError("GetApplicationExt returned None")
    return app


# --------------------------------------------------------------------------
# Child: one method, one fresh PowerFactory session
# --------------------------------------------------------------------------
def _write_json(path, payload):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def child_main(method, result_path):
    result = {"method": method, "runs": [], "error": None}
    try:
        needed = mapped_relay_types()

        t0 = time.perf_counter()
        app = open_powerfactory()
        result["login_s"] = time.perf_counter() - t0
        log(f"  PowerFactory session opened in {result['login_s']:.1f} s")

        build = BUILDERS[method]
        for label in ("cold", "warm") if WARM_REPEAT else ("cold",):
            t0 = time.perf_counter()
            by_name, source, stages, info, dig_all = build(app, needed)
            total = time.perf_counter() - t0
            result["runs"].append({
                "label": label,
                "total_s": total,
                "stages_s": stages,
                "types": len(by_name),
                "info": info,
            })
            stage_text = ", ".join(f"{k} {v:.2f}" for k, v in stages.items())
            log(
                f"  {method} {label}: {total:.2f} s, {len(by_name)} types "
                f"({stage_text})"
            )
            # Save after every build so a hang in the warm repeat or at
            # shutdown still leaves the cold figure for the parent.
            _write_json(result_path, result)

        # Untimed: what each mapped model resolved to, for the equivalence
        # check between methods.
        result["resolved"] = {
            name: {"path": by_name[name].GetFullName(), "source": source[name]}
            for name in sorted(needed)
            if name in by_name
        }
        # Mapped models a live search would have to choose between: more
        # than one DIgSILENT type of that name and no Ergon/local override.
        result["needed_duplicates"] = {
            name: [obj.GetFullName() for obj in objs]
            for name, objs in sorted(dig_all.items())
            if name in needed and len(objs) > 1
            and source.get(name) == "digsilent"
        }
    except Exception as exc:  # report, never leave the parent guessing
        result["error"] = f"{type(exc).__name__}: {exc}"
        log(f"  {method} FAILED: {result['error']}")
    result["complete"] = True
    _write_json(result_path, result)
    return 0 if result["error"] is None else 3


# --------------------------------------------------------------------------
# Parent: run the children, collate, report
# --------------------------------------------------------------------------
class _Tee:
    """Write lines to the console and the log file."""

    def __init__(self, log_path):
        self._file = open(log_path, "w", encoding="utf-8")
        self._lock = threading.Lock()

    def line(self, text):
        with self._lock:
            print(text, flush=True)
            self._file.write(text + "\n")
            self._file.flush()

    def log(self, message):
        self.line(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}")

    def close(self):
        self._file.close()


def _read_result(result_path):
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _run_child(method, result_path, tee):
    """Run one method in a fresh process. Returns (status, result or None)."""
    cmd = [
        sys.executable, "-u", str(Path(__file__).resolve()),
        "--child", method, str(result_path),
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    last_output = [time.perf_counter()]

    def pump():
        for raw in proc.stdout:
            last_output[0] = time.perf_counter()
            tee.line(raw.rstrip("\n"))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    start = time.perf_counter()
    status = "ok"
    complete_at = None
    try:
        while True:
            try:
                proc.wait(timeout=HEARTBEAT_S)
                break
            except subprocess.TimeoutExpired:
                now = time.perf_counter()
                written = _read_result(result_path) or {}
                if complete_at is None and written.get("complete"):
                    complete_at = now
                if complete_at is not None and now - complete_at >= EXIT_GRACE_S:
                    tee.log(f"  {method}: result written but the process has "
                            f"not exited after {EXIT_GRACE_S} s - killed")
                    proc.kill()
                    proc.wait()
                    break
                if complete_at is None and now - start >= CHILD_TIMEOUT_S:
                    status = "timeout"
                    proc.kill()
                    proc.wait()
                    break
                if now - last_output[0] >= 2 * HEARTBEAT_S:
                    last_output[0] = now
                    tee.log(f"  ... {method} still running, {now - start:.0f} s")
    finally:
        if proc.poll() is None:  # Ctrl+C etc: do not leave a session behind
            proc.kill()
            proc.wait()
    reader.join(timeout=10)

    # Judged on the result file, not the exit code: PowerFactory can return
    # a non-zero code while shutting down after a clean run.
    result = _read_result(result_path)
    if status == "ok" and not (
        result and result.get("complete") and result.get("error") is None
    ):
        status = "failed"
    return status, result


def _fmt(seconds):
    return "-" if seconds is None else f"{seconds:.2f}"


def _run_time(result, label):
    for run in (result or {}).get("runs", []):
        if run["label"] == label:
            return run["total_s"]
    return None


def _report(records, needed, tee):
    """Results table, per-method summary and equivalence check."""
    out = tee.line
    out("")
    out("=" * 78)
    out("RESULTS  (seconds; cold = first build in a fresh PowerFactory")
    out("process, warm = repeat in the same process)")
    out("=" * 78)
    out(f"{'round':<6}{'order':<6}{'method':<8}{'status':<9}"
        f"{'cold':>9}{'warm':>9}{'types':>7}  cold stages")
    for rec in records:
        res = rec["result"] or {}
        cold = next((r for r in res.get("runs", []) if r["label"] == "cold"), None)
        stages = ", ".join(
            f"{k} {v:.2f}" for k, v in (cold or {}).get("stages_s", {}).items()
        )
        out(f"{rec['round']:<6}{rec['order']:<6}{rec['method']:<8}"
            f"{rec['status']:<9}{_fmt(_run_time(res, 'cold')):>9}"
            f"{_fmt(_run_time(res, 'warm')):>9}"
            f"{(cold or {}).get('types', '-'):>7}  {stages}")

    out("")
    out("Per method (cold, each round):")
    cold_mean = {}
    for method in METHODS:
        colds = [
            _run_time(rec["result"], "cold")
            for rec in records if rec["method"] == method
        ]
        colds = [c for c in colds if c is not None]
        if colds:
            cold_mean[method] = sum(colds) / len(colds)
            out(f"  {METHOD_LABELS[method]:<32} "
                f"{', '.join(f'{c:.2f}' for c in colds)}  "
                f"(mean {cold_mean[method]:.2f} s)")
        else:
            timed_out = any(
                rec["method"] == method and rec["status"] == "timeout"
                for rec in records
            )
            out(f"  {METHOD_LABELS[method]:<32} "
                + (f"no result: over {CHILD_TIMEOUT_S} s" if timed_out
                   else "no result: failed (see log above)"))
    if "json" in cold_mean:
        for method in METHODS:
            if method != "json" and method in cold_mean:
                extra = cold_mean[method] - cold_mean["json"]
                out(f"  {METHOD_LABELS[method]} costs {extra:+.2f} s against "
                    f"the json method, once per process (the index is built "
                    f"once and reused for every project of a batch run).")

    # ---- Equivalence: does each method resolve the same types? ------------
    out("")
    out(f"Equivalence over the {len(needed)} model names mapped in "
        f"type_mapping.csv:")
    latest = {}
    for rec in records:
        if rec["result"] and "resolved" in rec["result"]:
            latest[rec["method"]] = rec["result"]
    for method in METHODS:
        res = latest.get(method)
        if not res:
            out(f"  {method}: no completed run to compare")
            continue
        resolved = res["resolved"]
        by_source = {}
        for entry in resolved.values():
            by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1
        source_text = ", ".join(f"{n} {s}" for s, n in sorted(by_source.items()))
        unresolved = sorted(needed - set(resolved))
        out(f"  {method}: {len(resolved)} resolved ({source_text}), "
            f"{len(unresolved)} unresolved")
        for name in unresolved:
            out(f"      unresolved: {name}")
        info = res["runs"][0]["info"]
        for name in info.get("stale_cache_entries", []):
            out(f"      stale path in dig_lib_paths.json: {name}")

    base = latest.get("json")
    for method in METHODS:
        other = latest.get(method)
        if method == "json" or not base or not other:
            continue
        differ = [
            name for name in sorted(needed)
            if name in base["resolved"] and name in other["resolved"]
            and base["resolved"][name]["path"] != other["resolved"][name]["path"]
        ]
        only_json = sorted(set(base["resolved"]) - set(other["resolved"]))
        only_live = sorted(set(other["resolved"]) - set(base["resolved"]))
        if not (differ or only_json or only_live):
            out(f"  json vs {method}: identical - every mapped model resolves "
                f"to the same library object")
            continue
        out(f"  json vs {method}: {len(differ)} resolve to a different object, "
            f"{len(only_json)} found by json only, {len(only_live)} found by "
            f"{method} only")
        for name in differ:
            out(f"      {name}")
            out(f"          json: {base['resolved'][name]['path']}")
            out(f"          {method}: {other['resolved'][name]['path']}")
        for name in only_json:
            out(f"      json only: {name}")
        for name in only_live:
            out(f"      {method} only: {name}")

    live = latest.get("walk") or latest.get("bulk")
    if live:
        dupes = live.get("needed_duplicates", {})
        total_dupes = live["runs"][0]["info"].get("digsilent_duplicate_names", 0)
        out(f"  DIgSILENT library names held by more than one type: "
            f"{total_dupes} in total, {len(dupes)} of them mapped models "
            f"that resolve from the DIgSILENT library")
        for name, paths in dupes.items():
            out(f"      {name}")
            for path in paths:
                out(f"          {path}")
        if dupes:
            out("      A live search takes the first one found; the json file "
                "pins one path.")
    out("=" * 78)


def parent_main():
    # Model names can hold characters the console code page lacks.
    sys.stdout.reconfigure(errors="replace")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    out_dir = Path(OUTPUT_DIR)
    log_path = out_dir / f"relay_index_probe_{stamp}.log"
    tee = _Tee(log_path)
    try:
        tee.log(f"Running {__file__} with {sys.executable}")
        tee.log(f"type_mapping.csv:   {TYPE_MAPPING_CSV}")
        tee.log(f"dig_lib_paths.json: {PATH_CACHE_JSON}")

        try:
            needed = mapped_relay_types()
        except OSError as exc:
            tee.log(f"Cannot read type_mapping.csv: {exc}")
            return 2
        if not needed:
            tee.log("type_mapping.csv yielded no model names; nothing to time")
            return 2
        cache = load_path_cache()
        tee.log(
            f"{len(needed)} mapped model name(s); path cache has "
            f"{len(cache)} entrie(s)"
        )
        if not cache and "json" in METHODS:
            tee.log(
                "WARNING: dig_lib_paths.json is missing or empty - the json "
                "method will resolve nothing from the DIgSILENT library"
            )

        records, skip = [], set()
        for round_no in range(1, ROUNDS + 1):
            order = METHODS if round_no % 2 else tuple(reversed(METHODS))
            for position, method in enumerate(order, 1):
                if method in skip:
                    tee.log(f"Round {round_no}: skipping {method} "
                            f"(did not complete in an earlier round)")
                    continue
                tee.log(f"Round {round_no}/{ROUNDS}, {position}/{len(order)}: "
                        f"{METHOD_LABELS[method]} - fresh PowerFactory process")
                result_path = out_dir / f"_probe_{stamp}_r{round_no}_{method}.json"
                status, result = _run_child(method, result_path, tee)
                if status != "ok":
                    skip.add(method)
                    tee.log(f"  {method}: {status}"
                            + (f" after {CHILD_TIMEOUT_S} s - killed"
                               if status == "timeout" else ""))
                records.append({
                    "round": round_no, "order": position, "method": method,
                    "status": status, "result": result,
                })
                try:
                    os.remove(result_path)
                except OSError:
                    pass

        _report(records, needed, tee)

        results_path = out_dir / f"relay_index_probe_{stamp}.json"
        _write_json(str(results_path), {
            "stamp": stamp,
            "methods": list(METHODS),
            "rounds": ROUNDS,
            "child_timeout_s": CHILD_TIMEOUT_S,
            "mapped_models": sorted(needed),
            "records": records,
        })
        tee.log(f"Log:     {log_path}")
        tee.log(f"Results: {results_path}")
        return 0 if all(rec["status"] == "ok" for rec in records) else 1
    finally:
        tee.close()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        sys.exit(child_main(sys.argv[2], sys.argv[3]))
    sys.exit(parent_main())