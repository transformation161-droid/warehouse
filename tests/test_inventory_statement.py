"""Synthetic procurement-value reconciliation and printable statement tests."""
from io import BytesIO

import pytest
from openpyxl import load_workbook

from server.inventory_statement import (
    summarize_inventory, require_complete, export_inventory_xlsx,
)


def lines():
    return [
        {"item": "Дрель", "sku": "001", "unit": "шт.", "expected": 1,
         "actual": None, "purchasePrice": "12500.00"},
        {"item": "Перфоратор", "sku": "002", "unit": "шт.", "expected": 1,
         "actual": 0, "purchasePrice": "18000.00"},
        {"item": "Лазерный уровень", "sku": "003", "unit": "шт.", "expected": 1,
         "actual": 2, "purchasePrice": "9500.00"},
    ]


def test_unchecked_distinct_from_missing_and_both_variances_separate():
    result = summarize_inventory(lines())
    assert result["positions"] == 3
    assert result["checked"] == 2
    assert result["unchecked"] == 1
    assert result["complete"] is False
    assert str(result["plannedPurchaseValue"]) == "40000.00"
    assert str(result["knownFactPurchaseValue"]) == "19000.00"
    assert str(result["shortagePurchaseValue"]) == "18000.00"
    assert str(result["surplusPurchaseValue"]) == "9500.00"
    assert result["rows"][0]["status"] == "Не проверено"
    assert result["rows"][0]["difference"] is None
    assert result["rows"][1]["status"] == "Недостача"
    assert result["rows"][2]["status"] == "Излишек"


def test_finalization_rejects_incomplete_then_accepts_completed():
    values = lines()
    with pytest.raises(ValueError, match="непроверенными"):
        require_complete(summarize_inventory(values))
    values[0]["actual"] = 1
    result = summarize_inventory(values)
    require_complete(result)
    assert result["checked"] == 3
    assert result["complete"] is True


def test_never_treat_blank_actual_as_zero():
    for value in (None, ""):
        x = summarize_inventory([{"item": "Инструмент", "expected": 1,
                                 "actual": value, "price": "25000"}])
        assert x["rows"][0]["shortageValue"] is None
        assert x["shortagePurchaseValue"] == 0
        assert not x["complete"]


def test_export_spreadsheet_preserves_unchecked_and_currency():
    report = summarize_inventory(lines())
    data = export_inventory_xlsx(
        {"site": "Тестовый объект", "title": "Тестовая инвентаризация", "date": "2026-11-11"},
        report,
    )
    ws = load_workbook(BytesIO(data), read_only=True, data_only=True).active
    assert ws["F6"].value is None   # not counted
    assert ws["F7"].value == 0      # counted but missing
    assert ws["K7"].value == 18000
    assert ws["L8"].value == 9500
    assert ws["K9"].value == 18000


def test_safe_text_not_an_excel_formula():
    report = summarize_inventory([
        {"item": "=HYPERLINK(\"https://example.com\")", "sku": "001",
         "expected": 1, "actual": 1, "price": 1},
    ])
    data = export_inventory_xlsx({"site": "Тест", "title": "Тест", "date": "2026-11-11"}, report)
    cell = load_workbook(BytesIO(data), data_only=False).active["B6"]
    assert cell.data_type != "f"
    assert cell.value.startswith("'=")


def test_negative_values_rejected():
    with pytest.raises(ValueError, match="Отрицательные"):
        summarize_inventory([{"item":"Тест","expected":1,"actual":-1,"price":12}])
