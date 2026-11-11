"""Explicit PostgreSQL transport policies. Never log a DSN or infer a downgrade.

The Amvera ingress terminates TLS: client encryption is not server identity or
end-to-end encryption. Internal plaintext is a separate, narrow opt-in policy.
"""
import ipaddress
import os
from pathlib import Path
import re
import socket

import psycopg
from psycopg.conninfo import conninfo_to_dict

PROFILES=('verify-full','amvera-external','amvera-internal')
INTERNAL_HOST='cnpg-instrakt-ksu-rw'
PRIVATE_NETWORKS=tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
LIBPQ_ENV=set(('PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGPASSWORD PGPASSFILE PGSERVICE PGSERVICEFILE PGOPTIONS PGAPPNAME PGCONNECT_TIMEOUT PGSSLMODE PGREQUIRESSL PGSSLROOTCERT PGSSLCERT PGSSLKEY PGSSLCRL PGSSLCRLDIR PGSSLCOMPRESSION PGSSLSNI PGSSLNEGOTIATION PGSSLMINPROTOCOLVERSION PGSSLMAXPROTOCOLVERSION PGGSSENCMODE PGGSSLIB PGKRBSRVNAME PGCHANNELBINDING PGTARGETSESSIONATTRS PGLOADBALANCEHOSTS PGREQUIREAUTH PGREQUIREPEER').split())
DNS_NAME=re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}')


def profile():
    value=os.getenv('KSU_PG_TRANSPORT_PROFILE','verify-full')
    if value not in PROFILES:raise ValueError('Unknown PostgreSQL transport profile')
    return value


def private_address(value):
    try:address=ipaddress.ip_address(value)
    except ValueError:return False
    return any(address in network for network in PRIVATE_NETWORKS)


def validate_transport(dsn,managed=False):
    # libpq parse exceptions can include the secret input: replace, never echo it.
    try:info=conninfo_to_dict(dsn)
    except Exception:raise ValueError('Invalid PostgreSQL connection parameters') from None
    if not info.get('user') or not info.get('dbname') or not (info.get('host') or info.get('hostaddr')):raise ValueError('Explicit PostgreSQL host, database and user are required')
    if info.get('service') or os.getenv('PGSERVICE'):raise ValueError('Explicit connection parameters required; service profiles are not supported')
    if '=' in info.get('dbname','') or info.get('dbname','').startswith(('postgres://','postgresql://')):raise ValueError('Database name must not contain nested connection settings')
    selected=profile();host=info.get('host') or info.get('hostaddr') or ''
    mode=info.get('sslmode',os.getenv('PGSSLMODE',''))
    if selected=='verify-full':
        if host not in ('localhost','127.0.0.1','::1') and not host.startswith('/') and mode!='verify-full':
            raise ValueError('Remote PostgreSQL requires sslmode=verify-full')
        if managed:
            if info.get('sslmode')!='verify-full' or not info.get('host') or ',' in host or host.startswith('/'):
                raise ValueError('Managed connections require a single hostname and explicit verify-full TLS')
            ca=info.get('sslrootcert','')
            if not ca or not Path(ca).is_file():raise ValueError('Explicit trusted CA file required')
        return info
    if any(os.getenv(key) for key in LIBPQ_ENV):
        raise ValueError('Amvera connection settings must be explicit; inherited libpq settings forbidden')
    # No alternative IP, host list, Unix socket, implicit mode/port, service or
    # libpq extensions which could redirect/alter these two provider contracts.
    allowed={'host','port','user','dbname','password','passfile','connect_timeout','application_name','sslmode'}
    if set(info)-allowed or info.get('port')!='5432':raise ValueError('Amvera requires explicit port 5432 and supported single-host parameters')
    if selected=='amvera-external':
        expected=os.getenv('KSU_AMVERA_PG_EXTERNAL_HOST','')
        if not DNS_NAME.fullmatch(expected) or expected in ('localhost.localdomain',) or host!=expected or info.get('sslmode')!='require':
            raise ValueError('Amvera external requires the explicitly pinned DNS hostname and sslmode=require')
        info.update(sslsni='1',sslnegotiation='postgres')
    else:
        if host!=INTERNAL_HOST or info.get('sslmode')!='disable' or os.getenv('KSU_AMVERA_INTERNAL_NETWORK')!='confirmed-private':
            raise ValueError('Amvera internal plaintext requires explicit private-network opt-in and the approved service hostname')
        try:addresses={r[4][0] for r in socket.getaddrinfo(host,5432,type=socket.SOCK_STREAM)}
        except OSError:raise ValueError('Amvera internal service DNS unavailable; no external fallback') from None
        if not addresses or not all(private_address(a) for a in addresses):raise ValueError('Amvera internal service must resolve exclusively to RFC1918 IPv4 addresses')
        # Pin the validated destination for libpq and native tools, closing the
        # gap between this DNS check and a second resolver call at connect time.
        info['hostaddr']=sorted(addresses)[0]
    info['gssencmode']='disable'
    return info


def connect(dsn,**kwargs):
    info=validate_transport(dsn)
    connection=psycopg.connect(**{**info,**kwargs})
    try:
        client=bool(connection.pgconn.ssl_in_use)
        if profile()=='amvera-external' and not client:raise ValueError('Amvera external client TLS required')
        if profile()=='amvera-internal' and (client or connection.info.hostaddr!=info['hostaddr'] or not private_address(connection.info.hostaddr)):
            raise ValueError('Amvera internal connection escaped the approved private plaintext route')
    except Exception:
        connection.close();raise
    return connection


def session_transport(connection):
    """Non-secret observations. pg_stat_ssl describes the last hop, not the client."""
    selected=profile();client=bool(connection.pgconn.ssl_in_use)
    row=connection.execute('select ssl from pg_stat_ssl where pid=pg_backend_pid()').fetchone()
    backend=bool(row[0]) if row else False
    if selected=='verify-full' and (not client or not backend):raise ValueError('Verified direct TLS session required')
    if selected=='amvera-external' and not client:raise ValueError('Client-to-ingress TLS required')
    if selected=='amvera-internal' and (client or backend or not private_address(connection.info.hostaddr)):
        raise ValueError('Unexpected Amvera internal transport')
    verified=selected=='verify-full'
    return {'profile':selected,'client_tls':client,'postgres_tls':backend,
        'server_identity_verified':verified,'tls_verified':verified,
        'tls_scope':'verified-endpoint' if verified else 'client-to-ingress-only' if client else 'none',
        'end_to_end_tls_claimed':False}
