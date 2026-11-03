from pathlib import Path
import tempfile
import unittest
import zipfile
from openpyxl import Workbook
from server.xlsx_stream import Reader, Rejected, CellError, Limits


class XLSXStream(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def book(self, rows=None):
        path=self.root/'source.xlsx';book=Workbook();sheet=book.active;sheet.title='DEMO'
        for row in rows or [['header','other'],['DEMO','00123'],['second',5]]:sheet.append(row)
        book.save(path);return path

    def replace(self,path,name,data):
        with zipfile.ZipFile(path) as source:parts={part.filename:source.read(part.filename) for part in source.infolist()}
        parts[name]=data
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as target:
            for key,value in parts.items():target.writestr(key,value)

    def test_range_original_coordinates_and_identifier_text(self):
        source=self.book()
        with Reader(source,self.root/'scratch') as reader:
            rows=list(reader.rows(first=2,last=3,columns={'B'}))
            self.assertEqual(rows[0][0],2);self.assertEqual(rows[0][1]['B']['value'],'00123')
            self.assertEqual(rows[1][1]['B']['value'],'5')
            self.assertNotIn('A',rows[0][1])
            # Re-reading a range is valid; shared strings are loaded only once.
            self.assertEqual(len(list(reader.rows(first=1,last=1))),1)

    def test_explicit_formula_cache_policy_and_missing_cache(self):
        source=self.book()
        xml='<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="2"><c r="A2"><f>1+2</f><v>3</v></c></row></sheetData></worksheet>'
        self.replace(source,'xl/worksheets/sheet1.xml',xml)
        with Reader(source,self.root/'a') as reader:
            with self.assertRaises(CellError) as error:list(reader.rows())
            self.assertEqual((error.exception.row,error.exception.column),(2,'A'))
        with Reader(source,self.root/'b') as reader:
            row=list(reader.rows(allow_cached=True))[0][1]['A']
            self.assertEqual(row['value'],'3');self.assertEqual(row['formula'],'1+2')
        self.replace(source,'xl/worksheets/sheet1.xml',xml.replace('<v>3</v>',''))
        with Reader(source,self.root/'c') as reader:
            with self.assertRaises(CellError):list(reader.rows(allow_cached=True))

    def test_external_entities_zip_expansion_and_traversal_rejected(self):
        source=self.book()
        self.replace(source,'xl/workbook.xml','<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>')
        with self.assertRaises(Exception):
            with Reader(source,self.root/'dtd'):pass
        source.unlink();source=self.book()
        with zipfile.ZipFile(source,'a',zipfile.ZIP_DEFLATED) as book:book.writestr('xl/bomb.xml','A'*1_000_000)
        with self.assertRaises(Rejected):
            with Reader(source,self.root/'bomb'):pass
        source.unlink();source=self.book()
        with zipfile.ZipFile(source,'a') as book:book.writestr('../escape.xml','bad')
        with self.assertRaises(Rejected):
            with Reader(source,self.root/'path'):pass

    def test_caps_on_bytes_cells_columns_and_long_values(self):
        source=self.book()
        with self.assertRaises(Rejected):
            with Reader(source,self.root/'size',Limits(file=10)):pass
        with Reader(source,self.root/'cells',Limits(cells=2)) as reader:
            with self.assertRaises(Rejected):list(reader.rows())
        with Reader(source,self.root/'columns',Limits(columns=1)) as reader:
            with self.assertRaises(Rejected):list(reader.rows())
        source.unlink();source=self.book([['header'],['x'*4097]])
        with Reader(source,self.root/'text') as reader:
            with self.assertRaises(CellError):list(reader.rows())
