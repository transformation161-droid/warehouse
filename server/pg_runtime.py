"""Explicit PostgreSQL image entrypoint, with no silent SQLite fallback or provisioning."""
import os
import sys

if not os.environ.get('KSU_DATABASE_URL'):
    raise SystemExit('KSU_DATABASE_URL is required; PostgreSQL mode never initializes a fallback database')
os.umask(0o077)
os.execv(sys.executable,[sys.executable,'-m','server','run','--host','0.0.0.0'])
