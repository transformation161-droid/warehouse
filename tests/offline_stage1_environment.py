"""Reproducible native PostgreSQL contracts on disposable synthetic TLS clusters."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from tests.pg_environment import local_cluster,test_environment,IMAGE
from tests.pg_tools import prepare


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    output=Path(args.output).resolve();output.mkdir(mode=0o700,parents=True,exist_ok=True)
    with local_cluster() as cluster:
        env=test_environment(cluster);env.update(prepare(IMAGE,output/'client-tools'))
        modules=['tests.test_offline_stage1.PostgreSQLOffline','tests.test_postgres',
                 'tests.test_postgres_versions','tests.test_postgres_analytics']
        with (output/'private-postgres-tests.txt').open('w') as log:
            result=subprocess.run([sys.executable,'-m','unittest',*modules,'-v'],env=env,stdout=log,stderr=log)
        # Only aggregate results are eligible for CI artifacts, not DB/dump/TLS keys or logs.
        summary={'synthetic_only':True,'backend':'postgresql17','cpu_per_db':'0.5','ram_per_db':'1 GiB',
                 'modules':modules,'exit_code':result.returncode}
        (output/'summary.json').write_text(json.dumps(summary,indent=2))
        print(json.dumps(summary))
        if result.returncode:raise SystemExit(result.returncode)


if __name__=='__main__':main()
