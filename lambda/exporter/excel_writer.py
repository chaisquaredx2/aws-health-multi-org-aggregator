"""
excel_writer.py

Generates an Excel workbook from AWS Health event data.

Ported and adapted from aws-health-excel-dashboard/health_dashboard.py.

Sheets produced:
  Summary         — KPI counts, charts, delta since last run, navigation links
  Events          — One row per (event, org), all fields, filterable table
  AffectedEntities— One row per (event, org, account), denormalized
  Delta_Latest    — New-open and resolved events since previous export
  Delta_Log       — Rolling history of all delta runs
  Pivot_Service   — Excel pivot by service × status
  Pivot_Account   — Excel pivot by account × status
  Pivot_Region    — Excel pivot by region × status
"""

import json
import tempfile
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd
import xlsxwriter
import xlsxwriter.utility


# ── Public entry point ────────────────────────────────────────────────────────

def write_excel(
    events: List[dict],
    prev_open_arns: Optional[List[str]] = None,
    delta_log_rows: Optional[List[dict]] = None,
) -> bytes:
    """
    Build the Excel workbook and return raw bytes.

    Args:
        events:         List of DynamoDB event items (already enriched with
                        org, account, classification data).
        prev_open_arns: ARNs of events that were open in the previous export
                        run (used to compute delta). Pass [] on first run.
        delta_log_rows: Accumulated delta history rows from previous runs.

    Returns:
        Raw .xlsx bytes ready for upload to S3.
    """
    prev_open_arns = prev_open_arns or []
    delta_log_rows = delta_log_rows or []

    events_df, entities_df = _build_dataframes(events)
    delta_new_df, delta_resolved_df = _compute_delta(events_df, prev_open_arns)
    delta_log_df = _build_delta_log(delta_new_df, delta_resolved_df, delta_log_rows)

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = tmp.name

    _write_workbook(
        tmp_path, events_df, entities_df,
        delta_new_df, delta_resolved_df, delta_log_df,
    )

    with open(tmp_path, "rb") as f:
        return f.read()


def current_open_arns(events: List[dict]) -> List[str]:
    """Return list of ARNs for currently-open events (for state persistence)."""
    return list({
        e["event_arn"]
        for e in events
        if e.get("status") == "open" and e.get("event_arn")
    })


# ── DataFrame builders ────────────────────────────────────────────────────────

def _build_dataframes(events: List[dict]):
    event_rows = []
    entity_rows = []

    for ev in events:
        event_rows.append({
            "event_arn":         ev.get("event_arn"),
            "org_id":            ev.get("org_id"),
            "org_name":          ev.get("org_name"),
            "category":          ev.get("category"),
            "service":           ev.get("service"),
            "event_type_code":   ev.get("event_type_code"),
            "region":            ev.get("region"),
            "status":            ev.get("status"),
            "severity":          ev.get("severity", "standard"),
            "is_operational":    ev.get("is_operational", False),
            "start_time":        ev.get("start_time"),
            "last_updated_time": ev.get("last_updated_time"),
            "end_time":          ev.get("end_time"),
            "affected_account_count": ev.get("affected_account_count", 0),
            "collected_at":      ev.get("collected_at"),
        })

        for acct in ev.get("affected_accounts", []):
            entity_rows.append({
                "event_arn":    ev.get("event_arn"),
                "org_id":       ev.get("org_id"),
                "org_name":     ev.get("org_name"),
                "service":      ev.get("service"),
                "region":       ev.get("region"),
                "status":       ev.get("status"),
                "account_id":   acct.get("account_id"),
                "account_name": acct.get("name"),
                "environment":  acct.get("env"),
                "business_unit": acct.get("bu"),
            })

    events_df = pd.DataFrame(event_rows)
    entities_df = pd.DataFrame(entity_rows)

    if not events_df.empty:
        events_df = events_df.drop_duplicates(subset=["event_arn", "org_id"])
    if not entities_df.empty:
        entities_df = entities_df.drop_duplicates(
            subset=["event_arn", "org_id", "account_id"]
        )

    return events_df, entities_df


