"""Reject incomplete or forged migration reports before any database access."""
import unittest
from server.pg_admin import TABLES
from server.pg_finalize import validate_report


class FinalizeReport(unittest.TestCase):
    def report(self):
        return {'source_read_only':True,'sessions_revoked':True,'tables':{
            table:{'count':2 if table=='users' else 0,'sha256':'0'*64} for table in TABLES}}

    def test_complete_legacy_report_is_supported(self):
        report=self.report();self.assertEqual(validate_report(report),report['tables'])

    def test_missing_unknown_or_malformed_report_is_refused(self):
        for value in (None,[],{}, {'source_read_only':'true'}, {'sessions_revoked':True}):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_report(value)
        mutations=[lambda r:r['tables'].pop('users'),lambda r:r['tables'].update(unexpected={}),
            lambda r:r.update(sessions_revoked=False),lambda r:r.update(source_read_only=False)]
        for mutate in mutations:
            report=self.report();mutate(report)
            with self.assertRaises(ValueError):validate_report(report)

    def test_invalid_counts_and_checksums_are_refused(self):
        for count in (-1,True,1.5,'2',None):
            report=self.report();report['tables']['records']['count']=count
            with self.subTest(count=count),self.assertRaises(ValueError):validate_report(report)
        for digest in ('mock','g'*64,0,None,'0'*63):
            report=self.report();report['tables']['records']['sha256']=digest
            with self.subTest(digest=digest),self.assertRaises(ValueError):validate_report(report)
