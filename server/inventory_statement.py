"""Plan/fact inventory statement with procurement-price valuation only.

Not a residual-value or employee-liability calculation. Unchecked is distinct
from physically missing, and shortages/surpluses are never netted.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side


CENT = Decimal("0.01")


def decimal(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Некорректное число: " + field) from None
    if not number.is_finite() or number < 0:
        raise ValueError("Отрицательные или бесконечные значения недопустимы: " + field)
    return number


def money(value):
    return (value or Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)


def summarize_inventory(lines):
    """Return per-position calculated statement and totals; never infer actual=0."""
    statement = []
    plan_value = counted_value = shortage_value = surplus_value = Decimal(0)
    checked = 0
    for i, original in enumerate(lines, 1):
        planned = decimal(original.get("expected"), "Учёт")
        price = decimal(original.get("purchasePrice", original.get("price")), "Цена закупки")
        actual_raw = original.get("actual")
        is_counted = actual_raw is not None and str(actual_raw).strip() != ""
        actual = decimal(actual_raw, "Факт") if is_counted else None
        plan_cost = money(planned * price)
        plan_value += plan_cost
        if is_counted:
            checked += 1
            fact_cost = money(actual * price)
            short = money(max(planned - actual, Decimal(0)) * price)
            surplus = money(max(actual - planned, Decimal(0)) * price)
            counted_value += fact_cost
            shortage_value += short
            surplus_value += surplus
        else:
            fact_cost = short = surplus = None
        statement.append({
            "no": i,
            "item": str(original.get("item", "")).strip(),
            "brand": str(original.get("brand") or "").strip(),
            "sku": (
                (str(original.get("inventoryNumber") or original.get("sku") or "").strip() +
                 (" / ID " + str(original["sourceId"]).strip()
                  if original.get("sourceId") and str(original["sourceId"]).strip() !=
                     str(original.get("inventoryNumber") or original.get("sku") or "").strip()
                  else ""))
            ),
            "unit": str(original.get("unit") or "шт."),
            "expected": planned,
            "actual": actual,
            "difference": actual - planned if is_counted else None,
            "purchasePrice": price,
            "planValue": plan_cost,
            "knownFactValue": fact_cost,
            "shortageValue": short,
            "surplusValue": surplus,
            "comment": str(original.get("reason", original.get("note", "")) or ""),
            "status": ("Не проверено" if not is_counted else
                       "Недостача" if planned > actual else
                       "Излишек" if planned < actual else "Соответствует"),
        })
    return {
        "rows": statement,
        "positions": len(statement),
        "checked": checked,
        "unchecked": len(statement) - checked,
        "complete": len(statement) > 0 and checked == len(statement),
        "plannedPurchaseValue": money(plan_value),
        "knownFactPurchaseValue": money(counted_value),
        "shortagePurchaseValue": money(shortage_value),
        "surplusPurchaseValue": money(surplus_value),
        "costBasis": "Закупочная стоимость, без амортизации и остаточной стоимости",
    }


def require_complete(summary):
    if not summary["complete"]:
        raise ValueError("Нельзя завершить инвентаризацию с непроверенными позициями")


def _text(value):
    result = str(value or "")
    return "'" + result if result.lstrip().startswith(("=", "+", "@")) else result


def export_inventory_xlsx(inventory, summary):
    """Excel plan/fact statement; can be printed before or after a count."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "План-факт"
    site, title, day = (str(inventory.get(k) or "") for k in ("site", "title", "date"))
    sheet.append(["ИНСТРАКТ | Ведомость результатов инвентаризации"])
    sheet.merge_cells("A1:N1")
    sheet.append(["Объект", _text(site), "Дата", day, "Документ", _text(title)])
    sheet.append(["Основа оценки", summary["costBasis"]])
    sheet.merge_cells("B3:N3")
    sheet.append([])
    sheet.append(["№", "Наименование", "Бренд", "Инв. № / ID", "Ед.", "Учёт", "Факт",
                  "Отклонение", "Цена закупки, ₽", "План, ₽", "Факт, ₽",
                  "Недостача, ₽", "Излишек, ₽", "Статус"])
    for row in summary["rows"]:
        sheet.append([
            row["no"], _text(row["item"]), _text(row["brand"]), _text(row["sku"]), _text(row["unit"]),
            float(row["expected"]), float(row["actual"]) if row["actual"] is not None else None,
            float(row["difference"]) if row["difference"] is not None else None,
            float(row["purchasePrice"]), float(row["planValue"]),
            float(row["knownFactValue"]) if row["knownFactValue"] is not None else None,
            float(row["shortageValue"]) if row["shortageValue"] is not None else None,
            float(row["surplusValue"]) if row["surplusValue"] is not None else None,
            _text(row["status"]),
        ])
    end = len(summary["rows"]) + 6
    sheet.cell(end, 2, "ИТОГО")
    for col, key in ((10, "plannedPurchaseValue"), (11, "knownFactPurchaseValue"),
                     (12, "shortagePurchaseValue"), (13, "surplusPurchaseValue")):
        sheet.cell(end, col, float(summary[key]))
    sheet.cell(end + 2, 1, "Проверено")
    sheet.cell(end + 2, 2, summary["checked"])
    sheet.cell(end + 3, 1, "Не проверено")
    sheet.cell(end + 3, 2, summary["unchecked"])
    sheet.cell(end + 4, 1, "Оценка")
    sheet.cell(end + 4, 2, "Предварительные расхождения по закупочной стоимости, НЕ установленный ущерб")
    sheet.merge_cells(start_row=end + 4, start_column=2, end_row=end + 4, end_column=14)
    sheet.cell(end + 6, 1, "Проверяющий")
    sheet.cell(end + 6, 6, "МОЛ")
    sheet.cell(end + 6, 10, "Утверждающий")
    sheet.freeze_panes = "F6"
    sheet.auto_filter.ref = f"A5:N{end-1}"
    sheet.sheet_view.showGridLines = False
    for col, width in zip("ABCDEFGHIJKLMN", [6, 43, 19, 22, 9, 10, 10, 12, 19, 17, 17, 18, 18, 21]):
        sheet.column_dimensions[col].width = width
    navy = PatternFill("solid", fgColor="173B59")
    for row_number in (1, 5):
        for cell in sheet[row_number]:
            cell.fill = navy
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
    for cell in sheet[end]:
        cell.fill = PatternFill("solid", fgColor="E8F1F8")
        cell.font = Font(color="173B59", bold=True)
    for row in sheet.iter_rows(min_row=6, max_row=end, min_col=9, max_col=13):
        for cell in row:
            cell.number_format = '#,##0.00'
    sheet.row_dimensions[1].height = 30
    sheet.row_dimensions[5].height = 32
    sheet.print_title_rows = "1:5"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth = 1
    sheet.print_options.horizontalCentered = True
    out = BytesIO()
    workbook.save(out)
    return out.getvalue()


