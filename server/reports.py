"""Reports are recomputed from authorized server records, never client totals."""
import csv
import base64
import os
import re
import io
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.sax.saxutils import escape
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as ReportImage
from .service import fail


def money(qty, price):
    return float((Decimal(str(qty))*Decimal(str(price))).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def build_report(state, params):
    kind=params.get('kind','inventory'); group=params.get('group','site'); today=date.today()
    if kind not in ('inventory','findings','audits','weekly') or group not in ('site','period','category','owner','item'):
        fail('Неизвестный вид отчёта или группировка')
    start=params.get('from',''); end=params.get('to',''); period=params.get('period','all')
    if period not in ('all','range','month','quarter','year'):
        fail('Неизвестный период')
    if period in ('month','quarter','year'):
        start=date(today.year, 1 if period=='year' else ((today.month-1)//3)*3+1 if period=='quarter' else today.month,1).isoformat()
        last_month=12 if period=='year' else ((today.month-1)//3)*3+3 if period=='quarter' else today.month
        next_month=date(today.year+1,1,1) if last_month==12 else date(today.year,last_month+1,1)
        end=(next_month-timedelta(days=1)).isoformat()
    if kind=='weekly':
        start=(today-timedelta(days=today.weekday())).isoformat();end=(date.fromisoformat(start)+timedelta(days=6)).isoformat()
    for value in (start,end):
        if value:
            try: date.fromisoformat(value)
            except ValueError: fail('Неверная дата отчёта')
    if start and end and start>end: fail('Начало периода позже окончания')
    sites={s['id']:s['name'] for s in state['sites']}
    def site_ok(x): return params.get('site','all')=='all' or x['siteId']==params['site']
    def matches(x):
        day=(x.get('date') or x['createdAt'])[:10]
        return site_ok(x) and (not start or day>=start) and (not end or day<=end)
    buckets={}
    def bucket(key, fields): return buckets.setdefault(key, {f:0 for f in fields})
    if kind=='inventory':
        parents={x['id']:x for x in state['inventories'] if matches(x)}
        fields=['positions','shortage','surplus','approved','recovered']
        headers=['Группа','Позиции','Первичные недостачи, ₽','Первичные излишки, ₽','Утверждённый итог, ₽','Взыскано, ₽']
        for line in state['inventoryLines']:
            inv=parents.get(line['inventoryId'])
            if not inv: continue
            key=sites[inv['siteId']] if group=='site' else inv['date'][:7] if group=='period' else line.get({'category':'category','owner':'owner','item':'item'}[group]) or 'Не задано'
            b=bucket(key,fields); b['positions']+=1
            shortage=money(max(Decimal(str(line['expected']))-Decimal(str(line['actual'])),Decimal(0)),line['price'])
            surplus=money(max(Decimal(str(line['actual']))-Decimal(str(line['expected'])),Decimal(0)),line['price'])
            b['shortage']+=shortage; b['surplus']+=surplus
            if inv.get('stage') in ('approved','recovered'): b['approved']+=shortage
            if inv.get('stage')=='recovered': b['recovered']+=shortage
    elif kind=='findings':
        fields=['total','open','closed','overdue','repeat'];headers=['Группа','Всего','Открыто','Устранено','Просрочено','Повторные']
        for finding in state['findings']:
            if not matches(finding) or (params.get('status','all')!='all' and finding['status']!=params['status']): continue
            key=sites[finding['siteId']] if group=='site' else finding['createdAt'][:7] if group=='period' else finding.get('section' if group=='category' else 'owner') or 'Не задано'
            b=bucket(key,fields);b['total']+=1;b['open']+=finding['status']!='closed';b['closed']+=finding['status']=='closed'
            b['overdue']+=finding['status']!='closed' and bool(finding.get('dueDate')) and finding['dueDate']<today.isoformat()
            b['repeat']+=bool(finding.get('repeat'))
    elif kind=='audits':
        fields=['total','checked','issues','ok'];headers=['Группа','Проверки','Пункты','Нарушения','Соответствует']
        for ins in state['inspections']:
            if not matches(ins): continue
            key=sites[ins['siteId']] if group=='site' else ins['date'][:7] if group=='period' else ins['inspector'] if group=='owner' else 'Все направления'
            b=bucket(key,fields);b['total']+=1
            for result in ins['results']:
                b['checked']+=result['status']!='na';b['issues']+=result['status']=='fail';b['ok']+=result['status']=='ok'
    else:
        fields=['checks','new','closed','open','overdue'];headers=['Объект','Проверки за неделю','Новые за неделю','Устранено за неделю','Открыто сейчас','Просрочено сейчас']
        for site in state['sites']:
            if params.get('site','all') not in ('all',site['id']):continue
            b=bucket(site['name'],fields)
            b['checks']=sum(matches(x) and x['siteId']==site['id'] for x in state['inspections'])
            for f in state['findings']:
                if f['siteId']!=site['id']:continue
                b['new']+=matches(f);b['closed']+=bool(f.get('closedAt')) and start<=f['closedAt'][:10]<=end
                b['open']+=f['status']!='closed';b['overdue']+=f['status']!='closed' and bool(f.get('dueDate')) and f['dueDate']<today.isoformat()
    rows=[[key]+[round(value[f],2) for f in fields] for key,value in sorted(buckets.items())]
    return {'headers':headers,'rows':rows,'period':f'{start or "Все даты"} — {end or "сейчас"}', 'kind':kind}


def safe_cell(value):
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')): return "'"+value
    return value


def export(report, extension):
    headers=report['headers']; rows=report['rows']
    if extension=='csv':
        out=io.StringIO();writer=csv.writer(out,delimiter=';');writer.writerow(headers)
        writer.writerows([[safe_cell(v) for v in row] for row in rows])
        return ('\ufeff'+out.getvalue()).encode(), 'text/csv; charset=utf-8'
    out=io.BytesIO()
    if extension=='xlsx':
        book=Workbook();sheet=book.active;sheet.title='Складской учет' if os.getenv('KSU_OFFLINE_MODE') is not None else 'ДЕМО · Отчёт';sheet.append(headers)
        for row in rows:sheet.append([safe_cell(v) for v in row])
        sheet.freeze_panes='B2';sheet.auto_filter.ref=sheet.dimensions
        for col in sheet.columns:sheet.column_dimensions[col[0].column_letter].width=32
        if rows:
            chart=BarChart();chart.title='Показатели' if os.getenv('KSU_OFFLINE_MODE') is not None else 'Демонстрационные показатели';chart.add_data(Reference(sheet,min_col=2,max_col=3 if len(headers)>2 else 2,min_row=1,max_row=len(rows)+1),titles_from_data=True)
            chart.set_categories(Reference(sheet,min_col=1,min_row=2,max_row=len(rows)+1));sheet.add_chart(chart,'A'+str(len(rows)+4))
        meta=book.create_sheet('Параметры');meta.append(['ИНСТРАКТ | Складской учет' if os.getenv('KSU_OFFLINE_MODE') is not None else 'ДЕМОНСТРАЦИОННЫЕ ДАННЫЕ',report['period']]);meta.append(['Первичные расхождения не являются установленным ущербом'])
        book.save(out);return out.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    if extension!='pdf':fail('Формат не поддерживается')
    font=Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    if not font.exists():fail('На сервере отсутствует шрифт DejaVu Sans для PDF',503)
    if 'KSU' not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont('KSU',str(font)))
    styles=getSampleStyleSheet();style=styles['Normal'];style.fontName='KSU';style.fontSize=8;style.leading=11
    cell=lambda x:Paragraph(escape(str(x)),style)
    table=Table([[cell(x) for x in row] for row in [headers]+rows],repeatRows=1,colWidths=[750/len(headers)]*len(headers))
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e8f1fa')),('GRID',(0,0),(-1,-1),.3,colors.grey),('VALIGN',(0,0),(-1,-1),'TOP')]))
    story=[cell('ИНСТРАКТ | Складской учет'+('' if os.getenv('KSU_OFFLINE_MODE') is not None else ' · ДЕМОНСТРАЦИОННЫЕ ДАННЫЕ')),cell(report['period']),Spacer(1,14),table,Spacer(1,12),cell('Первичные расхождения не являются установленным ущербом. Утверждённый итог и взыскано показаны отдельно.')]
    if os.getenv('KSU_OFFLINE_MODE') is not None:
        logo=(Path(__file__).resolve().parent.parent/'assets/instrakt-logo.svg').read_text()
        original=base64.b64decode(re.search(r'data:image/png;base64,([^\"]+)',logo).group(1))
        story.insert(0,ReportImage(io.BytesIO(original),width=100,height=48))
    SimpleDocTemplate(out,pagesize=landscape(A4),leftMargin=40,rightMargin=40).build(story)
    return out.getvalue(),'application/pdf'
