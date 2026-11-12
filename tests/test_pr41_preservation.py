"""Approved PR40 schema/auth/business remains intact; PR41 changes only UI/reports."""
from pathlib import Path
import subprocess

BASE='a923a5aa1e210e3f18d65cff3db60f95844e1c76'


def test_postgres_schema_acl_models_offline_templates_and_business_are_preserved():
    names=subprocess.run(['git','ls-tree','-r','--name-only',BASE,'server','offline','assets'],check=True,capture_output=True,text=True).stdout.splitlines()
    for name in names:
        if name=='server/inventory_statement.py':continue
        old=subprocess.run(['git','show',BASE+':'+name],check=True,capture_output=True).stdout
        assert Path(name).read_bytes()==old,name
