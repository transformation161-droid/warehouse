"""Synthetic, non-production fixtures for the inventory-plan XLSX/CSV reader."""
from io import BytesIO

import pytest
from openpyxl import Workbook

from server.inventory_plan import PlanError, parse_plan


def workbook_payload(headers=None, data=None):
    book = Workbook()
    sheet = book.active
    sheet["A2"] = "Отчёт по позициям"
    values = headers or ["", "ID", "Наименование комплекта", "Наименование", "Бренд",
                         "Склад", "Ответственный", "Статус", "№", "Стоимость",
                         "Счет", "Поставщик", "Комментарий", "QR-код"]
    for index, value in enumerate(values, 1):
        sheet.cell(4, index, value)
    if data is None:
        data = [["", str(i), "", f"Инструмент {i}", "Марка",
                 "Событие / 01.572 / Материально ответственное лицо", "",
                 "Активный", f"ИНВ-{i}", "1 234,50", "", "", "", ""]
                for i in range(1, 70)]
    for position, row in enumerate(data, 5):
        for index, value in enumerate(row, 1):
            sheet.cell(position, index, value)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_report_with_title_and_69_unique_tools_is_uncounted():
    result = parse_plan(workbook_payload(), "positions.xlsx")
    assert result["headerRow"] == 4
    assert result["positions"] == 69
    assert result["counted"] == 0
    assert result["resultReady"] is False
    assert result["rows"][0]["externalId"] == "1"
    assert result["rows"][0]["inventoryNumber"] == "ИНВ-1"
    assert result["rows"][0]["item"] == "Инструмент 1"
    assert result["rows"][0]["owner"] == "Материально ответственное лицо"
    assert all(r["expected"] == "1" and r["actual"] is None for r in result["rows"])
    assert result["totalPurchaseValue"] == "85180.50"


def test_same_positions_with_different_excel_title_are_same_plan():
    from openpyxl import load_workbook
    original = workbook_payload()
    book = load_workbook(BytesIO(original))
    book.active["A2"] = "Повторная выгрузка с другой шапкой"
    output = BytesIO()
    book.save(output)
    a = parse_plan(original, "source.xlsx")
    b = parse_plan(output.getvalue(), "fresh-export.xlsx")
    assert a["sourceSha256"] != b["sourceSha256"]
    assert a["planSha256"] == b["planSha256"]
    assert a["positions"] == b["positions"] == 69


def test_explicit_count_for_bulk_materials():
    headers = ["Наименование", "Количество", "Стоимость"]
    data = [["Профиль", 12, 250]]
    result = parse_plan(workbook_payload(headers, data), "bulk.xlsx")
    assert result["positions"] == 1
    assert result["rows"][0]["expected"] == "12"
    assert result["rows"][0]["purchasePrice"] == "250"
    assert result["rows"][0]["actual"] is None


def test_duplicate_ids_are_rejected_with_source_row():
    data = [
        ["", "6", "", "Инструмент А", "", "", "", "", "1", 200],
        ["", "6", "", "Инструмент Б", "", "", "", "", "2", 300],
    ]
    with pytest.raises(PlanError, match="Строка 6.*повторяется"):
        parse_plan(workbook_payload(data=data), "duplicates.xlsx")


def test_duplicate_nonempty_headers_rejected():
    headers = ["", "ID", "", "Наименование", "", "", "", "", "№", "Стоимость",
               "", "", "", "ID"]
    with pytest.raises(PlanError, match="Одинаковые"):
        parse_plan(workbook_payload(headers), "duplicate-headers.xlsx")


def test_price_is_required_not_silently_zero():
    headers = ["", "ID", "", "Наименование"]
    data = [["", "4", "", "Дрель"]]
    with pytest.raises(PlanError, match="закупочная цена"):
        parse_plan(workbook_payload(headers, data), "no-cost.xlsx")


def test_formula_in_import_is_rejected_instead_of_evaluated():
    row = ["", "4", "", "Дрель", "", "", "", "", "4", "=100+200"]
    with pytest.raises(PlanError, match="Формулы"):
        parse_plan(workbook_payload(data=[row]), "formula.xlsx")


def test_zero_purchasing_price_is_supported_when_explicit():
    row = ["", "4", "", "Дрель", "", "", "", "", "4", 0]
    result = parse_plan(workbook_payload(data=[row]), "cost-zero.xlsx")
    assert result["rows"][0]["purchasePrice"] == "0"
    assert result["rows"][0]["actual"] is None


def test_invalid_negative_price_is_rejected():
    row = ["", "4", "", "Дрель", "", "", "", "", "4", -1]
    with pytest.raises(PlanError, match="Недопустимое число"):
        parse_plan(workbook_payload(data=[row]), "negative.xlsx")


def test_csv_from_first_row_does_not_require_empty_columns():
    content = "item;externalId;price;owner\nТриммер;TOOL-1;2200;КСУ\n".encode("utf-8")
    result = parse_plan(content, "plan.csv")
    assert result["positions"] == 1
    assert result["headerRow"] == 1
    assert result["rows"][0]["actual"] is None


def test_csv_with_repeated_identifier_is_rejected():
    text = "item;externalId;price\nA;1;100\nB;1;200\n"
    with pytest.raises(PlanError, match="повторяется"):
        parse_plan(text.encode(), "duplicated.csv")


def test_no_user_source_uploaded_or_embedded():
    # Test fixtures contain only synthetic tools and invented warehouses.
    result = parse_plan(workbook_payload(), "test.xlsx")
    assert result["positions"] == 69
    assert not any(row["actual"] == 0 for row in result["rows"])
