"""Wholly invented source; no bytes or personal fields from the owner's workbooks."""
from decimal import Decimal
import io
from openpyxl import Workbook
from server.decision_import import FIELDS


def rows():
    result=[]
    for i in range(39):
        zero=i>=34
        row=dict(sourceId=f'DEMO-{i+1:03}',date='2026-07-08' if i<3 else '2026-06-10',
            site='ДЕМО · Север' if i%2==0 else 'ДЕМО · Юг',document='ДЕМО · Акт '+str(i%2+1),
            decisionType='Найдено' if zero else 'Списание',toolType='ДЕМО · Уровень',
            quantity='0' if zero else '2' if i<22 else '1',
            cost='0' if zero else '26787.50' if i==33 else '10000',
            residual='0' if zero else '14827.87' if i==33 else '3000',
            reason='ДЕМО · Недостача' if i%3==0 else 'ДЕМО · Повреждение',caseCategory='ДЕМО',
            investigation='ДЕМО · Проверяется',decision='ДЕМО · Решение',
            reimbursable='0' if zero else '14827.87' if i==33 else '3000',
            reimbursed='0' if zero else '17992.36' if i==33 else '3000',
            responsible='ДЕМО · Сотрудник',comment='Вымышленные данные для приёмки')
        result.append(row)
    return result


def workbook(items=None,custom=False):
    book=Workbook();sheet=book.active;sheet.title='Ввод_списаний'
    if custom:sheet.append(['ДЕМО · Инструкция']);sheet.append([])
    sheet.append(list(FIELDS.values()))
    for row in rows() if items is None else items:sheet.append([row.get(k) for k in FIELDS])
    # Formatting-only tail is deliberate; it must not create a decision.
    from openpyxl.styles import PatternFill
    sheet.cell(475,1).fill=PatternFill(fill_type='solid',fgColor='EEEEEE')
    other=book.create_sheet('ДЕМО · Другой лист');other.append(['Не исходный отчёт'])
    output=io.BytesIO();book.save(output);return output.getvalue()
