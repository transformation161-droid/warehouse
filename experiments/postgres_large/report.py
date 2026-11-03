"""Publish aggregate results and a scenario-based capacity estimate, never private fixtures."""
import argparse
import json
from pathlib import Path

MIB=1024**2
GIB=1024**3


def main():
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('--docs',type=Path,default=Path('docs'))
    args=parser.parse_args();m=json.loads((args.source/'postgres-large-metrics.json').read_text())
    db=json.loads((args.source/'postgres-large-db-resources.json').read_text());container=json.loads((args.source/'postgres-large-container.json').read_text())
    assert not m.get('failure') and not m['cap_raised'] and m['app_resources']['oom_kills']==0
    assert container['ExitCode']==0 and not container['OOMKilled'] and 'backup_after_restore' in m
    assert all(item['oom_kills']==0 for item in db.values())
    aggregate={**m,'database_resources':db,'container_exit':container}
    (args.docs/'POSTGRESQL_LARGE_ACCEPTANCE.json').write_text(json.dumps(aggregate,ensure_ascii=False,indent=2)+'\n')
    imports=[m[k] for k in ('18','18_then_18b','30')]
    per_row=max((item['after']['database_bytes']-item['before']['database_bytes'])/item['expected_rows'] for item in imports)
    base=m['18']['before']['database_bytes'];acc=m['18_then_18b']['after'];rows=acc['counts']['decision_records']
    backup_per_row=m['backup_repeat']['bytes']/rows
    out=['# Вместимость PostgreSQL — измерения Issue #20','',
         'Только оценка для согласования. Лимиты/тарифы не менялись. MB XLSX — 10⁶ байт, МиБ/ГиБ — 2²⁰/2³⁰.',
         'Исходные агрегаты: [POSTGRESQL_LARGE_ACCEPTANCE.json](POSTGRESQL_LARGE_ACCEPTANCE.json).','',
         '## Измеренный рост','',
         '| Сценарий | Строк | Upload / preview / commit, с | БД после, МиБ | Прирост БД, МиБ |',
         '|---|---:|---:|---:|---:|']
    for name,item in zip(('18 MB','ещё 18 MB','30 MB отдельно'),imports):
        out.append(f"| {name} | {item['expected_rows']:,} | {item['upload_seconds']} / {item['preview_seconds']} / {item['commit_seconds']} | {item['after']['database_bytes']/MIB:.2f} | {(item['after']['database_bytes']-item['before']['database_bytes'])/MIB:.2f} |")
    out.extend(['',f"Две книги: **{rows:,} строк**, БД {acc['database_bytes']/MIB:.2f} МиБ; cap 256 МиБ.",
        'Admission оценивает normalizedBytes × 3 + rows × 192 + 2 МиБ и прибавляет текущий pg_database_size.',
        f"Для 18 MB оценка {m['18']['admission_estimate_bytes']/MIB:.2f} МиБ; для 30 MB — {m['30']['admission_estimate_bytes']/MIB:.2f} МиБ.",
        'Поэтому 18→30 и 30→18 отклоняются заранее; после 18+18 новая 30 MB тоже отклоняется.',
        'Отказ не меняет counts/audit и выдаёт явное cap reached. Это консервативный admission guard,',
        'а не обещание заполнить каждый свободный байт и не квота WAL/всего сервера.','',
        f"Максимальный измеренный прирост на строку с индексами и audit: **{per_row:.0f} байт** (~{per_row/1024:.2f} КиБ).",
        f"Независимый пакет двух книг: {m['backup_repeat']['bytes']/MIB:.2f} МиБ (~{backup_per_row:.0f} байт/строку).",
        'Длинные малосжимаемые синтетические комментарии сохранены и в исходной строке, и в audit.',
        'Это модель такой нагрузки; другой текст, длина комментариев, фото/операционные записи и индексы',
        'изменят коэффициенты. Размер ZIP/XLSX сам по себе не является нормой потребления PostgreSQL.','',
        '## Пример: по одной книге на объект в месяц, 12 месяцев','',
        'Количество объектов — сценарии, не утверждённый план организации. Расчёт: строки = объекты × 12 ×',
        'строки книги; БД = начальный размер + строки × максимальный измеренный байт/строку. Запас 30%',
        'ниже относится к admission/росту БД; WAL, staging и резервные копии в него не включены.','',
        '| Объектов | Книга | Строк/год | БД, ГиБ | С запасом 30%, ГиБ | Один backup, ГиБ |',
        '|---:|---|---:|---:|---:|---:|'])
    for objects in (5,10,20):
        for label in ('18','30'):
            annual=objects*12*m[label]['expected_rows'];database=(base+annual*per_row)/GIB
            out.append(f'| {objects} | {label} MB/мес | {annual:,} | {database:.2f} | {database*1.3:.2f} | {annual*backup_per_row/GIB:.2f} |')
    out.extend(['','## Варианты для согласования','',
        '1. Сохранить 256 МиБ как ограниченный демонстрационный контур: две такие 18-MB книги уже близки',
        '   к пределу допуска следующего большого импорта. Меньшие диапазоны уменьшают staging, но не',
        '   решают годовое накопление. Архивирование допустимо только после отдельного процесса с проверкой',
        '   восстановления/доступа; автоматическое удаление истории или разбиение по БД здесь не выполняется.',
        '2. После согласования фактических объёмов установить явные бюджет/квоты для годового накопления.',
        '   Например, 5 объектов × 18 MB/месяц требуют около 8 ГиБ лимита БД с указанным запасом;',
        '   10 — около 16 ГиБ, 20 — около 32 ГиБ. При ежемесячных 30 MB — ориентиры 16/32/64 ГиБ.',
        '   Это предложения, не изменения конфигурации. Сначала решить логические версии (#21), иначе',
        '   исправления будут искажать суммы и дополнительно занимать место.',
        '3. Если будет согласован больший контур, отдельно пересмотреть backup cap 512 МиБ, свободный диск',
        '   под dump + пакет + restore, retention и transport. Нельзя поднять только лимит БД и считать',
        '   независимое резервирование готовым. 7 ежедневных + 4 недельных полных копии требуют до 11',
        '   размеров пакета на конец года (консервативно); добавить место для хотя бы одного restore.',
        '4. Считать WAL/checkpoint/PITR по отдельному бюджету, не умножать текущий WAL тестового кластера',
        '   на число файлов. В метриках сохранены WAL LSN и размеры, таблицы/индексы/TOAST и общий диск.',
        '   WAL содержит транзакции нескольких временных БД; он не равен размеру текущего реестра.',
        '5. RAM/CPU не повышать по одному пику total: каждый из трёх контейнеров работал на 0,5 CPU/1 ГиБ',
        '   без swap и OOM. Файловый cache занимает свободный лимит и вытесняется; PostgreSQL file также',
        '   включает shared/mapped страницы. Anon и total представлены отдельно. Результат не доказывает',
        '   пригодность для массовой конкуренции; повторить целевую нагрузку перед выбором тарифа.','',
        'Стоимость storage/реплик/независимых копий и региона согласовать отдельно по [вариантам размещения](POSTGRESQL_OPTIONS.md).',
        'Никаких заказов, подключения к Amvera или автоматического изменения лимитов в этой работе нет.'])
    (args.docs/'POSTGRESQL_CAPACITY.md').write_text('\n'.join(out)+'\n')


if __name__=='__main__':main()
