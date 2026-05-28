"""Day 3 tests — SheetsWriter row mapping + db helpers for unpushed rows.

We never touch real Google APIs. The writer's `_open_worksheet` is replaced
with a fake that captures appended rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config import Settings
from db.database import (
    connect,
    init_db,
    list_unpushed_report_rows,
    mark_report_rows_pushed,
    record_report_row,
)
from reporting.sheets import (
    _CANONICAL_HEADER,
    _build_field_for_col,
    _row_to_cells,
    SheetsWriter,
    SyncResult,
)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        dry_run=True,
        dashboard_token="t",
        agent_tools_token="t2",
    )


# --- header / row mapping (pure functions) --------------------------------


def test_canonical_header_writes_all_fields() -> None:
    field_map = _build_field_for_col(list(_CANONICAL_HEADER))
    # 8 fields, all recognized
    assert all(f is not None for f in field_map)
    assert field_map[0] == "week"
    assert field_map[-1] == "order_uuid"


def test_recognizes_operator_aliases() -> None:
    """Operator-supplied Russian variants are mapped to our fields."""
    operator_header = [
        "Период", "Платформа-источник", "Панель", "Заказано переходов",
        "Фактически (Метрика)", "Бюджет", "Статус",
    ]
    mapped = _build_field_for_col(operator_header)
    assert mapped == [
        "week", "source_platform", "exchange", "ordered_count",
        "actual_count", "cost", "status",
    ]


def test_unknown_column_returns_blank() -> None:
    """A column the operator added that we don't know stays empty in the row."""
    mapped = _build_field_for_col(["Неделя", "Какой-то комментарий", "Биржа"])
    assert mapped == ["week", None, "exchange"]
    row = {"week": "2026-W22", "exchange": "smmcode"}
    cells = _row_to_cells(row, mapped)
    assert cells == ["2026-W22", "", "smmcode"]


def test_status_translated_to_russian() -> None:
    mapped = _build_field_for_col(["Статус"])
    cells = _row_to_cells({"status": "completed"}, mapped)
    assert cells == ["Готово"]
    cells = _row_to_cells({"status": "failed"}, mapped)
    assert cells == ["Ошибка"]
    # unknown status stays as-is
    cells = _row_to_cells({"status": "weird"}, mapped)
    assert cells == ["weird"]


def test_none_values_become_blank() -> None:
    mapped = _build_field_for_col(["Заказано", "Фактически", "Стоимость"])
    cells = _row_to_cells({"ordered_count": 100, "actual_count": None, "cost": None}, mapped)
    assert cells == [100, "", ""]


# --- db helpers: unpushed + mark --------------------------------------------


