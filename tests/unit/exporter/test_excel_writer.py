"""Unit tests for lambda/exporter/excel_writer.py"""
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

from excel_writer import (
    write_excel,
    current_open_arns,
    _build_dataframes,
    _compute_delta,
    _build_delta_log,
    _table_addr,
)


def _event(arn="arn::1", org_id="o-aaa", status="open", service="EC2",
           region="us-east-1", category="issue"):
    return {
        "event_arn": arn,
        "org_id": org_id,
        "org_name": f"Org {org_id}",
        "category": category,
        "service": service,
        "event_type_code": "AWS_EC2_ISSUE",
        "region": region,
        "status": status,
        "severity": "standard",
        "is_operational": True,
        "start_time": "2026-01-01T12:00:00+00:00",
        "last_updated_time": "2026-01-01T13:00:00+00:00",
        "affected_account_count": 1,
        "affected_accounts": [
            {"account_id": "111", "name": "Acct1", "env": "production", "bu": "Eng"}
        ],
    }


# ── current_open_arns ─────────────────────────────────────────────────────────

class TestCurrentOpenArns:
    def test_returns_open_arns(self):
        events = [_event("arn::1", status="open"), _event("arn::2", status="closed")]
        result = current_open_arns(events)
        assert "arn::1" in result
        assert "arn::2" not in result

    def test_deduplicates(self):
        events = [_event("arn::1", org_id="o-a"), _event("arn::1", org_id="o-b")]
        result = current_open_arns(events)
        assert result.count("arn::1") == 1

    def test_empty_events(self):
        assert current_open_arns([]) == []

    def test_all_closed(self):
        events = [_event(status="closed")]
        assert current_open_arns(events) == []

    def test_skips_events_without_arn(self):
        events = [{"status": "open"}]  # no event_arn key
        assert current_open_arns(events) == []


# ── _build_dataframes ─────────────────────────────────────────────────────────

class TestBuildDataframes:
    def test_returns_two_dataframes(self):
        events_df, entities_df = _build_dataframes([_event()])
        assert isinstance(events_df, pd.DataFrame)
        assert isinstance(entities_df, pd.DataFrame)

    def test_events_df_has_one_row(self):
        events_df, _ = _build_dataframes([_event()])
        assert len(events_df) == 1

    def test_entities_df_has_one_row_per_account(self):
        ev = _event()
        ev["affected_accounts"] = [
            {"account_id": "111", "name": "A", "env": "prod", "bu": "Eng"},
            {"account_id": "222", "name": "B", "env": "non-prod", "bu": "Sales"},
        ]
        _, entities_df = _build_dataframes([ev])
        assert len(entities_df) == 2

    def test_empty_events_returns_empty_df(self):
        events_df, entities_df = _build_dataframes([])
        assert events_df.empty
        assert entities_df.empty

    def test_deduplicates_by_arn_and_org(self):
        # Two identical items should deduplicate to one
        events_df, _ = _build_dataframes([_event(), _event()])
        assert len(events_df) == 1

    def test_two_orgs_same_arn_kept(self):
        events = [_event("arn::1", "o-aaa"), _event("arn::1", "o-bbb")]
        events_df, _ = _build_dataframes(events)
        assert len(events_df) == 2

    def test_events_df_columns(self):
        events_df, _ = _build_dataframes([_event()])
        for col in ("event_arn", "service", "status", "region", "category"):
            assert col in events_df.columns

    def test_entities_df_columns(self):
        _, entities_df = _build_dataframes([_event()])
        for col in ("event_arn", "account_id", "org_id"):
            assert col in entities_df.columns


# ── _compute_delta ────────────────────────────────────────────────────────────

class TestComputeDelta:
    def test_new_open_event(self):
        events_df, _ = _build_dataframes([_event("arn::1", status="open")])
        delta_new, delta_resolved = _compute_delta(events_df, [])
        assert "arn::1" in delta_new["event_arn"].values

    def test_resolved_event(self):
        events_df, _ = _build_dataframes([_event("arn::1", status="closed")])
        delta_new, delta_resolved = _compute_delta(events_df, ["arn::1"])
        assert "arn::1" in delta_resolved["event_arn"].values

    def test_unchanged_open_not_in_delta(self):
        events_df, _ = _build_dataframes([_event("arn::1", status="open")])
        delta_new, delta_resolved = _compute_delta(events_df, ["arn::1"])
        assert "arn::1" not in delta_new["event_arn"].values
        assert "arn::1" not in delta_resolved["event_arn"].values

    def test_empty_events_df_returns_empty_deltas(self):
        delta_new, delta_resolved = _compute_delta(pd.DataFrame(), ["arn::1"])
        assert delta_new.empty
        assert delta_resolved.empty

    def test_no_prev_arns_all_current_open_are_new(self):
        events = [_event("arn::1", status="open"), _event("arn::2", status="open")]
        events_df, _ = _build_dataframes(events)
        delta_new, delta_resolved = _compute_delta(events_df, [])
        assert len(delta_new) == 2
        assert delta_resolved.empty


