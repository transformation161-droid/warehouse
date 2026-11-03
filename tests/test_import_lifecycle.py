"""An import belongs to the server process, not a disposable request thread."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from server.database import ROOT

CHILD = '''
import os, time
from pathlib import Path
import server.decision_import as worker

def inspect(folder):
    print('ready', flush=True)
    time.sleep(float(os.environ['DEMO_WORK_SECONDS']))
    print('finished', flush=True)
worker.inspect=inspect
worker.worker_main(Path(os.environ['DEMO_JOB_FOLDER']), 'inspect')
'''
PARENT = '''
import os, subprocess, sys, threading
children=[]
def launch():
    process=subprocess.Popen([sys.executable,'-u','-c',os.environ['DEMO_CHILD_CODE']],
        env={**os.environ,'KSU_IMPORT_PARENT_PID':str(os.getpid())},stdout=subprocess.PIPE,text=True)
    children.append(process)
    assert process.stdout.readline().strip()=='ready'
thread=threading.Thread(target=launch)
thread.start();thread.join()
if os.environ['DEMO_EXIT_PARENT']=='1':
    print('parent exiting',flush=True)
    os._exit(0)
output,_=children[0].communicate(timeout=5)
assert children[0].returncode==0,(children[0].returncode,output)
assert 'finished' in output,output
print('worker survived request thread',flush=True)
'''


@unittest.skipUnless(sys.platform.startswith('linux'),'Linux worker parent watchdog')
class ImportLifecycle(unittest.TestCase):
    def exercise(self, exit_parent):
        import json
        with tempfile.TemporaryDirectory(prefix='ksu-parent-test-') as folder:
            path=Path(folder)
            (path/'meta.json').write_text(json.dumps({'database':str(path/'DEMO.sqlite3')}))
            result=subprocess.run([sys.executable,'-u','-c',PARENT],cwd=ROOT,
                env={**os.environ,'DEMO_JOB_FOLDER':folder,'DEMO_CHILD_CODE':CHILD,
                     'DEMO_WORK_SECONDS':'30' if exit_parent else '1.5',
                     'DEMO_EXIT_PARENT':'1' if exit_parent else '0'},
                capture_output=True,text=True,timeout=8)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout

    def test_worker_survives_request_thread_retirement(self):
        self.assertIn('worker survived request thread',self.exercise(False))

    def test_worker_stops_when_server_process_exits(self):
        self.assertIn('parent exiting',self.exercise(True))
