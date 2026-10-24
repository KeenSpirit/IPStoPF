"""
Run the switch matcher (mapping/switch_matcher.py) inside IPStoPF.

PowerFactory runtime module. It only reads the model: switch names, their
containers, terminal voltages and the relays/fuses already in each cubicle.
ex_settings decides what to do with the result (see config/switch_matching.py
for the legacy / shadow / new modes).

It also writes the per-project match log: one row per switch in the project,
with the same switch columns as export_pf_switches.py plus how the switch
matched. switch_match_audit.py reads a folder of these for the fleet audit.
"""

import csv
import dataclasses
import os
import time
from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from config import switch_matching as smc
from incremental import recorder as run_recorder
from logging_config import get_logger
from mapping import switch_matcher as sm

logger = get_logger(__name__)

SUBSTATION_CLASSES = ("ElmSubstat", "ElmTrfstat", "ElmSite")

# Switch columns, as export_pf_switches.py writes them.
SWITCH_COLUMNS = [
    "export_timestamp", "project", "project_full_name", "study_case", "class",
    "loc_name", "full_name", "fold_id_name", "fold_id_class", "aUsage",
    "on_off", "out_of_service", "energised", "calc_relevant", "grid",
    "valid_switch", "should_process", "cubicle_name",
    "terminal_name", "terminal_kv", "substation", "substation_class",
    "relays", "fuses",
]
MATCH_COLUMNS = [
    "match_mode", "match_status", "match_rule", "ips_names", "setting_ids",
    "legacy_setting_ids", "rejections", "match_note",
]


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        value = fn()
    except Exception:
        return default
    return default if value is None else value


def switch_cubicle(switch: Any) -> Any:
    """The cubicle IPStoPF puts a switch's relays in (as ex_settings does)."""
    if switch.GetClassName() == "StaSwitch":
        return switch.fold_id
    return switch.GetCubicle(0)


def _containing_substation(terminal: Any) -> Any:
    obj = _safe(lambda: terminal.fold_id)
    for _ in range(6):
        if obj is None:
            return None
        cls = _safe(obj.GetClassName, "")
        if cls in SUBSTATION_CLASSES:
            return obj
        if cls in ("ElmNet", "IntPrj", "IntPrjfolder", ""):
            return None
        obj = _safe(lambda: obj.fold_id)
    return None


def _devices_in(cubicle: Any, pattern: str) -> str:
    if cubicle is None:
        return ""
    items = []
    for dev in _safe(lambda: cubicle.GetContents(pattern), []):
        typ = _safe(lambda: dev.typ_id)
        items.append(f"{dev.loc_name} [{typ.loc_name if typ else 'no type'}]")
    return "; ".join(items)


