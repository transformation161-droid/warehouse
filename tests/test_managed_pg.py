"""Production boundary checks without provider calls or corporate data."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from server.managed_pg import tls,binding


class ManagedTLS(unittest.TestCase):
    def test_explicit_ca_hostname_and_verify_full_required_even_on_loopback(self):
        with tempfile.TemporaryDirectory() as folder:
            ca=Path(folder)/'synthetic-ca.pem';ca.write_text('synthetic test placeholder')
            base=f'host=127.0.0.1 dbname=ksu_app user=ksu_runtime sslrootcert={ca}'
            self.assertEqual(tls(base+' sslmode=verify-full')['user'],'ksu_runtime')
            for mode in ('disable','allow','prefer','require','verify-ca'):
                with self.subTest(mode=mode),self.assertRaises(ValueError):tls(base+' sslmode='+mode)
            with self.assertRaises(ValueError):tls('host=127.0.0.1 dbname=ksu_app user=ksu_runtime sslmode=verify-full')
            with self.assertRaises(ValueError):tls(base.replace('127.0.0.1','one,two')+' sslmode=verify-full')

    def test_secret_source_must_be_unambiguous(self):
        with patch.dict(os.environ,{'SYNTHETIC_BINDING':'private one','SYNTHETIC_BINDING_FILE':'private two'}):
            with self.assertRaises(ValueError):binding('SYNTHETIC_BINDING')
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError):binding('SYNTHETIC_BINDING')