def _compute_delta(events_df: pd.DataFrame, prev_open_arns: List[str]):
    empty = pd.DataFrame(columns=[
        "event_arn", "org_id", "org_name", "service", "region",
        "category", "status", "severity", "last_updated_time",
    ])
    if events_df.empty:
        return empty, empty

    current_open = set(
        events_df.loc[events_df["status"] == "open", "event_arn"].tolist()
    )
    prev_open = set(prev_open_arns)

    new_arns = current_open - prev_open
    resolved_arns = prev_open - current_open

    cols = ["event_arn", "org_id", "org_name", "service", "region",
            "category", "status", "severity", "last_updated_time"]

    def _subset(arns):
        if not arns:
            return empty.copy()
        mask = events_df["event_arn"].isin(arns)
        return events_df.loc[mask, [c for c in cols if c in events_df.columns]].copy()

    return _subset(new_arns), _subset(resolved_arns)


def _build_delta_log(delta_new, delta_resolved, prev_rows):
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _tag(df, tag):
        if df.empty:
            return df
        out = df.copy()
        out.insert(0, "delta_type", tag)
        out.insert(0, "run_timestamp_utc", run_ts)
        return out

    new_rows = pd.concat(
        [pd.DataFrame(prev_rows), _tag(delta_new, "new_open"), _tag(delta_resolved, "resolved")],
        ignore_index=True,
    )
    return new_rows


# ── Workbook writer ───────────────────────────────────────────────────────────

