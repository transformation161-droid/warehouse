"""Expose whitelisted CI diagnostics in check annotations; no environment or sources."""
import json
import os
from pathlib import Path
import re
import sys


def command(level,title,message):
    def escape(value):return str(value).replace('%','%25').replace('\r','%0D').replace('\n','%0A')
    print('::'+level+' title='+escape(title)+'::'+escape(message),flush=True)


def run(root):
    root=Path(root);path=root/'prerelease-metrics.json';lines=['| Revision | Cohort | n | Median, s | p95, s |','| --- | --- | --- | --- | --- |']
    if path.exists():
        data=json.loads(path.read_text())
        for revision in ('before','after'):
            for phase in ('first_open','warm'):
                values=data.get(revision,{}).get(phase)
                if not values:continue
                message=f"{revision} {phase}: n={values['n']}; median={values['median_seconds']}; p95={values['p95_seconds']} s"
                command('notice','Paired analytics latency',message)
                lines.append(f"| {revision} | {phase} | {values['n']} | {values['median_seconds']} | {values['p95_seconds']} |")
        command('notice','Acceptance status','verified='+str(data.get('verified',False))+'; snapshot='+str(data.get('same_snapshot_comparison_cohorts',False))+'; restore='+str(data.get('independent_restore',{}).get('verified',False)))
    else:command('warning','Acceptance diagnostics','No aggregate metrics file was produced')
    log=root/'prerelease-log.txt'
    if log.exists():
        # Exception type and numeric assertion expressions only; never echo arbitrary
        # log lines, credentials, URLs or record payloads into annotations.
        errors=re.findall(r'^(AssertionError|RuntimeError|TimeoutError|OperationalError|CheckViolation)(?::.*)?$',log.read_text(),re.M)
        frames=re.findall(r'^\s+File "[^\n"]*/(experiments/prerelease/[^\n"]+)", line (\d+)',log.read_text(),re.M)
        if errors:command('warning','Acceptance exception',errors[-1]+(' at '+frames[-1][0]+':'+frames[-1][1] if frames else ''))
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as output:output.write('\n'.join(lines)+'\n')


if __name__=='__main__':run(sys.argv[1])