def describe_switch(
    switch: Any,
    base: Dict[str, str],
    valid: bool,
    should_process: bool,
) -> Tuple[sm.SwitchRef, Dict[str, Any]]:
    """
    The matcher's view of one switch, and its match-log row.

    Args:
        switch: StaSwitch or ElmCoup.
        base: Columns shared by every row of the project.
        valid: Passed ex_settings._get_valid_switches.
        should_process: Passed ex_settings._should_process_switch.
    """
    cls = switch.GetClassName()
    name = switch.loc_name
    fold = _safe(lambda: switch.fold_id)
    fold_name = _safe(lambda: fold.loc_name, "")
    fold_class = _safe(lambda: fold.GetClassName(), "")
    cubicle = _safe(lambda: switch_cubicle(switch))
    terminal = _safe(lambda: cubicle.cterm) if cubicle is not None else None
    kv = _safe(lambda: float(terminal.GetAttribute("uknom"))) if terminal else None
    terminal_name = _safe(lambda: terminal.loc_name, "") if terminal else ""
    sub = _containing_substation(terminal) if terminal is not None else None
    sub_name = _safe(lambda: sub.loc_name, "") if sub is not None else ""
    full_name = _safe(switch.GetFullName, f"{fold_name}\\{name}")

    if cls == "ElmCoup":
        container, container_class, substation = fold_name, fold_class, None
    else:
        container, container_class = terminal_name, "ElmTerm"
        substation = sub_name or None

    ref = sm.SwitchRef(
        key=full_name,
        project=base.get("project", ""),
        cls=cls,
        name=name,
        container=container,
        container_class=container_class,
        fold_name=fold_name,
        kv=kv,
        substation=substation,
    )
    row = dict(base)
    row.update({
        "class": cls,
        "loc_name": name,
        "full_name": full_name,
        "fold_id_name": fold_name,
        "fold_id_class": fold_class,
        "aUsage": _safe(lambda: switch.GetAttribute("aUsage"), ""),
        "on_off": _safe(lambda: switch.GetAttribute("on_off"), ""),
        "out_of_service": int(bool(_safe(switch.IsOutOfService, 0))),
        "energised": int(bool(_safe(switch.IsEnergized, 0))),
        "calc_relevant": int(bool(_safe(switch.IsCalcRelevant, 0))),
        "grid": _safe(lambda: switch.GetAttribute("cpGrid").loc_name, ""),
        "valid_switch": int(valid),
        "should_process": int(should_process),
        "cubicle_name": _safe(lambda: cubicle.loc_name, "") if cubicle else "",
        "terminal_name": terminal_name,
        "terminal_kv": "" if kv is None else kv,
        "substation": sub_name,
        "substation_class": _safe(lambda: sub.GetClassName(), "") if sub else "",
        "relays": _devices_in(cubicle, "*.ElmRelay"),
        "fuses": _devices_in(cubicle, "*.RelFuse"),
    })
    return ref, row


OVERRIDES_LABEL = "switch_match_overrides.csv"


def _rules() -> set:
    rules = (set(sm.DEFAULT_RULES) | set(smc.ENABLE_RULES)) - set(smc.DISABLE_RULES)
    unknown = rules - sm.ALL_RULES
    if unknown:
        logger.warning(f"Unknown switch-matching rule(s) ignored: {sorted(unknown)}")
    return rules & sm.ALL_RULES


def _cb_alt_name_index() -> Dict[Tuple[str, str], str]:
    """CB_ALT_NAME.csv as ex_settings.cb_alt_name_index builds it."""
    from ips_data.cb_mapping import get_cb_alt_name_list
    index: Dict[Tuple[str, str], str] = {}
    for row in get_cb_alt_name_list():
        index.setdefault((row.get("SUBSTATION"), row.get("CB_NAME")), row.get("NEW_NAME"))
    return index


def matcher_for(setting_index: Any) -> sm.SwitchMatcher:
    """
    The SwitchMatcher over a SettingIndex, built once and kept on it.

    The run and the incremental precheck share the cached SettingIndex, so
    they also share this matcher (and the files it was built from).
    """
    matcher = getattr(setting_index, "_switch_matcher", None)
    if matcher is not None:
        run_recorder.file_used(run_recorder.SHARED_FILE, OVERRIDES_LABEL)
        _cb_alt_name_index()            # reports CB_ALT_NAME.csv use
        return matcher
    path = smc.get_overrides_file()
    run_recorder.file_loaded(run_recorder.SHARED_FILE, OVERRIDES_LABEL, path)
    overrides, problems = sm.load_overrides(str(path))
    for p in problems:
        logger.warning(f"{OVERRIDES_LABEL} {p}; row ignored")
    matcher = sm.SwitchMatcher(
        setting_index.records, overrides=overrides,
        cb_alt_names=_cb_alt_name_index(), rules=_rules(),
    )
    setting_index._switch_matcher = matcher
    return matcher


