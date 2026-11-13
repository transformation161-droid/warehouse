"""Synthetic facts and an independent scaled-integer oracle (no analytics imports)."""
import hashlib
import json
from uuid import uuid4
from server.database import now
from server.decision_versions import initial_document

MEASURES=('quantity','cost','residual','reimbursable','reimbursed')


def records(count=8):
    for i in range(count):
        yield {'sourceId':f'DEMO-{i:06}', 'date':None if i%11==0 else f'2026-{1+i%6:02}-{1+i%27:02}',
               'site':f'ДЕМО · Объект {1+i%3}', '_site':f's{1+i%3}',
               'document':f'ДЕМО · Акт {i//100:04}-{i%3}',
               'decisionType':f'ДЕМО · Решение {i%2}', 'toolType':f'ДЕМО · Инструмент {i%4}',
               'quantity':'0.000001' if i%5==0 else str(1+i%3),
               'cost':None if i%13==0 else '0.0000' if i%7==0 else f'{i%10000}.0001',
               'residual':'0' if i%3==0 else None if i%3==1 else '0.0150',
               'reimbursable':None if i%7==0 else '0' if i%4==0 else '5.1250',
               'reimbursed':None if i%5==0 else '2.0625' if i%4 else '3',
               'reason':None if i%5==0 else f'ДЕМО · Причина {i%3}',
               'caseCategory':f'ДЕМО · Категория {i%2}', 'investigation':f'ДЕМО · Статус {i%3}',
               'decision':'ДЕМО · Готовое решение', 'responsible':f'ДЕМО · МОЛ {i%4}',
               'comment':'=DEMO_SYNTHETIC_ONLY()', 'sourceCells':{'A':{'value':'ДЕМО · источник'}}}


def insert(db,items):
    """A legacy-data fixture permits missing dates; production XLSX validation stays intact."""
    actor=db.user('manager');signature=hashlib.sha256(uuid4().bytes).hexdigest();created=now();seen=set();result=[]
    with db.connect(actor) as con:
        con.execute('begin immediate')
        con.execute('insert into imports values(?,?,?,?)',(signature,actor['id'],json.dumps({'synthetic':True}),created))
        for index,original in enumerate(items,2):
            item=dict(original);site=item.pop('_site');ident=uuid4().hex
            con.execute('insert into decision_records values(?,?,?,?,?,?,?,?)',
                (ident,signature,signature,'DEMO legacy',index,site,actor['id'],json.dumps(item,ensure_ascii=False)))
            doc,version=initial_document(con,signature,site,item,actor,created,seen)
            con.execute('insert into decision_entries values(?,?,?,?,?)',(ident,doc,version,site,None))
            result.append({**item,'_site':site,'_doc':doc,'_id':ident})
    return result


def scaled(value,places):
    if value is None or value=='':return None
    text=str(value);whole,_,fraction=text.partition('.')
    return int(whole)*10**places+int((fraction+'0'*places)[:places])


def oracle(rows):
    """Integer minor units and independent unknown coverage of source fields."""
    result={'documents':str(len({r['_doc'] for r in rows})),'decisions':str(len(rows))};coverage={}
    for field in MEASURES:
        places=6 if field=='quantity' else 4;values=[]
        for row in rows:
            v=scaled(row.get(field),places)
            if v is not None:values.append(v)
        total=sum(values);unit=10**places
        result[field]=f'{total//unit}.{total%unit:0{places}}' if values else None
        coverage[field]=(len(values),len(rows)-len(values))
    return result,coverage
