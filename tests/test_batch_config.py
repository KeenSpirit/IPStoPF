"""Batch validation and the ODS fallback policy."""

import pytest

from config import validation as v
from ips_data import query_database as qd


def test_batch_validation_does_not_test_netdash(monkeypatch):
    called = []
    monkeypatch.setattr(v, "_validate_database", lambda *a: called.append(1))
    monkeypatch.setattr(v, "_batch_validation", None)
    monkeypatch.setattr(v, "validate_startup",
                        lambda app, config: (called.append(config.check_database)
                                             or v.ValidationResult()))
    v.validate_for_batch_mode(None)
    assert called == [False]


def test_batch_validation_cached_once_valid(monkeypatch):
    calls = []
    monkeypatch.setattr(v, "_batch_validation", None)
    monkeypatch.setattr(v, "validate_startup",
                        lambda app, config: calls.append(1) or v.ValidationResult())
    first = v.validate_for_batch_mode(None)
    second = v.validate_for_batch_mode(None)
    assert first is second and calls == [1]


def test_invalid_batch_validation_not_cached(monkeypatch):
    calls = []

    def failing(app, config):
        calls.append(1)
        result = v.ValidationResult()
        result.add_error("missing type_mapping.csv")
        return result

    monkeypatch.setattr(v, "_batch_validation", None)
    monkeypatch.setattr(v, "validate_startup", failing)
    v.validate_for_batch_mode(None)
    v.validate_for_batch_mode(None)
    assert calls == [1, 1]


class _App:
    def PrintError(self, msg):
        pass


def test_ods_outage_fails_loudly_instead_of_netdash(monkeypatch):
    def unavailable(region):
        raise qd.ods_connection.ODSUnavailable("no client")

    monkeypatch.setattr(qd.ods_connection, "connect_to_db", unavailable)
    monkeypatch.setattr(qd, "_fetch_settings_in_batches",
                        lambda *a: pytest.fail("NetDash fallback used"))
    with pytest.raises(qd.TransferError):
        qd.batch_settings(_App(), "Ergon", True, ["S1"])


def test_both_batch_sqls_filter_null_actuals_server_side():
    for sql in (qd.ENERGEX_BATCH_SQL, qd.ERGON_BATCH_SQL):
        assert "actual is not null" in " ".join(sql.lower().split())


def test_it_report_read_once_per_process(monkeypatch):
    from types import SimpleNamespace as Row
    reads = []

    def fake(report, max_age):
        reads.append(report)
        return iter([Row(relaysettingid="S1"), Row(relaysettingid="S2")])

    monkeypatch.setattr(qd, "get_cached_data", fake)
    monkeypatch.setattr(qd, "_it_report_cache", {})
    assert len(qd.reg_get_ips_it_details(None, ["S1"])) == 1
    assert len(qd.reg_get_ips_it_details(None, ["S1", "S2"])) == 2
    assert len(qd.seq_get_ips_it_details(None, ["S2"])) == 1
    assert reads == ["Report-Cache-ProtectionITSettings-EE",
                     "Report-Cache-ProtectionITSettings-EX"]
