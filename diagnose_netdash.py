"""
diagnose_netdash.py - locate and characterise the getdata.py ConnectionError.

Context
-------
The 22/08 batch log shows, once, before determine_region:

    query.py:   86:  netdashreader is no longer supported
    getdata.py: 48:  ConnectionError: could not connect to http://eq09808/netdashapi/

This module answers four questions, in the order that determines what the
fix is:

    A. WHERE does the message come from, and does that code path RAISE or
       swallow the error and return empty? (Raise = loud failure. Swallow =
       silent wrong data, and config.validation._validate_database will
       report "OK (reachable)" while the database is down.)
    B. Is the host reachable at all - DNS, TCP, HTTP - independent of
       netdashread? Proxy environment variables are included because
       `requests` honours them and a proxy is a classic cause of a
       connect failure that looks like a dead host.
    C. What does get_json_data actually do right now: raise, or return []?
    D. WHO called it in the real run? Phase D installs a logging handler
       that dumps a Python stack whenever netdashread logs, so the caller
       is identified rather than inferred.

Phases A-C are read-only and need neither PowerFactory nor an active
project. Phase D can be run standalone (it exercises the suspected
validation path) or installed into a real batch run - see
`install_netdash_stack_tracer` at the bottom.

Usage
-----
    cd /d <IPStoPF repo root>
    "C:\\Program Files\\Python312\\python.exe" diagnose_netdash.py

Exit codes: 0 = netdashread reachable and returning data; 1 = a probe
failed (see SUMMARY); 2 = could not import netdashread at all.
"""

import logging
import os
import re
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The URL from the log. Phase A tries to find the real one in the source;
# this is only the fallback for the connectivity probe.
FALLBACK_URL = "http://eq09808/netdashapi/"

# Mirrors config.validation._validate_database exactly, so phase C
# reproduces the call the batch run makes at startup.
PROBE_REPORT = "Protection-SettingRelay-EX"
PROBE_PARAMS = {"setting_id": "__CONNECTIVITY_TEST__"}
PROBE_TIMEOUT = 30  # validate_for_batch_mode uses timeout_seconds=30

log = logging.getLogger("netdash_diag")


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"
    ))
    root.addHandler(h)


def banner(text):
    log.info("")
    log.info("=" * 72)
    log.info(text)
    log.info("=" * 72)


# ===========================================================================
# Phase A - where does the message come from, and does it raise?
# ===========================================================================

