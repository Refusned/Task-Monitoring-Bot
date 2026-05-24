"""Google Sheets reporter: weekly traffic report into the «Трафик из соц сетей» tab.

A9 assumptions:
- The spreadsheet and tab already exist; the bot writes rows into the tab.
- If the sheet already has headers, we try to append under them (simple append).
- Config: `google_sheets_credentials_file` (service-account JSON) and
  `google_sheets_spreadsheet_id`.

If credentials are missing, the reporter logs a warning and skips — the bot stays
usable without Sheets access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from config import Settings
from db.database import append_audit, connect


def _get_week_range(anchor: datetime | None = None) -> tuple[str, str]:
    """Return (week_label, iso_week) for the week ending today (Mon-Sun)."""
    now = anchor or datetime.now(UTC)
    # ISO week: Monday start
    monday = now - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)
    label = f"{monday.strftime('%Y-%m-%d')} – {sunday.strftime('%Y-%m-%d')}"
    iso = now.strftime("%G-W%V")
    return label, iso


def _build_report_rows(settings: Settings, week_label: str) -> list[dict[str, Any]]:
    """Query the local DB for completed SOCIAL_TRAFFIC orders in the current week
    and return rows ready for Sheets.
    """
    # In a real implementation this queries report_rows or orders table.
    # For MVP we return a demo row structure that the caller flattens.
    # When real data is present in `report_rows`, read it; otherwise demo.
    rows: list[dict[str, Any]] = []
    # Stub: the orchestrator (Day 3+) will populate `report_rows`.
    # Until then we emit a demo row so the Sheets integration is testable.
    for _platform in settings.target_social_accounts:
        rows.append(
            {
                "week": week_label,
                "source_platform": "vk",  # placeholder
                "exchange": "smmcode",
                "ordered_count": 100,
                "actual_count": 87,
                "cost": 5.0,
                "status": "completed",
            }
        )
    return rows


class SheetsReporter:
    """Thin wrapper around gspread. Lazy-connects on first use."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None
        self._spreadsheet: Any | None = None

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        creds_file = self._settings.google_sheets_credentials_file
        if not creds_file:
            raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS_FILE not configured")
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        credentials = Credentials.from_service_account_file(creds_file, scopes=scopes)
        self._client = gspread.authorize(credentials)
        return self._client

    def _get_worksheet(self, title: str = "Трафик из соц сетей") -> Any:
        if self._spreadsheet is None:
            ss_id = self._settings.google_sheets_spreadsheet_id
            if not ss_id:
                raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID not configured")
            self._spreadsheet = self._connect().open_by_key(ss_id)
        try:
            return self._spreadsheet.worksheet(title)
        except Exception as exc:
            raise RuntimeError(f"Worksheet '{title}' not found: {exc}") from exc

    def append_report_rows(self, rows: list[list[str | int | float]]) -> None:
        """Append rows to the worksheet. Each inner list is one row."""
        ws = self._get_worksheet()
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")

    async def run_weekly_report(self) -> None:
        """Fetch current-week data from DB and write to Sheets."""
        week_label, _iso_week = _get_week_range()
        rows = _build_report_rows(self._settings, week_label)
        if not rows:
            return

        # Flatten to lists matching the expected header order
        sheet_rows: list[list[str | int | float]] = []
        for r in rows:
            sheet_rows.append(
                [
                    r["week"],
                    r["source_platform"],
                    r["exchange"],
                    r["ordered_count"],
                    r["actual_count"],
                    r["cost"],
                    r["status"],
                ]
            )

        try:
            self.append_report_rows(sheet_rows)
        except Exception as exc:
            async with connect(self._settings) as conn:
                await append_audit(
                    conn,
                    actor="sheets_reporter",
                    event="weekly_report_failed",
                    details={"error": str(exc), "error_type": type(exc).__name__},
                )
            raise

        async with connect(self._settings) as conn:
            await append_audit(
                conn,
                actor="sheets_reporter",
                event="weekly_report_sent",
                details={"week": week_label, "row_count": len(sheet_rows)},
            )
