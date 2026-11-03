"""Validate configured transport without displaying connection strings."""
import os
from psycopg.conninfo import conninfo_to_dict


def validate_transport(dsn):
    info=conninfo_to_dict(dsn)
    if not info.get('user') or not info.get('dbname') or not (info.get('host') or info.get('hostaddr')):raise ValueError('Explicit PostgreSQL host, database and user are required')
    if info.get('service') or os.getenv('PGSERVICE'):raise ValueError('Explicit connection parameters required; service profiles are not supported')
    if '=' in info.get('dbname','') or info.get('dbname','').startswith(('postgres://','postgresql://')):raise ValueError('Database name must not contain nested connection settings')
    host=info.get('host') or info.get('hostaddr') or os.getenv('PGHOST','')
    mode=info.get('sslmode',os.getenv('PGSSLMODE',''))
    if host not in ('','localhost','127.0.0.1','::1') and not host.startswith('/') and mode!='verify-full':
        raise ValueError('Remote PostgreSQL requires sslmode=verify-full')
    return info