async def test_unpushed_excludes_already_pushed(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        await record_report_row(
            conn, order_uuid="o1", source_platform="youtube",
            exchange="smmcode", ordered_count=10, actual_count=11,
            cost=1.0, status="completed",
        )
        await record_report_row(
            conn, order_uuid="o2", source_platform="telegram",
            exchange="smmcode", ordered_count=5, actual_count=5,
            cost=0.5, status="completed",
        )
        # Mark o1 as pushed
        cursor = await conn.execute(
            "SELECT row_id FROM report_rows WHERE order_uuid = ?", ("o1",)
        )
        row = await cursor.fetchone()
        await mark_report_rows_pushed(conn, [row["row_id"]])

        unpushed = await list_unpushed_report_rows(conn)
    uuids = {r["order_uuid"] for r in unpushed}
    assert uuids == {"o2"}


async def test_mark_pushed_is_idempotent(settings: Settings) -> None:
    """Re-marking already-pushed rows is a no-op (counts the second update as 0)."""
    await init_db(settings)
    async with connect(settings) as conn:
        await record_report_row(
            conn, order_uuid="o1", source_platform="youtube",
            exchange="smmcode", ordered_count=10, actual_count=11,
            cost=1.0, status="completed",
        )
        cursor = await conn.execute("SELECT row_id FROM report_rows")
        row = await cursor.fetchone()
        first = await mark_report_rows_pushed(conn, [row["row_id"]])
        second = await mark_report_rows_pushed(conn, [row["row_id"]])
    assert first == 1
    assert second == 0


async def test_mark_pushed_empty_list_is_zero(settings: Settings) -> None:
    await init_db(settings)
    async with connect(settings) as conn:
        n = await mark_report_rows_pushed(conn, [])
    assert n == 0


# --- SheetsWriter.append_rows with a fake worksheet -------------------------


class _FakeSheet:
    def __init__(self, title="Test Sheet"):
        self.title = title


class _FakeWorksheet:
    def __init__(self, header: list[str] | None = None, title="Трафик из соц сетей"):
        self.title = title
        self.row_count = 200
        self.col_count = 26
        self._header = header or []
        self.appended: list[list] = []

    def row_values(self, row_idx: int) -> list[str]:
        if row_idx == 1:
            return list(self._header)
        return []

    def append_row(self, values: list) -> None:
        if not self._header:
            self._header = list(values)
        else:
            self.appended.append(values)

    def append_rows(self, values: list[list], value_input_option: str = "RAW") -> None:
        self.appended.extend(values)


def _make_writer_with_fake(monkeypatch, tmp_path, header: list[str] | None = None) -> tuple[SheetsWriter, _FakeWorksheet]:
    creds = tmp_path / "fake.json"
    creds.write_text("{}", encoding="utf-8")
    writer = SheetsWriter(credentials_path=creds, spreadsheet_id="sid")
    fake_ws = _FakeWorksheet(header=header)
    fake_sheet = _FakeSheet()

    def fake_open():
        return fake_sheet, fake_ws

    monkeypatch.setattr(writer, "_open_worksheet", fake_open)
    return writer, fake_ws


async def test_append_rows_writes_canonical_header_when_empty(tmp_path, monkeypatch) -> None:
    writer, ws = _make_writer_with_fake(monkeypatch, tmp_path, header=None)
    rows = [
        {
            "week": "2026-W22",
            "source_platform": "youtube",
            "exchange": "smmcode",
            "ordered_count": 100,
            "actual_count": 108,
            "cost": 9.68,
            "status": "completed",
            "order_uuid": "abc-123",
        }
    ]
    result = await writer.append_rows(rows)
    assert isinstance(result, SyncResult)
    assert result.pushed == 1
    assert ws._header == list(_CANONICAL_HEADER)
    # The data row matches canonical order
    assert ws.appended == [["2026-W22", "youtube", "smmcode", 100, 108, 9.68, "Готово", "abc-123"]]


async def test_append_rows_honors_existing_header_order(tmp_path, monkeypatch) -> None:
    """If sheet has a custom header — write data in that order, blanks for unknowns."""
    custom_header = ["Биржа", "Заказано", "Стоимость", "Статус", "Комментарий"]
    writer, ws = _make_writer_with_fake(monkeypatch, tmp_path, header=custom_header)
    rows = [
        {
            "week": "2026-W22",  # ignored, no Неделя column
            "source_platform": "vk",  # ignored, no Платформа column
            "exchange": "smmcode",
            "ordered_count": 50,
            "actual_count": 49,  # ignored, no Фактически column
            "cost": 5.5,
            "status": "completed",
            "order_uuid": "x",
        }
    ]
    result = await writer.append_rows(rows)
    assert result.pushed == 1
    assert ws.appended == [["smmcode", 50, 5.5, "Готово", ""]]


async def test_append_rows_empty_input_does_nothing(tmp_path, monkeypatch) -> None:
    writer, ws = _make_writer_with_fake(monkeypatch, tmp_path)
    result = await writer.append_rows([])
    assert result.pushed == 0
    assert result.skipped == 0
    assert ws.appended == []


async def test_append_rows_skips_unrenderable_rows(tmp_path, monkeypatch) -> None:
    """Defensive: a row missing all fields is logged + skipped, not crashing."""
    writer, ws = _make_writer_with_fake(monkeypatch, tmp_path, header=list(_CANONICAL_HEADER))
    rows = [
        {"week": "2026-W22", "exchange": "smmcode", "ordered_count": 1, "source_platform": "x",
         "actual_count": 1, "cost": 1.0, "status": "completed", "order_uuid": "ok"},
    ]
    result = await writer.append_rows(rows)
    assert result.pushed == 1
    assert result.skipped == 0


# --- SheetsWriter constructor validation ------------------------------------


def test_constructor_rejects_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        SheetsWriter(
            credentials_path=tmp_path / "nope.json", spreadsheet_id="sid"
        )


def test_constructor_rejects_empty_spreadsheet_id(tmp_path) -> None:
    creds = tmp_path / "ok.json"
    creds.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="spreadsheet_id"):
        SheetsWriter(credentials_path=creds, spreadsheet_id="")