def propose_records(setting_index: Any, *ref_args: Any) -> List[Any]:
    """
    Records the matcher proposes for one switch, from SwitchRef fields
    (project, cls, name, container, fold_name, kv, substation). Backs
    SettingIndex.get_switch_match, which the incremental precheck replays.
    """
    project, cls, name, container, fold_name, kv, substation = ref_args
    ref = sm.SwitchRef(
        key="", project=project, cls=cls, name=name, container=container,
        fold_name=fold_name or "", kv=kv, substation=substation,
    )
    return matcher_for(setting_index).propose(ref).records


def _lookup_args(ref: sm.SwitchRef) -> Tuple[Any, ...]:
    return (ref.project, ref.cls, ref.name, ref.container, ref.fold_name,
            ref.kv, ref.substation)


class ProjectSwitchMatching:
    """
    The new matcher's results for one project, keyed by switch full name.

    Args:
        app: PowerFactory application.
        prjt: Active project.
        setting_index: The run's SettingIndex.
        batch: True in a batch run (decides where the match log goes).
        record_lookups: True when these results drive the model ("new"
            mode): every processed switch's lookup is then recorded for the
            incremental precheck, as the legacy lookups are.
    """

    def __init__(self, app, prjt, setting_index, batch: bool,
                 record_lookups: bool = False):
        self.app = app
        self.prjt = prjt
        self.batch = batch
        self.setting_index = setting_index
        self.record_lookups = record_lookups
        self.matcher = matcher_for(setting_index)
        self.by_key: Dict[str, sm.SwitchMatch] = {}
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.legacy: Dict[str, List[str]] = {}
        self.order: List[str] = []

    @staticmethod
    def _key(switch: Any) -> Optional[str]:
        return _safe(switch.GetFullName)

    def match(
        self,
        all_switches: Sequence[Any],
        valid: Iterable[Any],
        should_process: Callable[[Any], bool],
    ) -> None:
        """
        Describe every switch and match them.

        Processed switches (valid and should_process) are matched together,
        so the claim rules see the whole project; filtered switches are
        matched separately, for the log only. Must run before
        _handle_unmatched_switch deletes anything, so the log records the
        cubicle contents as found.
        """
        t0 = time.monotonic()
        valid_keys = {self._key(s) for s in valid}
        case = _safe(self.app.GetActiveStudyCase)
        base = {
            "export_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "project": self.prjt.loc_name,
            "project_full_name": _safe(self.prjt.GetFullName, ""),
            "study_case": _safe(lambda: case.loc_name, "") if case else "",
        }
        processed_refs, filtered_refs = [], []
        skipped = 0
        for switch in all_switches:
            key = self._key(switch)
            if key is None or key in self.rows:
                skipped += 1
                continue
            is_valid = key in valid_keys
            passes = bool(_safe(lambda: should_process(switch), False))
            try:
                ref, row = describe_switch(switch, base, is_valid, passes)
            except Exception as e:
                # The switch then has no result; ex_settings uses the legacy
                # lookup for it.
                skipped += 1
                logger.warning(
                    f"Switch matching: could not read '{_safe(lambda: switch.loc_name, '?')}' "
                    f"({e!r}); legacy lookup used for it"
                )
                continue
            ref = dataclasses.replace(ref, key=key)
            self.rows[key] = row
            self.order.append(key)
            (processed_refs if is_valid and passes else filtered_refs).append(ref)

        for refs in (processed_refs, filtered_refs):
            for ref, result in zip(refs, self.matcher.match_all(refs)):
                self.by_key[ref.key] = result

        if self.record_lookups:
            for ref in processed_refs:
                self.setting_index.get_switch_match(*_lookup_args(ref))

        logger.info(
            f"Switch matching: {len(processed_refs)} processed and "
            f"{len(filtered_refs)} filtered switch(es) matched in "
            f"{time.monotonic() - t0:.1f} s"
            + (f" ({skipped} not read)" if skipped else "")
        )

    def result_for(self, switch: Any) -> Optional[sm.SwitchMatch]:
        key = self._key(switch)
        return self.by_key.get(key) if key is not None else None

    def records_for(self, switch: Any) -> Optional[List[Any]]:
        """
        The records for a switch, or None when the matcher has no result
        for it (the caller then uses the legacy lookup).
        """
        result = self.result_for(switch)
        return result.records if result is not None else None

    def is_legacy_record(self, switch: Any, setting_id: str) -> bool:
        result = self.result_for(switch)
        return bool(result is not None and result.is_legacy_entry(setting_id))

    def note_legacy(self, switch: Any, setting_ids: List[str]) -> None:
        """Shadow mode: record what the legacy lookup produced."""
        try:
            self._note_legacy(switch, setting_ids)
        except Exception:
            logger.warning("Switch matching: shadow comparison failed", exc_info=True)

    def _note_legacy(self, switch: Any, setting_ids: List[str]) -> None:
        key = self._key(switch)
        if key is None:
            return
        self.legacy[key] = list(setting_ids)
        result = self.by_key.get(key)
        if result is None:
            return
        new_ids = result.setting_ids
        if new_ids == list(setting_ids):
            return
        logger.info(
            f"Switch match [shadow] '{switch.loc_name}': legacy "
            f"{len(setting_ids)} record(s), new {len(new_ids)} "
            f"({result.status}"
            + (f", {result.rule} via '{result.pf_name}'" if result.rule else "")
            + (f"; {result.rejections[-1].reason}" if result.rejections and not new_ids else "")
            + ")"
        )

    def finish(self, write_log: bool = True) -> Optional[str]:
        """Log the project summary and, optionally, write the match log."""
        processed = [k for k in self.order if self.rows[k]["should_process"]
                     and self.rows[k]["valid_switch"]]
        status = Counter(self.by_key[k].status for k in processed if k in self.by_key)
        rules = Counter(self.by_key[k].rule for k in processed
                        if k in self.by_key and self.by_key[k].status == sm.MATCHED)
        msg = (
            f"Switch matching ({smc.MODE}): {len(processed)} processed switch(es): "
            + ", ".join(f"{k} {v}" for k, v in sorted(status.items()))
            + "; rules: " + ", ".join(f"{k} {v}" for k, v in rules.most_common())
        )
        if self.legacy:
            same = sum(
                1 for k, ids in self.legacy.items()
                if k in self.by_key and self.by_key[k].setting_ids == ids
            )
            msg += f"; same as legacy for {same} of {len(self.legacy)}"
        problems = [self.by_key[k].override_problem for k in processed
                    if k in self.by_key and self.by_key[k].override_problem]
        for problem in problems:
            logger.warning(f"Switch matching: {problem}")
        logger.info(msg)
        if not (write_log and smc.WRITE_MATCH_LOG):
            return None
        try:
            return self._write_log()
        except Exception:
            logger.warning("Switch matching: could not write the match log", exc_info=True)
            return None

    def _write_log(self) -> str:
        parent = _safe(lambda: self.prjt.fold_id.loc_name, "")
        name = f"switch_match_{parent}_{self.prjt.loc_name}.csv".replace("/", "_")
        path = os.path.join(str(smc.get_match_log_dir(self.batch)), name)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f, fieldnames=SWITCH_COLUMNS + MATCH_COLUMNS,
                quoting=csv.QUOTE_ALL, extrasaction="ignore",
            )
            writer.writeheader()
            for key in self.order:
                row = dict(self.rows[key])
                result = self.by_key.get(key)
                row["match_mode"] = smc.MODE
                if result is not None:
                    row.update({
                        "match_status": result.status,
                        "match_rule": result.rule,
                        "ips_names": "; ".join(sorted({e.name for e in result.entries})),
                        "setting_ids": "; ".join(result.setting_ids),
                        "rejections": " | ".join(
                            f"{r.pf_rule} '{r.pf_name}': {r.reason}"
                            for r in result.rejections
                        ),
                        "match_note": result.note,
                    })
                if key in self.legacy:
                    row["legacy_setting_ids"] = "; ".join(self.legacy[key])
                writer.writerow(row)
        logger.info(f"Switch match log: {path}")
        return path
