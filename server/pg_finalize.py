"""Read-only verification of an existing migration before the launcher marks it ready."""
import json
import os
import re
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict
from .pg_admin import TABLES, fingerprints
from .pg_transport import validate_transport


def validate_report(report):
    if not isinstance(report,dict) or report.get('source_read_only') is not True or report.get('sessions_revoked') is not True:
        raise ValueError('Missing migration guarantees')
    tables=report.get('tables')
    if not isinstance(tables,dict) or set(tables)!=set(TABLES):raise ValueError('Unexpected migration tables')
    for table,value in tables.items():
        if not isinstance(value,dict) or set(value)!={'count','sha256'}:raise ValueError('Invalid table report')
        if type(value['count']) is not int or value['count']<0:raise ValueError('Invalid record count')
        if not isinstance(value['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',value['sha256']):raise ValueError('Invalid table checksum')
    if tables['users']['count']<2:raise ValueError('Demo accounts missing')
    return tables


def verify_database(report):
    expected=validate_report(report)
    for variable,role in (('KSU_DATABASE_URL','ksu_runtime'),('KSU_PG_BACKUP_DSN','ksu_backup')):
        info=conninfo_to_dict(os.environ[variable]);validate_transport(os.environ[variable])
        if info.get('host')!='127.0.0.1' or info.get('port','5432')!='5432' or info.get('dbname')!='postgres' or info.get('user')!=role:
            raise ValueError('Unexpected application database binding')
    with psycopg.connect(os.environ['KSU_PG_BACKUP_DSN'],connect_timeout=10) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        connection.execute("SET LOCAL statement_timeout = '60s'")
        if connection.execute('select max(version) from ksu.schema_version').fetchone()[0]!=2:raise ValueError('Unexpected schema version')
        if fingerprints(connection,True)!=expected:raise ValueError('Database differs from migration report; do not repeat migration')
    return {'verified':True}


def main():
    try:
        report=json.load(sys.stdin)
        print(json.dumps(verify_database(report)))
    except Exception as error:
        # Connection exceptions may contain credentials. Never print exception values or DSNs.
        raise SystemExit('Finalize verification failed: '+type(error).__name__+'. Inspect the private migration report and database locally.')


if __name__=='__main__':main()