def _write_workbook(path, events_df, entities_df, delta_new_df, delta_resolved_df, delta_log_df):
    with pd.ExcelWriter(path, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm:ss") as writer:
        wb = writer.book
        _write_data_sheets(writer, wb, events_df, entities_df, delta_new_df, delta_resolved_df, delta_log_df)
        _write_pivot_sheets(wb, events_df)
        ws_summary = wb.add_worksheet("Summary")
        _write_summary(wb, ws_summary, events_df, delta_new_df, delta_resolved_df)


def _write_data_sheets(writer, wb, events_df, entities_df, delta_new_df, delta_resolved_df, delta_log_df):
    """Write Events, AffectedEntities, Delta_Log, and Delta_Latest sheets."""
    events_df.to_excel(writer, sheet_name="Events", index=False)
    entities_df.to_excel(writer, sheet_name="AffectedEntities", index=False)
    delta_log_df.to_excel(writer, sheet_name="Delta_Log", index=False)

    # Delta_Latest: new open on top, resolved below
    delta_new_df.to_excel(writer, sheet_name="Delta_Latest", index=False)
    ws_delta = writer.sheets["Delta_Latest"]
    sep_row = len(delta_new_df) + 2
    ws_delta.write(sep_row, 0, "Resolved since last run")
    delta_resolved_df.to_excel(
        writer, sheet_name="Delta_Latest",
        startrow=sep_row + 1, index=False,
    )

    # Apply autofit + freeze panes to the tabular sheets
    for sheet_name, df in [
        ("Events", events_df),
        ("AffectedEntities", entities_df),
        ("Delta_Log", delta_log_df),
    ]:
        ws = writer.sheets[sheet_name]
        _autofit(ws, df)
        ws.freeze_panes(1, 0)
        _add_table(wb, ws, df, sheet_name.replace(" ", "_"))


def _write_pivot_sheets(wb, events_df):
    """Add Pivot_Service, Pivot_Account, and Pivot_Region sheets."""
    events_addr = _table_addr("Events", events_df)

    for sheet_name, row_field, table_name in [
        ("Pivot_Service", "service",  "pvt_service"),
        ("Pivot_Account", "org_name", "pvt_account"),
        ("Pivot_Region",  "region",   "pvt_region"),
    ]:
        ws = wb.add_worksheet(sheet_name)
        ws.write(0, 0, f"Events by {row_field.capitalize()}")
        try:
            ws.add_pivot_table({
                "name": table_name,
                "source": events_addr,
                "destination": "A3",
                "fields": {
                    row_field: "row",
                    "status": "column",
                    "event_arn": "sum",
                    "org_name": "filter",
                    "region": "filter",
                    "category": "filter",
                    "severity": "filter",
                },
            })
        except Exception:
            ws.write(2, 0, "(Pivot requires data in Events sheet)")


def _write_summary(wb, ws, events_df, delta_new_df, delta_resolved_df):
    ws.write(0, 0, "AWS Health Aggregator — Export Summary")
    ws.write(1, 0, "Generated UTC")
    ws.write(1, 1, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    # Navigation
    for col, target in enumerate(["Events", "AffectedEntities", "Pivot_Service",
                                   "Pivot_Account", "Pivot_Region", "Delta_Latest"]):
        ws.write_url(2, col, f"internal:'{target}'!A1", string=f"→ {target}")

    # Status KPIs
    ws.write(4, 0, "Status")
    ws.write(4, 1, "Count")
    open_cnt   = int((events_df["status"] == "open").sum())   if not events_df.empty else 0
    closed_cnt = int((events_df["status"] == "closed").sum()) if not events_df.empty else 0
    ws.write(5, 0, "open");   ws.write(5, 1, open_cnt)
    ws.write(6, 0, "closed"); ws.write(6, 1, closed_cnt)

    chart_status = wb.add_chart({"type": "column"})
    chart_status.add_series({
        "name": "Events by Status",
        "categories": "=Summary!$A$6:$A$7",
        "values":     "=Summary!$B$6:$B$7",
    })
    chart_status.set_title({"name": "Events by Status"})
    ws.insert_chart("D4", chart_status)

    # Severity KPIs
    ws.write(4, 3, "Severity")
    ws.write(4, 4, "Count")
    for i, sev in enumerate(["critical", "standard"]):
        cnt = int((events_df["severity"] == sev).sum()) if not events_df.empty else 0
        ws.write(5 + i, 3, sev)
        ws.write(5 + i, 4, cnt)

    # Top services
    ws.write(8, 0, "Top Services")
    ws.write(8, 1, "Events")
    row = 9
    if not events_df.empty:
        svc_counts = (
            events_df.groupby("service")["event_arn"]
            .count()
            .sort_values(ascending=False)
            .head(10)
        )
        for svc, cnt in svc_counts.items():
            ws.write(row, 0, str(svc) if pd.notna(svc) else "(unknown)")
            ws.write(row, 1, int(cnt))
            row += 1

    if row > 9:
        chart_svc = wb.add_chart({"type": "bar"})
        chart_svc.add_series({
            "name": "Top Services",
            "categories": f"=Summary!$A$10:$A${row}",
            "values":     f"=Summary!$B$10:$B${row}",
        })
        chart_svc.set_title({"name": "Top Services"})
        chart_svc.set_legend({"none": True})
        ws.insert_chart("D18", chart_svc)

    # Delta summary
    ws.write(4, 6, "Delta since last run")
    ws.write(5, 6, "New OPEN events")
    ws.write(5, 7, len(delta_new_df))
    ws.write(6, 6, "Resolved events")
    ws.write(6, 7, len(delta_resolved_df))


# ── Utilities ─────────────────────────────────────────────────────────────────

def _autofit(ws, df: pd.DataFrame):
    for i, col in enumerate(df.columns):
        try:
            max_len = max(len(str(col)), *(len(str(v)) for v in df[col].astype(str).head(100)))
        except (ValueError, TypeError):
            max_len = len(str(col))
        ws.set_column(i, i, min(max_len + 2, 60))


def _add_table(wb, ws, df: pd.DataFrame, name: str):
    if df.empty or df.shape[1] == 0:
        return
    nrows, ncols = df.shape
    try:
        ws.add_table(0, 0, nrows, ncols - 1, {
            "name": name,
            "columns": [{"name": c} for c in df.columns],
        })
    except Exception:
        pass  # table creation is best-effort


def _table_addr(sheet_name: str, df: pd.DataFrame) -> str:
    last_col = xlsxwriter.utility.xl_col_to_name(max(len(df.columns) - 1, 0))
    last_row = max(len(df), 0) + 1
    return f"'{sheet_name}'!$A$1:${last_col}${last_row}"
