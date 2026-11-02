"""Bounded XLSX read prototype. No application writes, formula evaluation or network."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import posixpath
from pathlib import Path
import re
import sqlite3
import time
import zipfile
from defusedxml.ElementTree import fromstring, iterparse

MAX_FILE = 30_000_000
MAX_UNPACKED = 256_000_000
MAX_PART = 192_000_000
MAX_ROWS = 100_000
MAX_COLUMNS = 128
MAX_SHARED_STRINGS = 300_000
MAX_STRING_BYTES = 64_000_000
MAX_TEXT = 4096
MAX_CELLS = 2_000_000
MAIN = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
CELL = re.compile(r'^([A-Z]{1,3})([1-9][0-9]{0,6})$')


class Rejected(ValueError):
    pass


class CellError(Rejected):
    def __init__(self, row, column, reason, fix):
        self.row, self.column, self.reason, self.fix = row, column, reason, fix
        super().__init__(f'{column}{row}: {reason}. {fix}')


def column_number(letters):
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - 64
    return value


class BoundedPart:
    def __init__(self, handle, maximum):
        self.handle, self.maximum, self.count = handle, maximum, 0

    def read(self, size=-1):
        # XML parsers consume in chunks; a read-all call is bounded too.
        data = self.handle.read(min(size if size >= 0 else self.maximum+1, 64*1024))
        self.count += len(data)
        if self.count > self.maximum:
            raise Rejected('Превышен размер XML-части книги')
        return data


@dataclass
class Limits:
    file: int = MAX_FILE
    unpacked: int = MAX_UNPACKED
    rows: int = MAX_ROWS
    columns: int = MAX_COLUMNS
    cells: int = MAX_CELLS
    seconds: int = 180


class Reader:
    def __init__(self, path, scratch, limits=None):
        self.path, self.scratch = Path(path), Path(scratch)
        self.limits = limits or Limits()
        self.started = time.monotonic()
        self.cache = OrderedDict()
        self.string_db = None
        self.archive = None
        self.strings_loaded = False

    def check_time(self):
        if time.monotonic()-self.started > self.limits.seconds:
            raise Rejected('Истекло время обработки книги; уменьшите выбранный диапазон')

    def __enter__(self):
        try:
            if self.path.stat().st_size > self.limits.file:
                raise Rejected('Размер XLSX превышает 30 МБ')
            self.archive = zipfile.ZipFile(self.path)
            entries = self.archive.infolist()
            if len(entries) > 512 or len({x.filename for x in entries}) != len(entries):
                raise Rejected('Слишком много частей или повторяющиеся имена в XLSX')
            if sum(x.file_size for x in entries) > self.limits.unpacked or any(x.file_size > MAX_PART for x in entries):
                raise Rejected('Превышен безопасный распакованный объём XLSX')
            for entry in entries:
                if entry.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED) or len(entry.filename)>512 or entry.flag_bits & 1 or entry.filename.startswith('/') or '..' in entry.filename.split('/'):
                    raise Rejected('Зашифрованная или некорректная структура XLSX')
                if entry.file_size > max(1, entry.compress_size)*400:
                    raise Rejected('Недопустимая степень сжатия XLSX')
            workbook = self.small_xml('xl/workbook.xml')
            relationships = self.small_xml('xl/_rels/workbook.xml.rels')
            targets = {}
            self.shared_path = None
            for rel in relationships:
                if rel.get('TargetMode') == 'External':
                    continue
                target = rel.get('Target','')
                target = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
                if not target.startswith('xl/') or '..' in target.split('/'):
                    raise Rejected('Некорректная внутренняя ссылка XLSX')
                targets[rel.get('Id')] = target
                if rel.get('Type','').endswith('/sharedStrings'):
                    self.shared_path = target
            sheets = workbook.find(MAIN+'sheets')
            if sheets is None or not 1 <= len(sheets) <= 20:
                raise Rejected('В книге должно быть от 1 до 20 листов')
            self.sheets = [(sheet.get('name',''), targets.get(sheet.get(REL+'id'))) for sheet in sheets]
            if any(not name or len(name)>120 for name,_ in self.sheets) or len({name for name,_ in self.sheets})!=len(self.sheets):
                raise Rejected('Некорректные названия листов')
            if any(not path or path not in self.archive.namelist() for _,path in self.sheets):
                raise Rejected('Не найден XML листа')
            self.date1904 = workbook.find(MAIN+'workbookPr') is not None and workbook.find(MAIN+'workbookPr').get('date1904') in ('1','true')
            self.formats=['General']
            if 'xl/styles.xml' in self.archive.namelist():
                from openpyxl.styles.numbers import BUILTIN_FORMATS
                styles=self.small_xml('xl/styles.xml');custom=styles.find(MAIN+'numFmts');xfs=styles.find(MAIN+'cellXfs')
                codes={int(x.get('numFmtId')):x.get('formatCode','') for x in custom} if custom is not None else {}
                if any(len(code)>512 for code in codes.values()) or (xfs is not None and len(xfs)>4096):raise Rejected('Превышен безопасный предел форматов ячеек')
                if xfs is not None:
                    self.formats=[codes.get(int(x.get('numFmtId','0')),BUILTIN_FORMATS.get(int(x.get('numFmtId','0')),'General')) for x in xfs]

            self.scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = self.scratch/'shared-strings.sqlite3'
            with target.open('xb'): pass
            self.string_db = sqlite3.connect(target)
            self.string_db.execute('pragma cache_size=-2048')
            self.string_db.execute('pragma journal_mode=off')
            self.string_db.execute('create table strings(id integer primary key, value text not null)')
            return self
        except Exception:
            self.close()
            raise

    def small_xml(self, name):
        entry = self.archive.getinfo(name)
        if entry.file_size > 4_000_000:
            raise Rejected('Слишком большой служебный XML книги')
        with self.archive.open(name) as part:
            data = part.read(4_000_001)
        if len(data)>4_000_000:
            raise Rejected('Слишком большой служебный XML книги')
        return fromstring(data, forbid_dtd=True)

    def close(self):
        if self.string_db:
            self.string_db.close(); self.string_db = None
        if self.archive:
            self.archive.close(); self.archive = None

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def elements(self, name, tag):
        with self.archive.open(name) as part:
            stream = BoundedPart(part, min(MAX_PART,self.archive.getinfo(name).file_size))
            def scan():
                stack = []
                for event, element in iterparse(stream, events=('start','end'), forbid_dtd=True):
                    if event == 'start':
                        stack.append(element)
                    else:
                        if element.tag == MAIN+tag:
                            self.check_time()
                            yield element
                            if len(stack)>1: stack[-2].remove(element)
                            element.clear()
                        stack.pop()
            yield scan()

    def load_strings(self):
        if self.strings_loaded: return
        self.strings_loaded = True
        if not self.shared_path: return
        total = 0
        with self.elements(self.shared_path, 'si') as entries:
            for index, entry in enumerate(entries):
                text = ''.join(t.text or '' for t in entry.iter(MAIN+'t'))
                total += len(text.encode())
                if index >= MAX_SHARED_STRINGS or len(text)>MAX_TEXT or total>MAX_STRING_BYTES:
                    raise Rejected('Превышен предел общих строк XLSX')
                self.string_db.execute('insert into strings values(?,?)',(index,text))
            self.string_db.commit()

    def string(self, index):
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index]
        value = self.string_db.execute('select value from strings where id=?',(index,)).fetchone()
        if value is None: raise Rejected('Некорректная ссылка на общую строку XLSX')
        self.cache[index] = value[0]
        if len(self.cache)>512: self.cache.popitem(last=False)
        return value[0]

    def rows(self, sheet=0, first=1, last=MAX_ROWS, columns=None, allow_cached=False, allow_missing_cached=False, allow_cell_errors=False):
        if not 0 <= sheet < len(self.sheets) or not 1 <= first <= last <= self.limits.rows:
            raise Rejected('Недопустимый лист или диапазон строк')
        self.load_strings()
        total_cells = 0
        previous = 0
        with self.elements(self.sheets[sheet][1], 'row') as rows:
            for row in rows:
                number = int(row.get('r', previous+1))
                if number <= previous or number > self.limits.rows:
                    raise Rejected('Неверный номер или слишком много строк листа')
                previous = number
                if number > last: break
                if number < first: continue
                result = {}; seen = set()
                for cell in row:
                    if cell.tag != MAIN+'c': continue
                    reference = CELL.fullmatch(cell.get('r',''))
                    if not reference or int(reference[2]) != number:
                        raise Rejected('Некорректный адрес ячейки')
                    letters = reference[1]
                    column = column_number(letters)
                    if column > self.limits.columns or letters in seen:
                        raise Rejected('Слишком много столбцов или повторный адрес ячейки')
                    seen.add(letters)
                    total_cells += 1
                    if total_cells>self.limits.cells: raise Rejected('Превышен предел ячеек')
                    if columns is not None and letters not in columns: continue
                    value = cell.find(MAIN+'v')
                    formula = cell.find(MAIN+'f')
                    raw = value.text if value is not None else None
                    kind = cell.get('t','n')
                    if kind == 's' and raw is not None:
                        raw = self.string(int(raw))
                    elif kind == 'inlineStr':
                        raw = ''.join(t.text or '' for t in cell.iter(MAIN+'t'))
                    if raw is not None and len(raw)>MAX_TEXT:
                        raise CellError(number,letters,'слишком длинное значение','Сократите значение до 4096 символов')
                    if kind == 'e' and not allow_cell_errors:
                        raise CellError(number,letters,'ошибка Excel','Исправьте ошибку в исходной ячейке')
                    if formula is not None:
                        if not allow_cached:
                            raise CellError(number,letters,'формула в импортируемой ячейке','Явно выберите сохранённые значения формул или замените формулу значением')
                        if raw is None and not allow_missing_cached:
                            raise CellError(number,letters,'нет сохранённого результата формулы','Пересчитайте и сохраните книгу в Excel')
                    result[letters] = {'value':raw,'type':kind,'formula':None if formula is None else (formula.text if formula.text is not None else str(dict(formula.attrib))),
                                       'style':cell.get('s','0')}
                    if cell.get('s') is not None:
                        try:result[letters]['numberFormat']=self.formats[int(cell.get('s'))]
                        except (IndexError,ValueError):raise CellError(number,letters,'Некорректный формат ячейки','Пересохраните книгу в Excel')
                yield number, result