def show_source(path, lineno, context=12):
    """Print the numbered source around a line so raise-vs-return is visible."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        log.error(f"cannot read {path}: {exc}")
        return None
    lo = max(0, lineno - context)
    hi = min(len(lines), lineno + context)
    log.info(f"--- {path} lines {lo + 1}-{hi} ---")
    for i in range(lo, hi):
        marker = ">>" if i + 1 == lineno else "  "
        log.info(f"{marker} {i + 1:>4} | {lines[i]}")
    return lines


def phase_a():
    banner("PHASE A - source of the message")

    from config.paths import NETDASH_READER_PATH
    log.info(f"NETDASH_READER_PATH: {NETDASH_READER_PATH}")
    if NETDASH_READER_PATH not in sys.path:
        sys.path.append(NETDASH_READER_PATH)

    try:
        import netdashread
    except ImportError as exc:
        log.error(f"cannot import netdashread: {exc}")
        return None, False

    pkg_dir = Path(netdashread.__file__).parent
    log.info(f"netdashread package: {pkg_dir}")
    log.info(f"version: {getattr(netdashread, '__version__', '<none>')}")

    findings = {"raises": None, "urls": set()}

    for name, lineno in (("getdata.py", 48), ("query.py", 86)):
        path = pkg_dir / name
        if not path.exists():
            log.warning(f"{name} not found in the package directory")
            continue
        lines = show_source(path, lineno)
        if lines is None or name != "getdata.py":
            continue
        # Does the handler around line 48 re-raise, or return/pass?
        window = "\n".join(lines[max(0, 48 - 12):48 + 12])
        raises = bool(re.search(r"\braise\b", window))
        findings["raises"] = raises
        if raises:
            log.info("getdata.py appears to RAISE near line 48 - callers see "
                     "the failure.")
        else:
            log.error(
                "No 'raise' near getdata.py:48 - the error is likely LOGGED "
                "AND SWALLOWED, returning an empty result. If so, "
                "config.validation._validate_database records "
                "'OK (reachable)' while the database is unreachable, and "
                "query_database.get_data returns [] rather than retrying."
            )

    # Where is the host configured? If NetDash moved, this is what to edit.
    log.info("")
    log.info("scanning the package for the API host...")
    for py in sorted(pkg_dir.rglob("*.py")):
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r"https?://[^\s'\"),]+", text):
            findings["urls"].add(match.group())
        for kw in ("eq09808", "netdashapi", "BASE_URL", "HOST"):
            for i, line in enumerate(text.splitlines(), 1):
                if kw in line and not line.strip().startswith("#"):
                    log.info(f"    {py.name}:{i}: {line.strip()[:120]}")
    if findings["urls"]:
        log.info(f"URLs found in source: {sorted(findings['urls'])}")

    return findings, True


# ===========================================================================
# Phase B - raw reachability, independent of netdashread
# ===========================================================================

def phase_b(findings):
    banner("PHASE B - raw connectivity")

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "no_proxy"):
        value = os.environ.get(var)
        if value:
            log.info(f"{var} = {value}")
    if not any(os.environ.get(v) for v in ("HTTP_PROXY", "http_proxy")):
        log.info("no HTTP proxy environment variables set")

    urls = sorted(findings["urls"]) if findings and findings["urls"] else []
    candidates = [u for u in urls if "netdash" in u.lower()] or [FALLBACK_URL]
    ok_any = False

    for url in candidates:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        log.info("")
        log.info(f"probing {url}  (host {host}, port {port})")

        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            addrs = sorted({i[4][0] for i in infos})
            log.info(f"    DNS   OK -> {addrs}")
        except socket.gaierror as exc:
            log.error(f"    DNS   FAILED: {exc}  <-- name does not resolve; "
                      f"the host may have been decommissioned or renamed")
            continue

        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=10):
                log.info(f"    TCP   OK in {time.perf_counter() - t0:.1f}s")
        except OSError as exc:
            log.error(f"    TCP   FAILED after {time.perf_counter() - t0:.1f}s"
                      f": {exc}  <-- resolves but nothing is listening, or a "
                      f"firewall is dropping it")
            continue

        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read(400)
                log.info(f"    HTTP  {resp.status} in "
                         f"{time.perf_counter() - t0:.1f}s")
                log.info(f"    body[:200]: {body[:200]!r}")
                ok_any = True
        except urllib.error.HTTPError as exc:
            # A 4xx still proves the service is answering.
            log.info(f"    HTTP  {exc.code} {exc.reason} - service is "
                     f"responding")
            ok_any = True
        except Exception as exc:  # noqa: BLE001
            log.error(f"    HTTP  FAILED after "
                      f"{time.perf_counter() - t0:.1f}s: "
                      f"{type(exc).__name__}: {exc}")

    return ok_any


# ===========================================================================
# Phase C - what does get_json_data actually do?
# ===========================================================================

class _RecordCounter(logging.Handler):
    """Count records emitted by the netdashread modules."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        if record.filename in ("getdata.py", "query.py"):
            self.records.append(record)


def phase_c():
    banner("PHASE C - get_json_data behaviour")

    from netdashread import get_json_data

    counter = _RecordCounter()
    logging.getLogger().addHandler(counter)
    t0 = time.perf_counter()
    raised, result = None, None
    try:
        result = get_json_data(
            report=PROBE_REPORT, params=PROBE_PARAMS, timeout=PROBE_TIMEOUT
        )
        result = list(result) if result is not None else None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    finally:
        elapsed = time.perf_counter() - t0
        logging.getLogger().removeHandler(counter)

    log.info(f"elapsed: {elapsed:.1f}s")
    log.info(f"netdashread log records emitted: {len(counter.records)}")
    for record in counter.records:
        log.info(f"    {record.filename}:{record.lineno} "
                 f"{record.levelname}: {record.getMessage()}")

    if raised is not None:
        log.error(f"RAISED {type(raised).__name__}: {raised}")
        log.info("Interpretation: callers see a hard failure. get_data's "
                 "tenacity retry will make three attempts and then "
                 "propagate. _validate_database will record a warning, not "
                 "a false OK.")
        return False

    log.info(f"returned: {type(result).__name__}, "
             f"{len(result) if result is not None else 'None'} row(s)")
    if not result:
        log.error(
            "Returned EMPTY without raising. This is the silent-wrong-data "
            "case: _validate_database records 'OK (reachable)', get_data "
            "logs 'Query returned no data' and returns [], and every relay "
            "silently gets no settings. Note the probe uses a deliberately "
            "invalid setting_id, so empty is also the CORRECT answer here - "
            "the log records above are what distinguish the two. A "
            "ConnectionError line means the emptiness is a failure."
        )
        # A connection message alongside an empty return is the giveaway.
        connection_failed = any(
            "connect" in r.getMessage().lower() for r in counter.records
        )
        return not connection_failed

    log.info("Returned rows for a deliberately invalid id - unexpected, but "
             "the service is clearly answering.")
    return True


