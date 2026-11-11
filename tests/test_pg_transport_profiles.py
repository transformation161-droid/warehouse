"""Synthetic provider transport matrix, no live provider or credentials."""
import ast
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from server.pg_transport import validate_transport,connect,session_transport
from server.managed_pg import transport
from server.pg_backup import tool_environment

BASE='host=synthetic-amvera.invalid port=5432 user=ksu_runtime dbname=ksu_app password=SYNTHETIC_ONLY'
EXTERNAL={'KSU_PG_TRANSPORT_PROFILE':'amvera-external','KSU_AMVERA_PG_EXTERNAL_HOST':'synthetic-amvera.invalid'}
INTERNAL={'KSU_PG_TRANSPORT_PROFILE':'amvera-internal','KSU_AMVERA_INTERNAL_NETWORK':'confirmed-private'}
INSIDE=BASE.replace('synthetic-amvera.invalid','cnpg-instrakt-ksu-rw')+' sslmode=disable'


class Profiles(unittest.TestCase):
    def test_default_remote_requires_verify_full_and_managed_ca(self):
        with patch.dict(os.environ,{},clear=True),tempfile.TemporaryDirectory() as folder:
            ca=Path(folder)/'synthetic.pem';ca.write_text('SYNTHETIC')
            for mode in ('disable','allow','prefer','require','verify-ca',''):
                with self.subTest(mode=mode),self.assertRaises(ValueError):validate_transport(BASE+(' sslmode='+mode if mode else ''))
            self.assertEqual(transport(BASE+' sslmode=verify-full sslrootcert='+str(ca))['sslmode'],'verify-full')
            with self.assertRaises(ValueError):transport(BASE+' sslmode=verify-full')

    def test_external_requires_explicit_profile_host_mode_no_routes_or_defaults(self):
        with patch.dict(os.environ,EXTERNAL,clear=True):
            info=transport(BASE+' sslmode=require');self.assertEqual(info['sslmode'],'require')
            env=tool_environment(BASE+' sslmode=require');self.assertEqual(env['PGSSLMODE'],'require');self.assertEqual(env['PGSSLSNI'],'1');self.assertEqual(env['PGGSSENCMODE'],'disable')
            self.assertNotIn('PGSSLROOTCERT',env);self.assertNotIn('PGHOSTADDR',env)
            for suffix in ('',' sslmode=disable',' sslmode=prefer',' sslmode=verify-full',' sslmode=allow',' sslmode=verify-ca',
                ' sslmode=require hostaddr=127.0.0.1',' sslmode=require sslrootcert=/tls/ca.pem',' sslmode=require service=other',' sslmode=require options=bad'):
                with self.subTest(suffix=suffix),self.assertRaises(ValueError):transport(BASE+suffix)
            for old,new in [('synthetic-amvera.invalid','other.invalid'),('synthetic-amvera.invalid','one,two'),('synthetic-amvera.invalid','127.0.0.1'),('5432','5433')]:
                with self.assertRaises(ValueError):transport(BASE.replace(old,new)+' sslmode=require')
            for key in ('PGHOSTADDR','PGSSLMODE','PGSSLROOTCERT','PGSERVICE','PGOPTIONS','PGGSSENCMODE'):
                with patch.dict(os.environ,{key:'SYNTHETIC'}),self.assertRaises(ValueError):transport(BASE+' sslmode=require')
        with patch.dict(os.environ,{'KSU_PG_TRANSPORT_PROFILE':'amvera-external'},clear=True),self.assertRaises(ValueError):transport(BASE+' sslmode=require')
        with patch.dict(os.environ,{'KSU_PG_TRANSPORT_PROFILE':'typo'},clear=True),self.assertRaises(ValueError):transport(BASE+' sslmode=require')

    def test_internal_exact_service_private_dns_pinned_address_no_fallback(self):
        def dns(address):return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,5432))]
        with patch.dict(os.environ,INTERNAL,clear=True):
            with patch('server.pg_transport.socket.getaddrinfo',return_value=dns('10.12.0.7')):
                self.assertEqual(transport(INSIDE)['hostaddr'],'10.12.0.7')
                self.assertEqual(tool_environment(INSIDE)['PGHOSTADDR'],'10.12.0.7')
                for bad in (INSIDE.replace('disable','require'),INSIDE+' hostaddr=10.12.0.7',INSIDE.replace('cnpg-instrakt-ksu-rw','other-private'),INSIDE.replace('5432','5433')):
                    with self.assertRaises(ValueError):transport(bad)
            for address in ('8.8.8.8','127.0.0.1','169.254.1.1','0.0.0.0','::1','fc00::1'):
                with patch('server.pg_transport.socket.getaddrinfo',return_value=dns(address)),self.assertRaises(ValueError):transport(INSIDE)
            with patch('server.pg_transport.socket.getaddrinfo',return_value=dns('10.12.0.7')+dns('8.8.8.8')),self.assertRaises(ValueError):transport(INSIDE)
            with patch('server.pg_transport.socket.getaddrinfo',side_effect=socket.gaierror),self.assertRaises(ValueError):transport(INSIDE)
        with patch.dict(os.environ,{'KSU_PG_TRANSPORT_PROFILE':'amvera-internal'},clear=True),self.assertRaises(ValueError):transport(INSIDE)

    def test_observations_distinguish_client_tls_from_postgres_and_identity(self):
        def connection(client,backend):
            return SimpleNamespace(pgconn=SimpleNamespace(ssl_in_use=client),info=SimpleNamespace(hostaddr='10.0.0.7'),execute=lambda _:SimpleNamespace(fetchone=lambda:(backend,)))
        for values,client,backend,identity in [({},True,True,True),(EXTERNAL,True,False,False),(INTERNAL,False,False,False)]:
            with patch.dict(os.environ,values,clear=True):
                observed=session_transport(connection(client,backend));self.assertEqual(observed['server_identity_verified'],identity);self.assertEqual(observed['postgres_tls'],backend)
                self.assertFalse(observed['end_to_end_tls_claimed'])
        with patch.dict(os.environ,EXTERNAL,clear=True),self.assertRaises(ValueError):session_transport(connection(False,False))
        with patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError):session_transport(connection(True,False))

    def test_release_business_ui_and_22_questions_are_byte_preserved(self):
        base='87373094b6b84acd56f2a7dc7627d73f84716562'
        paths=['release_online','offline','assets','server/service.py','server/models.py','server/checklists.py','server/checklist_template.py',
            'server/viewer.py','server/viewer_schema.py','server/database.py','server/offline.py','server/offline_release.py','server/reports.py','server/app.py']
        names=subprocess.run(['git','ls-tree','-r','--name-only',base,*paths],check=True,capture_output=True,text=True).stdout.splitlines()
        for name in names:
            old=subprocess.run(['git','show',base+':'+name],check=True,capture_output=True).stdout
            self.assertEqual(Path(name).read_bytes(),old,name)


if __name__=='__main__':unittest.main()
