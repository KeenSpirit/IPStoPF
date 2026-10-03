"""Smoke test: the modules touched by the review import offline."""

import importlib

import pytest


@pytest.mark.parametrize("name", [
    "core.protection_device",
    "core.update_result",
    "update_powerfactory.relay_settings",
    "update_powerfactory.relay_reclosing",
    "update_powerfactory.fuse_settings",
    "update_powerfactory.ct_settings",
    "update_powerfactory.vt_settings",
    "update_powerfactory.pu_base_check",
    "update_powerfactory.setting_utils",
    "ips_data.query_database",
    "ips_data.ex_settings",
    "ips_data.ee_settings",
    "ips_data.add_relay_skeletons",
    "utils.pf_utils",
    "utils.file_utils",
])
def test_imports(name):
    importlib.import_module(name)