def export_inventory_pdf(inventory, summary):
    """Multi-page printable A4 landscape statement; embedded Cyrillic font."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not font_path.is_file():
        raise ValueError("Не найден шрифт для формирования PDF")
    if "InventoryCyr" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("InventoryCyr", str(font_path)))
    normal = ParagraphStyle("inv", fontName="InventoryCyr", fontSize=7.0, leading=10)
    heading = ParagraphStyle("inv_title", parent=normal, fontSize=14, leading=18)
    doc = SimpleDocTemplate(None, pagesize=landscape(A4), leftMargin=26, rightMargin=26,
                            topMargin=23, bottomMargin=23)
    buf = BytesIO()
    doc.filename = buf
    line = lambda value: Paragraph(xml_escape(str(value if value is not None else "")), normal)
    story = [
        Paragraph("ИНСТРАКТ | Ведомость результатов инвентаризации", heading),
        Spacer(1, 10),
        line("Объект: " + str(inventory.get("site", "")) +
             "  |  Дата: " + str(inventory.get("date", "")) +
             "  |  Документ: " + str(inventory.get("title", ""))),
        line(summary["costBasis"]),
        Spacer(1, 10),
    ]
    headings = ["№", "Наименование", "Бренд", "ID/№", "Учёт", "Факт", "Δ",
                "Цена закупки", "План, ₽", "Факт, ₽", "Недост., ₽", "Излиш., ₽", "Статус"]
    data = [[line(v) for v in headings]]
    for row in summary["rows"]:
        fmt = lambda number: "—" if number is None else f"{number:,.2f}".replace(",", " ")
        data.append([line(row["no"]), line(row["item"]), line(row["brand"]), line(row["sku"]),
                     line(fmt(row["expected"])), line(fmt(row["actual"])),
                     line(fmt(row["difference"])), line(fmt(row["purchasePrice"])),
                     line(fmt(row["planValue"])), line(fmt(row["knownFactValue"])),
                     line(fmt(row["shortageValue"])), line(fmt(row["surplusValue"])),
                     line(row["status"])])
    table = Table(data, colWidths=[22, 122, 66, 58, 35, 35, 34, 58, 61, 61, 66, 66, 65], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F1F8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.HexColor("#8198AA")),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#CFDAE3")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([
        table, Spacer(1, 13),
        line(f'Позиции: {summary["positions"]}; проверено: {summary["checked"]}; '
             f'не проверено: {summary["unchecked"]}.'),
        line(f'По закупочной стоимости, ₽: план {summary["plannedPurchaseValue"]}; '
             f'факт (только проверенные) {summary["knownFactPurchaseValue"]}; '
             f'недостачи {summary["shortagePurchaseValue"]}; '
             f'излишки {summary["surplusPurchaseValue"]}.'),
        Spacer(1, 8),
        line("Первичные расхождения не являются установленным ущербом. Остаточная стоимость не рассчитывается."),
        Spacer(1, 20),
        line("Проверяющий: __________________  МОЛ: __________________  Утверждающий: __________________"),
    ])
    doc.build(story)
    return buf.getvalue()
