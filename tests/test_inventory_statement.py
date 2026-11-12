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
         "actual": None, "purchasePrice": "12500.00", "brand": "Makita"},
        {"item": "Перфоратор", "sku": "002", "unit": "шт.", "expected": 1,
         "actual": 0, "purchasePrice": "18000.00", "brand": "Bosch"},
        {"item": "Лазерный уровень", "sku": "003", "unit": "шт.", "expected": 1,
         "actual": 2, "purchasePrice": "9500.00", "brand": "DeWalt"},
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
    assert result["rows"][1]["brand"] == "Bosch"
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
    assert ws["C5"].value == "Бренд"
    assert ws["N5"].value == "Статус"
    assert ws["C7"].value == "Bosch"
    assert ws["N6"].value == "Не проверено"
    assert ws["N7"].value == "Недостача"
    assert ws["N8"].value == "Излишек"
    assert ws["G6"].value is None   # not counted
    assert ws["G7"].value == 0      # counted but missing
    assert ws["L7"].value == 18000
    assert ws["M8"].value == 9500
    assert ws["L9"].value == 18000


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


def test_all_statuses_zero_plan_fractional_quantities_and_rounding():
    values=[{'item':'ДЕМО · '+str(i),'expected':e,'actual':a,'price':'2.675','brand':'ДЕМО · Бренд'}
            for i,(e,a) in enumerate([(0,None),(0,0),(0,1),('0.001',0)])]
    report=summarize_inventory(values)
    assert [r['status'] for r in report['rows']]==['Не проверено','Соответствует','Излишек','Недостача']
    assert report['rows'][3]['difference'].as_tuple().exponent==-3
    assert str(report['surplusPurchaseValue'])=='2.68'
    assert str(report['shortagePurchaseValue'])=='0.00'  # quantity shortage still has its own status
    ws=load_workbook(BytesIO(export_inventory_xlsx({'site':'ДЕМО','title':'ДЕМО'},report))).active
    assert ws['F9'].value==.001 and ws['H9'].value==-.001
    assert ws['G6'].value is None and ws['G7'].value==0
    assert ws['N9'].value=='Недостача'
    assert [ws.cell(10,c).value for c in (10,11,12,13)]==[0,2.68,0,2.68]


def test_new_text_columns_are_safe_and_printable():
    values=[{'item':'ДЕМО · '+('Длинное наименование '*20),'brand':'=HYPERLINK("https://example.invalid")','sku':'ДЕМО-1',
             'expected':1,'actual':1,'price':1}]
    ws=load_workbook(BytesIO(export_inventory_xlsx({'site':'ДЕМО','title':'ДЕМО'},summarize_inventory(values)))).active
    assert ws['C6'].data_type=='s' and ws['C6'].value.startswith("'=")
    assert ws['C6'].alignment.wrap_text and ws['N6'].alignment.wrap_text
    assert ws.row_dimensions[6].height>30 and ws.page_setup.fitToHeight==0
    assert ws.print_title_rows=='$1:$5' and ws.freeze_panes=='F6'


def test_real_pdf_contains_brands_statuses_exact_quantities_and_repeated_headers():
    from pypdf import PdfReader
    from server.inventory_statement import export_inventory_pdf
    values=[{'item':'ДЕМО · Позиция '+str(i),'brand':'СИНТЕТИКА-'+str(i),'sku':'TEST-'+str(i),'unit':'кг',
             'expected':'0.001','actual':(None,0,'.001','.002')[i%4],'price':100} for i in range(90)]
    report=summarize_inventory(values)
    reader=PdfReader(BytesIO(export_inventory_pdf({'site':'ДЕМО · Объект','title':'ДЕМО · Ведомость','date':'2026-11-12'},report)))
    assert len(reader.pages)>1
    text='\n'.join(p.extract_text() for p in reader.pages)
    for brand in ('СИНТЕТИКА-0','СИНТЕТИКА-89'):assert brand in text
    for status in ('Не проверено','Соответствует','Недостача','Излишек'):assert status in text
    assert '0.001' in text and '-0.001' in text
    assert sum('Бренд' in p.extract_text() and 'Статус' in p.extract_text() for p in reader.pages)>=len(reader.pages)-1
    # Every text run remains inside the printed page, including the last status column.
    for page in reader.pages:
        width=float(page.mediabox.width)
        def check_bounds(value,cm,tm,font,size):
            if value.strip():assert -1<=tm[4]+cm[4]<=width+1
        page.extract_text(visitor_text=check_bounds)


def test_pdf_handles_maximum_text_and_fractional_unit_prices_without_truncation():
    from pypdf import PdfReader
    from server.inventory_statement import export_inventory_pdf
    brand='ДЕМОБРЕНД'+'Б'*100
    rows=[{'item':'ДЕМО '+('Инструмент '*44),'brand':brand,'sku':'ID'+'A'*118,'sourceId':'SRC'+'B'*117,
           'expected':2,'actual':1,'price':'0.005'} for _ in range(8)]
    report=summarize_inventory(rows)
    pdf=export_inventory_pdf({'site':'ДЕМО','title':'ДЕМО'},report)
    text=''.join(''.join(p.extract_text().split()) for p in PdfReader(BytesIO(pdf)).pages)
    assert brand in text and '0.005' in text
    ws=load_workbook(BytesIO(export_inventory_xlsx({'site':'ДЕМО','title':'ДЕМО'},report))).active
    assert ws['I6'].value==.005 and ws['I6'].number_format=='#,##0.00############'
    assert ws['L6'].value==.01 and ws['M6'].value==0