# ── _build_delta_log ──────────────────────────────────────────────────────────

class TestBuildDeltaLog:
    def test_returns_dataframe(self):
        result = _build_delta_log(pd.DataFrame(), pd.DataFrame(), [])
        assert isinstance(result, pd.DataFrame)

    def test_appends_to_prev_rows(self):
        prev = [{"run_timestamp_utc": "2026-01-01", "delta_type": "new_open", "event_arn": "arn::0"}]
        events_df, _ = _build_dataframes([_event("arn::1", status="open")])
        delta_new, delta_resolved = _compute_delta(events_df, [])
        result = _build_delta_log(delta_new, delta_resolved, prev)
        assert len(result) >= 2  # at least prev row + new row

    def test_empty_delta_preserves_prev_rows(self):
        prev = [{"run_timestamp_utc": "t", "delta_type": "new_open", "event_arn": "arn::x"}]
        result = _build_delta_log(pd.DataFrame(), pd.DataFrame(), prev)
        assert len(result) >= 1

    def test_run_timestamp_column_added(self):
        events_df, _ = _build_dataframes([_event(status="open")])
        delta_new, _ = _compute_delta(events_df, [])
        result = _build_delta_log(delta_new, pd.DataFrame(), [])
        assert "run_timestamp_utc" in result.columns

    def test_delta_type_column_added(self):
        events_df, _ = _build_dataframes([_event(status="open")])
        delta_new, _ = _compute_delta(events_df, [])
        result = _build_delta_log(delta_new, pd.DataFrame(), [])
        assert "delta_type" in result.columns


# ── _table_addr ───────────────────────────────────────────────────────────────

class TestTableAddr:
    def test_returns_string(self):
        df = pd.DataFrame({"a": [1], "b": [2]})
        addr = _table_addr("Events", df)
        assert isinstance(addr, str)
        assert "'Events'" in addr

    def test_includes_sheet_name(self):
        df = pd.DataFrame({"a": [1]})
        addr = _table_addr("MySheet", df)
        assert "MySheet" in addr


# ── write_excel (integration smoke test) ─────────────────────────────────────

class TestWriteExcel:
    def test_returns_bytes(self):
        events = [_event()]
        result = write_excel(events, prev_open_arns=[], delta_log_rows=[])
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_empty_events_returns_bytes(self):
        result = write_excel([], prev_open_arns=[], delta_log_rows=[])
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_xlsx_magic_bytes(self):
        # .xlsx files start with PK (zip format)
        result = write_excel([_event()], prev_open_arns=[], delta_log_rows=[])
        assert result[:2] == b"PK"

    def test_with_delta_log_rows(self):
        prev_rows = [{"run_timestamp_utc": "t", "delta_type": "new_open", "event_arn": "arn::0"}]
        result = write_excel([_event()], prev_open_arns=["arn::old"], delta_log_rows=prev_rows)
        assert isinstance(result, bytes)

    def test_multiple_events(self):
        events = [_event(f"arn::{i}") for i in range(5)]
        result = write_excel(events)
        assert isinstance(result, bytes)


# ── _add_table ────────────────────────────────────────────────────────────────

class TestAddTable:
    def test_no_op_on_empty_df(self):
        from excel_writer import _add_table
        mock_ws = MagicMock()
        _add_table(None, mock_ws, pd.DataFrame(), "tbl")
        mock_ws.add_table.assert_not_called()

    def test_swallows_exception(self):
        from excel_writer import _add_table
        mock_ws = MagicMock()
        mock_wb = MagicMock()
        mock_ws.add_table.side_effect = Exception("xlsxwriter error")
        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        # Should not raise
        _add_table(mock_wb, mock_ws, df, "tbl")
