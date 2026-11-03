"""Build deterministic 18–30 MB fictional XLSX; never reads an uploaded workbook."""
import argparse
from decimal import Decimal
import base64
import hashlib
import json
from pathlib import Path
import random
import tempfile
from xml.sax.saxutils import escape
import zipfile
import zlib

HEADERS = ['Row ID','Act date','Site','Document','Decision type','Tool type','Quantity',
           'Cost','Residual cost','Reason','Case category','Investigation status','Decision',
           'Reimbursable','Reimbursed','Responsible','Comment']
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def generate(path, target, shared=False, *, variant=0, site="DEMO site", rich=False, scratch=None):
    path=Path(path)
    if path.exists(): raise FileExistsError(path)
    if not 18_000_000 <= target <= 30_000_000: raise ValueError('18–30 MB synthetic fixtures only')
    rng=random.Random(11010+variant)
    expected=hashlib.sha256();projection=hashlib.sha256();cells=0;count=0
    totals={k:Decimal(0) for k in ("quantity","cost","residual","reimbursable","reimbursed")}
    with tempfile.TemporaryDirectory(prefix='ksu-generate-',dir=scratch) as folder:
        sheet=Path(folder)/'sheet.xml';strings=Path(folder)/'strings.xml'
        with sheet.open('w',encoding='utf-8') as data, strings.open('w',encoding='utf-8') as sst:
            compressors=[zlib.compressobj(6,zlib.DEFLATED,-15),zlib.compressobj(6,zlib.DEFLATED,-15)]
            compressed=0
            def write(index,text):
                nonlocal compressed
                (data if index==0 else sst).write(text)
                compressed+=len(compressors[index].compress(text.encode()))
            write(0,f'<worksheet xmlns="{NS}"><sheetData>')
            write(1,f'<sst xmlns="{NS}">')
            for rowno in range(1,100_000):
                count=rowno
                if rowno==1:
                    values=[('inlineStr',h,None) for h in HEADERS]
                else:
                    noise=base64.b64encode(rng.randbytes(1500)).decode()
                    quantity=rowno%7+1
                    values=[('n',str(variant*100000+rowno-1),None),('inlineStr','2026-11-01',None),
                        ('inlineStr',site,None),('inlineStr','DEMO-'+str(rowno//5),None),
                        ('inlineStr','DEMO decision',None),('inlineStr','DEMO tool '+str(rowno%8),None),
                        ('n',str(quantity),None),('n',str(quantity*10.5),f'G{rowno}*10.5' if rowno%11==0 else None),
                        ('n',str(quantity*8.25),None),('inlineStr','DEMO reason',None),('inlineStr','DEMO case',None),
                        ('inlineStr','DEMO pending',None),('inlineStr','DEMO review',None),('n','0',None),
                        ('n','0',None),('inlineStr','DEMO person',None),('s' if shared else 'inlineStr',noise,None)]
                    if rich:
                        for index,multiplier in ((7,'10.1250'),(8,'8.0625'),(13,'1.1250'),(14,'0.3125')):
                            amount='0' if index==13 and rowno%11==0 or index==14 and rowno%5==0 else format(quantity*Decimal(multiplier),'.4f')
                            values[index]=('n',amount,f'G{rowno}*10.125' if index==7 and rowno%11==0 else None)
                        projected=[value for _,value,_ in values]
                        projected[0]=projected[0].zfill(10)
                        projection.update(json.dumps([rowno,projected],ensure_ascii=False,separators=(',',':')).encode()+b'\n')
                        for field,index in (('quantity',6),('cost',7),('residual',8),('reimbursable',13),('reimbursed',14)):totals[field]+=Decimal(projected[index])
                    if shared:write(1,'<si><t>'+escape(noise)+'</t></si>')
                xml=f'<row r="{rowno}">';logical={}
                for index,(kind,value,formula) in enumerate(values):
                    col=chr(65+index);raw=str(rowno-2) if kind=='s' else value
                    logical[col]={'value':value,'type':kind,'formula':formula,'style':'1' if rich and index==0 and rowno>1 else '0'}
                    style=' s="1"' if rich and index==0 and rowno>1 else ''
                    if style:logical[col]['numberFormat']='0000000000'
                    xml+=f'<c r="{col}{rowno}" t="{kind}"{style}>'
                    if formula is not None:xml+='<f>'+escape(formula)+'</f>'
                    xml+=('<is><t>'+escape(raw)+'</t></is>') if kind=='inlineStr' else '<v>'+escape(raw)+'</v>'
                    xml+='</c>'
                xml+='</row>'
                expected.update(json.dumps([rowno,logical],sort_keys=True,ensure_ascii=False).encode());cells+=17
                write(0,xml)
                if compressed>=target-40_000:break
            write(0,'</sheetData></worksheet>');write(1,'</sst>')
        rootrels='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        rels='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        if shared:rels+='<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        if rich:rels+='<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        rels+='</Relationships>'
        types='<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        if shared:types+='<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        if rich:types+='<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        types+='</Types>'
        with zipfile.ZipFile(path,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as book:
            book.writestr('[Content_Types].xml',types);book.writestr('_rels/.rels',rootrels)
            book.writestr('xl/workbook.xml',f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="DEMO data" sheetId="1" r:id="rId1"/></sheets></workbook>')
            book.writestr('xl/_rels/workbook.xml.rels',rels)
            if rich:book.writestr('xl/styles.xml',f'<styleSheet xmlns="{NS}"><numFmts count="1"><numFmt numFmtId="164" formatCode="0000000000"/></numFmts><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs></styleSheet>')
            book.write(sheet,'xl/worksheets/sheet1.xml')
            if shared:book.write(strings,'xl/sharedStrings.xml')
        difference=target-path.stat().st_size
        if not 0<=difference<=65535:raise RuntimeError('Synthetic size estimate out of bounds: '+str(difference))
        with zipfile.ZipFile(path,'a') as book:book.comment=b'D'*difference
    assert path.stat().st_size==target
    return {'synthetic_only':True,'bytes':target,'rows':count,'cells':cells,'shared_strings':shared,'digest':expected.hexdigest(),'projection_digest':projection.hexdigest(),'totals':{k:format(v,'f') for k,v in totals.items()},'rich':rich,'variant':variant,'site':site}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('output');p.add_argument('--bytes',type=int,required=True);p.add_argument('--shared',action='store_true');a=p.parse_args()
    print(json.dumps(generate(a.output,a.bytes,a.shared),indent=2))