# ===========================================================================
# Phase D - who is calling it?
# ===========================================================================

class NetdashStackTracer(logging.Handler):
    """Dump a Python stack whenever netdashread logs a connection problem.

    The batch log gives a filename and line number but no caller, so the
    origin of the single ConnectionError has to be inferred from timing.
    This removes the guesswork.
    """

    def __init__(self, match="connect"):
        super().__init__(level=logging.DEBUG)
        self.match = match.lower()
        self.hits = 0

    def emit(self, record):
        if record.filename not in ("getdata.py", "query.py"):
            return
        if self.match not in record.getMessage().lower():
            return
        self.hits += 1
        print(f"\n--- netdash stack trace #{self.hits} "
              f"({record.filename}:{record.lineno}) ---")
        # Trim the logging machinery off the top of the stack.
        for line in traceback.format_stack()[:-8]:
            print(line.rstrip())
        print("--- end stack trace ---\n")


def phase_d():
    banner("PHASE D - caller identification")

    tracer = NetdashStackTracer()
    logging.getLogger().addHandler(tracer)
    try:
        # Reproduce the suspected startup path directly.
        from config.validation import (
            ValidationResult, ValidationConfig, ValidationLevel,
            _validate_database,
        )
        result = ValidationResult()
        config = ValidationConfig(
            level=ValidationLevel.FULL, check_database=True,
            timeout_seconds=PROBE_TIMEOUT,
        )
        _validate_database(result, config)
        log.info(f"_validate_database -> info={result.info} "
                 f"warnings={result.warnings}")
        if result.info.get("database:netdash") == "OK (reachable)" and tracer.hits:
            log.error(
                "CONFIRMED FALSE PASS: _validate_database reported "
                "'OK (reachable)' in the same call that logged a connection "
                "failure. Batch runs will proceed as though the database is "
                "up."
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(f"could not exercise _validate_database: {exc}")
    finally:
        logging.getLogger().removeHandler(tracer)

    log.info(f"stack traces captured: {tracer.hits}")
    if not tracer.hits:
        log.info("No connection message from this path. The batch-log "
                 "ConnectionError came from somewhere else - install the "
                 "tracer in the real run (see install_netdash_stack_tracer).")
    return tracer.hits


def install_netdash_stack_tracer():
    """Attach the tracer to the root logger for a real batch run.

    Add to ips_to_pf_batch.py immediately after logging is configured:

        from diagnose_netdash import install_netdash_stack_tracer
        install_netdash_stack_tracer()

    Every netdashread connection message then prints a full stack to
    stdout, naming the caller. Remove once the origin is known - it is a
    diagnostic, not a permanent handler.
    """
    tracer = NetdashStackTracer()
    logging.getLogger().addHandler(tracer)
    return tracer


# ===========================================================================
# Driver
# ===========================================================================

def main():
    setup_logging()
    log.info(f"netdash diagnostic - {Path(__file__).resolve()}")
    log.info(f"interpreter: {sys.executable}")

    findings, imported = phase_a()
    if not imported:
        return 2

    outcomes = {
        "connectivity": phase_b(findings),
        "get_json_data": phase_c(),
    }
    phase_d()

    banner("SUMMARY")
    for name, ok in outcomes.items():
        log.info(f"    {name:<16} {'PASS' if ok else 'FAIL'}")
    if findings.get("raises") is False:
        log.warning("    getdata.py appears to swallow connection errors - "
                    "treat any PASS above with suspicion.")
    failed = [n for n, ok in outcomes.items() if not ok]
    if failed:
        log.error(f"FAILED: {', '.join(failed)}")
        return 1
    log.info("NetDash appears reachable and answering.")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:  # noqa: BLE001
        logging.getLogger("netdash_diag").exception("diagnostic aborted")
        rc = 2
    sys.exit(rc)