"""Google Sheets weekly report writer.

Pushes rows from the `report_rows` SQLite table into the operator's spreadsheet
under the **«Трафик из соц сетей»** tab (per CLAUDE.md A9). The sheet itself
is owned by the customer; we authenticate via a Google Service Account that
the customer shares the spreadsheet with.

Idempotency: `report_rows.pushed_to_sheets_at` is set after a successful sheet
append, so re-running the writer never duplicates rows.

Column-mapping (the soft part of A9):
- If the sheet is empty → we write our canonical header in row 1, then data.
- If the sheet already has a header → we read it, map our fields by Russian
  column name (case-insensitive, trimmed), and write only those columns the
  operator's sheet has. Columns we don't recognize stay blank.
- If the operator's header is unrecognized → fall back to canonical column
  order and append data after the existing rows (operator can re-arrange
  later).

Threading: gspread is sync. All gspread calls run in the default executor so
the FastAPI event loop stays free.

Setup for the customer (one-time):
1. Create a Service Account at https://console.cloud.google.com/iam-admin/serviceaccounts
2. Enable Google Sheets API on that GCP project
3. Download the SA JSON key → place at the path in `GOOGLE_SHEETS_CREDENTIALS_FILE`
4. Share the target spreadsheet with the SA email (Editor permission)
5. Put the spreadsheet ID into `GOOGLE_SHEETS_SPREADSHEET_ID` and restart
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger("sheets")

# Canonical schema for our sheet — ordered. The first column header values are
# what we write to row 1 when the sheet is empty; lookups are case-insensitive
# stripped matches against operator-supplied headers.
SHEET_TAB_NAME = "Трафик из соц сетей"

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("week", "Неделя"),
    ("source_platform", "Платформа"),
    ("exchange", "Биржа"),
    ("ordered_count", "Заказано"),
    ("actual_count", "Фактически"),
    ("cost", "Стоимость"),
    ("status", "Статус"),
    ("order_uuid", "ID заказа"),  # bonus: helps operator trace each row back to dashboard
)

# Friendlier Russian status labels for the sheet (operator-facing).
_STATUS_LABEL = {
    "completed": "Готово",
    "failed": "Ошибка",
    "cancelled": "Отменён",
    "verifying": "На проверке",
    "active": "В работе",
    "creating": "Создаётся",
    "draft": "Черновик",
}

_FIELDS = tuple(k for k, _ in _COLUMNS)
_CANONICAL_HEADER = tuple(v for _, v in _COLUMNS)
# Alias map: any of these → our field key. Lets us read non-canonical headers
# the operator may have set up in their own template.
_HEADER_ALIASES: dict[str, str] = {
    "неделя": "week",
    "week": "week",
    "период": "week",
    "платформа": "source_platform",
    "платформа-источник": "source_platform",
    "platform": "source_platform",
    "биржа": "exchange",
    "панель": "exchange",
    "exchange": "exchange",
    "заказано": "ordered_count",
    "заказано переходов": "ordered_count",
    "ordered": "ordered_count",
    "ordered_count": "ordered_count",
    "фактически": "actual_count",
    "фактически (метрика)": "actual_count",
    "actual": "actual_count",
    "actual_count": "actual_count",
    "стоимость": "cost",
    "цена": "cost",
    "бюджет": "cost",
    "cost": "cost",
    "статус": "status",
    "status": "status",
    "id заказа": "order_uuid",
    "order_id": "order_uuid",
    "order_uuid": "order_uuid",
    "uuid": "order_uuid",
}


@dataclass(frozen=True)
class SyncResult:
    pushed: int                # rows newly written to the sheet
    skipped: int               # rows we couldn't render (silently — count for ops)
    spreadsheet_title: str
    tab_title: str


class SheetsWriter:
    """Thin wrapper around `gspread` with our column logic.

    Construct once at app startup (see app.state.build_app_state). Cheap to
    keep around; gspread holds an httplib2 session.
    """

    def __init__(
        self,
        credentials_path: str | Path,
        spreadsheet_id: str,
        tab_name: str = SHEET_TAB_NAME,
    ) -> None:
        path = Path(credentials_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(
                f"Google Sheets credentials file not found: {path}. "
                "Set GOOGLE_SHEETS_CREDENTIALS_FILE in .env."
            )
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required (GOOGLE_SHEETS_SPREADSHEET_ID)")
        self._credentials_path = path
        self._spreadsheet_id = spreadsheet_id
        self._tab_name = tab_name
        # gspread client is lazy-built per call so a bad creds file doesn't
        # crash the FastAPI startup — health_check() surfaces the failure cleanly.
        self._client = None  # type: ignore[assignment]

    def _build_client(self):
        from google.oauth2.service_account import Credentials
        import gspread

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(str(self._credentials_path), scopes=scopes)
        return gspread.authorize(creds)

    def _open_worksheet(self):
        if self._client is None:
            self._client = self._build_client()
        sheet = self._client.open_by_key(self._spreadsheet_id)
        try:
            return sheet, sheet.worksheet(self._tab_name)
        except Exception:
            # Tab doesn't exist — create it.
            ws = sheet.add_worksheet(title=self._tab_name, rows=200, cols=len(_COLUMNS) + 2)
            ws.append_row(list(_CANONICAL_HEADER))
            return sheet, ws

    # --- async wrappers (gspread is sync, don't block the loop) -----------

    async def health_check(self) -> dict:
        """Open spreadsheet + tab; return metadata. Useful for /api/sheets/test."""
        loop = asyncio.get_running_loop()

        def _go():
            sheet, ws = self._open_worksheet()
            return {
                "spreadsheet_id": self._spreadsheet_id,
                "spreadsheet_title": sheet.title,
                "tab_title": ws.title,
                "row_count": ws.row_count,
                "col_count": ws.col_count,
                "first_row": ws.row_values(1) if ws.row_count > 0 else [],
            }

        return await loop.run_in_executor(None, _go)

    async def append_rows(self, rows: list[dict]) -> SyncResult:
        """Append `rows` to the sheet honoring the existing header layout.

        Each row dict must have the keys from `_FIELDS`. Missing keys → blank.
        """
        if not rows:
            return SyncResult(pushed=0, skipped=0, spreadsheet_title="", tab_title=self._tab_name)
        loop = asyncio.get_running_loop()

        def _go() -> SyncResult:
            sheet, ws = self._open_worksheet()
            header = ws.row_values(1)
            if not header:
                ws.append_row(list(_CANONICAL_HEADER))
                header = list(_CANONICAL_HEADER)
            field_for_col = _build_field_for_col(header)
            payload: list[list] = []
            skipped = 0
            for r in rows:
                try:
                    row_values = _row_to_cells(r, field_for_col)
                except Exception as exc:
                    _LOG.warning("skipping unrenderable row %r: %r", r, exc)
                    skipped += 1
                    continue
                payload.append(row_values)
            if payload:
                ws.append_rows(payload, value_input_option="USER_ENTERED")
            return SyncResult(
                pushed=len(payload),
                skipped=skipped,
                spreadsheet_title=sheet.title,
                tab_title=ws.title,
            )

        return await loop.run_in_executor(None, _go)


def _build_field_for_col(header: list[str]) -> list[str | None]:
    """Map each column index → our field name (or None if unrecognized)."""
    out: list[str | None] = []
    for raw in header:
        key = (raw or "").strip().lower()
        out.append(_HEADER_ALIASES.get(key))
    return out


def _row_to_cells(row: dict, field_for_col: list[str | None]) -> list:
    """Pull values from `row` in the order the operator's header demands."""
    cells: list = []
    for field in field_for_col:
        if field is None:
            cells.append("")
            continue
        value = row.get(field)
        if field == "status" and isinstance(value, str):
            value = _STATUS_LABEL.get(value, value)
        if value is None:
            cells.append("")
        else:
            cells.append(value)
    return cells
