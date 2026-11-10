"""Synthetic owner setup validation and demo guards; no real identities/passwords in test output."""
import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from server.production_init import configuration,template
from server.database import seed_demo


def approved():
    value=template();value['accounts']={'manager':{'username':'qa-lead','name':'СИНТЕТИКА · Руководитель'},'inspector':{'username':'qa-field','name':'СИНТЕТИКА · Исполнитель'}}
    value['sites']=[{'id':'qa-site','name':'СИНТЕТИКА · Объект'}];value['inspectorSiteIds']=['qa-site'];value['checklistApproved']=True
    return value


class InitialSetup(unittest.TestCase):
    def test_owner_values_and_approval_required_no_password_or_third_role_input(self):
        self.assertEqual(len(configuration(approved())['questions']),22)
        with self.assertRaises(ValueError):configuration(template())
        for field,change in [('password',lambda v:v['accounts']['manager'].update(password='forbidden')),
            ('role',lambda v:v['accounts'].update(viewer={'username':'qa-view','name':'СИНТЕТИКА'})),
            ('demo',lambda v:v['accounts']['manager'].update(name='ДЕМО · manager')),
            ('assign',lambda v:v.update(inspectorSiteIds=['unknown'])),
            ('questions',lambda v:v.update(questions=[])),
            ('approval',lambda v:v.update(checklistApproved=False))]:
            value=approved();change(value)
            with self.subTest(field=field),self.assertRaises(ValueError):configuration(value)

    def test_production_demo_guard_runs_before_seed_file_or_database_access(self):
        with patch.dict(os.environ,{'KSU_DEPLOYMENT_PROFILE':'production'}),patch('pathlib.Path.read_text',side_effect=AssertionError('seed read forbidden')):
            with self.assertRaises(ValueError):seed_demo(None,None,None)
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'should-not-exist.sqlite3'
            result=subprocess.run([__import__('sys').executable,'-m','server','init-demo','--generate','--db',str(target)],
                env={**os.environ,'KSU_DEPLOYMENT_PROFILE':'production'},capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertFalse(target.exists());self.assertFalse(list(Path(folder).iterdir()))

    def test_authorizer_and_old_offline_package_validation_are_preserved(self):
        base='3f53781d5d4b7e1402048f405f7a30f860d1b2e7'
        for filename,names in [('server/database.py',('connect','user')),('server/offline.py',('validate_package','receipt','store_receipt'))]:
            previous=ast.parse(subprocess.run(['git','show',base+':'+filename],check=True,capture_output=True,text=True).stdout)
            current=ast.parse(Path(filename).read_text())
            for name in names:
                old=next(n for n in ast.walk(previous) if isinstance(n,ast.FunctionDef) and n.name==name)
                new=next(n for n in ast.walk(current) if isinstance(n,ast.FunctionDef) and n.name==name)
                self.assertEqual(ast.dump(old),ast.dump(new),name)
