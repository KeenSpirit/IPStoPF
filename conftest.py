"""
Offline pytest harness.

The pure modules are tested without PowerFactory, Oracle or the corporate
network libraries. A few PF-runtime modules are still worth testing for
their pure helpers, but they import NetDash-Reader / AssetClasses /
tenacity / powerfactory at module level. Those imports are satisfied here
with inert stand-ins ONLY when the real package is not importable, so on a
Citrix machine with the real libraries nothing is replaced.

The run-log folder is redirected to a temporary directory so importing
logging_config during a test run never creates a "C:\\LocalData\\..."
folder in the working directory on a non-Windows machine.
"""

import importlib
import sys
import tempfile
import types


def _stub(name: str, **attrs) -> None:
    try:
        importlib.import_module(name)
        return
    except Exception:
        pass
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


def _passthrough_decorator(*_args, **_kwargs):
    def wrap(func):
        return func
    return wrap


def _unavailable(*_args, **_kwargs):
    raise RuntimeError("not available in the offline test harness")


_stub("netdashread", get_json_data=_unavailable)
_stub("assetclasses")
_stub("assetclasses.corporate_data", get_cached_data=_unavailable)
_stub(
    "tenacity",
    retry=_passthrough_decorator,
    stop_after_attempt=lambda *a, **k: None,
    wait_random_exponential=lambda *a, **k: None,
    wait_fixed=lambda *a, **k: None,
    retry_if_exception_type=lambda *a, **k: None,
    before_sleep_log=lambda *a, **k: None,
    RetryError=Exception,
)
_stub("powerfactory")

import config.paths as _paths  # noqa: E402

_paths.PROTECTION_BATCH_OUTPUT_DIR = tempfile.mkdtemp(prefix="ipstopf_test_")
