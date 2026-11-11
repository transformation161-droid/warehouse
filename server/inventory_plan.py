"""Read-only import of inventory PLAN sources, without asserting that goods were counted.

This module deliberately has no database writes. The existing result-import endpoint
is unchanged. A later application integration must preview and authorize each plan
before storing it and must keep actual=None distinct from zero.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import re
import zipfile

from openpyxl import load_workbook


MAX_FILE_BYTES = 3_000_000
MAX_UNCOMPRESSED_BYTES = 20_000_000
MAX_DATA_ROWS = 2000
MAX_COLUMNS = 40
MAX_SHEETS = 20

ALIASES = {
    "externalId": ("id", "ид", "идентификатор", "код позиции", "уникальный id"),
    "inventoryNumber": ("№", "инвентарный номер", "инв. номер", "инв №", "инвентарный №"),
    "item": ("item", "наименование", "наименование тмц", "номенклатура", "инструмент"),
    "brand": ("бренд", "производитель", "марка"),
    "warehouse": ("склад", "склад / объект", "местонахождение"),
    "owner": ("owner", "ответственный", "мол", "материально ответственное лицо"),
    "price": ("price", "стоимость", "цена закупки", "закупочная цена", "цена за единицу"),
    "expected": ("expected", "количество", "учёт", "учет", "учётное количество", "учетное количество"),
    "category": ("category", "категория", "группа", "вид"),
    "unit": ("unit", "ед. изм.", "единица измерения"),
    "note": ("comment", "комментарий", "примечание"),
}

LOOKUP = {alias: field for field, aliases in ALIASES.items() for alias in aliases}


class PlanError(ValueError):
    """Safe client-facing structural validation failure (never includes source cells)."""


def _clean(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value).strip()


def _headers(row):
    """Ignore empty columns but preserve their actual indices for cell lookup."""
    named = {}
    for index, value in enumerate(row):
        name = _clean(value)
        if not name:
            continue
        key = name.casefold()
        if key in named:
            raise PlanError("Одинаковые названия колонок в строке заголовков")
        named[key] = (name, index)
    return named


def _recognition(headers):
    return {field: headers[alias][1] for alias, field in LOOKUP.items() if alias in headers}


def _detect_header(rows):
    choice = None
    for index, row in enumerate(rows[:30]):
        headers = _headers(row)
        matching = _recognition(headers)
        # A general title row must not be mistaken for a data header.
        if "item" not in matching or not (set(matching) & {"externalId", "inventoryNumber", "expected", "price"}):
            continue
        rank = (len(matching), -index)
        if choice is None or rank > choice[0]:
            choice = (rank, index)
    if choice is None:
        raise PlanError("Не найдена строка заголовков: нужны «Наименование» и ID/№/количество/цена")
    return choice[1]


def _amount(raw, *, required, label, allow_zero=True):
    value = _clean(raw)
    if not value:
        if required:
            raise PlanError("Не заполнено обязательное числовое поле: " + label)
        return None
    value = re.sub(r"[\s\u00a0\u202f₽]", "", value).replace(",", ".")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise PlanError("Некорректное число в поле: " + label) from None
    if not amount.is_finite() or amount < 0 or (not allow_zero and amount == 0) or amount > 100_000_000:
        raise PlanError("Недопустимое число в поле: " + label)
    if label == "Цена закупки" and amount.as_tuple().exponent < -2:
        raise PlanError("Цена закупки должна быть с точностью до копеек")
    return amount


def parse_plan(raw: bytes, filename: str, *, sheet_index: int = 0, header_row: int | None = None, mapping: dict | None = None):
    """Return a normalized, **uncommitted** plan suitable for a preview UI.

    Unlike completed inventory imports, "actual" is ALWAYS None. A selected object
    and date belong to the import operation, not guessed from the source workbook.
    All dates/quantities/prices remain operator-reviewable.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_FILE_BYTES:
        raise PlanError("Файл отсутствует или превышает 3 МБ")
    if not isinstance(sheet_index, int) or not 0 <= sheet_index < MAX_SHEETS:
        raise PlanError("Некорректный номер листа")
    if header_row is not None and (not isinstance(header_row, int) or not 1 <= header_row <= 30):
        raise PlanError("Строка заголовков должна быть между 1 и 30")
    rows = []
    if filename.lower().endswith(".xlsx"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                if sum(info.file_size for info in zf.infolist()) > MAX_UNCOMPRESSED_BYTES:
                    raise PlanError("Слишком большой распакованный XLSX")
            book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
            try:
                if sheet_index >= len(book.worksheets):
                    raise PlanError("Такого листа в книге нет")
                for cells in book.worksheets[sheet_index].iter_rows():
                    if len(cells) > MAX_COLUMNS:
                        raise PlanError("Не более 40 колонок на лист")
                    if len(rows) > MAX_DATA_ROWS + 30:
                        raise PlanError("Не более 2000 строк данных за импорт")
                    if any(cell.data_type == "f" for cell in cells):
                        raise PlanError("Формулы в XLSX не поддерживаются: замените значениями")
                    rows.append([cell.value for cell in cells])
            finally:
                book.close()
        except PlanError:
            raise
        except Exception:
            raise PlanError("Не удалось прочитать XLSX") from None
    elif filename.lower().endswith(".csv"):
        try:
            data = raw.decode("utf-8-sig")
            dialect = csv.Sniffer().sniff(data[:4096], delimiters=";,\t")
            rows = list(csv.reader(io.StringIO(data), dialect=dialect, strict=True))
        except (UnicodeError, csv.Error):
            raise PlanError("CSV должен быть UTF-8 с разделителем ; , или табуляцией") from None
    else:
        raise PlanError("Поддерживаются только XLSX и CSV")

    if not rows or not any(any(_clean(v) for v in row) for row in rows):
        raise PlanError("В файле нет строк")
    if any(len(row) > MAX_COLUMNS for row in rows):
        raise PlanError("Не более 40 колонок")

    index = header_row - 1 if header_row is not None else _detect_header(rows)
    headers = _headers(rows[index])
    fields = _recognition(headers)
    if mapping is not None:
        if not isinstance(mapping, dict) or not set(mapping).issubset(ALIASES):
            raise PlanError("Неизвестное поле сопоставления")
        fields = {}
        for field, column in mapping.items():
            key = _clean(column).casefold()
            if key not in headers:
                raise PlanError("Не найдена колонка для: " + field)
            fields[field] = headers[key][1]
        if len(set(fields.values())) != len(fields):
            raise PlanError("Нельзя назначать одну колонку нескольким полям")

    if "item" not in fields:
        raise PlanError("Не сопоставлено обязательное поле «Наименование»")
    if "price" not in fields:
        raise PlanError("Нужна закупочная цена для денежной ведомости")
    if not any(key in fields for key in ("externalId", "inventoryNumber", "expected")):
        raise PlanError("Нужны уникальный ID, инвентарный номер или учётное количество")

    result = []
    duplicates = set()
    for row_number, row in enumerate(rows[index + 1:], index + 2):
        if not any(_clean(v) for v in row):
            continue
        if len(result) >= MAX_DATA_ROWS:
            raise PlanError("Не более 2000 строк данных за импорт")
        cell = lambda key: _clean(row[fields[key]]) if key in fields and fields[key] < len(row) else ""
        item = cell("item")
        if not item:
            raise PlanError(f"Строка {row_number}: пустое наименование")
        source_id = cell("externalId")
        inv_number = cell("inventoryNumber")
        unique_key = source_id or inv_number
        if unique_key:
            if unique_key.casefold() in duplicates:
                raise PlanError(f"Строка {row_number}: повторяется ID/инвентарный номер")
            duplicates.add(unique_key.casefold())
        expected = _amount(cell("expected"), required="expected" in fields, label="Учётное количество")
        if expected is None:
            if not unique_key:
                raise PlanError(f"Строка {row_number}: укажите учётное количество")
            expected = Decimal(1)
        price = _amount(cell("price"), required=True, label="Цена закупки")
        warehouse = cell("warehouse")
        owner = cell("owner") or (warehouse.rsplit("/", 1)[-1].strip() if "/" in warehouse else "")
        result.append({
            "row": row_number,
            "externalId": source_id,
            "inventoryNumber": inv_number,
            "item": item,
            "brand": cell("brand"),
            "warehouse": warehouse,
            "owner": owner,
            "sku": inv_number or source_id,
            "category": cell("category") or "Прочее",
            "unit": cell("unit") or "шт.",
            "expected": str(expected),
            "actual": None,
            "purchasePrice": str(price),
            "note": cell("note"),
        })

    if not result:
        raise PlanError("После заголовков нет позиций")
    total = sum(Decimal(row["expected"]) * Decimal(row["purchasePrice"]) for row in result)
    return {
        "sourceSha256": hashlib.sha256(raw).hexdigest(),
        "sheet": sheet_index,
        "headerRow": index + 1,
        "headers": [x[0] for x in headers.values()],
        "mapping": {field: list(headers.values())[[value[1] for value in headers.values()].index(column)][0] for field, column in fields.items()},
        "positions": len(result),
        "totalPurchaseValue": str(total.quantize(Decimal("0.01"))),
        "rows": result,
        "counted": 0,
        "resultReady": False,
    }
