"""Cgroup and disk sampling; only aggregate metrics leave this private test volume."""
from pathlib import Path
import threading
import time


def values(path):return {k:int(v) for k,v in (line.split() for line in Path(path).read_text().splitlines())}


def size(path):
    total=0
    for entry in Path(path).rglob('*'):
        try:
            if entry.is_file() and not entry.is_symlink():total+=entry.stat().st_size
        except FileNotFoundError:pass
    return total


class Resources:
    def __init__(self,root):
        self.root=Path(root);self.stop=threading.Event();self.phase='setup';self.samples={};self.start=time.monotonic()
        self.cpu_start=values('/sys/fs/cgroup/cpu.stat');self.events_start=values('/sys/fs/cgroup/memory.events')
        self.thread=threading.Thread(target=self.watch,daemon=True);self.thread.start()
    def watch(self):
        while not self.stop.wait(.5):
            stat=values('/sys/fs/cgroup/memory.stat');total=int(Path('/sys/fs/cgroup/memory.current').read_text())
            sample=self.samples.setdefault(self.phase,{'anon_peak_bytes':0,'file_cache_peak_bytes':0,'total_peak_bytes':0,'disk_peak_bytes':0})
            for key,value in [('anon_peak_bytes',stat['anon']),('file_cache_peak_bytes',stat['file']),('total_peak_bytes',total),('disk_peak_bytes',size(self.root))]:sample[key]=max(sample[key],value)
            for label,pattern in [('staging','*/work'),('backup','*/exports')]:
                key=label+'_disk_peak_bytes';sample[key]=max(sample.get(key,0),sum(size(path) for path in self.root.glob(pattern)))
    def finish(self):
        self.stop.set();self.thread.join(timeout=5)
        cpu=values('/sys/fs/cgroup/cpu.stat');events=values('/sys/fs/cgroup/memory.events')
        return {'phases':self.samples,'elapsed_seconds':round(time.monotonic()-self.start,3),
            'cpu_usage_seconds':round((cpu['usage_usec']-self.cpu_start['usage_usec'])/1e6,3),
            'cpu_throttled_seconds':round((cpu.get('throttled_usec',0)-self.cpu_start.get('throttled_usec',0))/1e6,3),
            'cpu_limit':Path('/sys/fs/cgroup/cpu.max').read_text().strip(),
            'memory_limit':Path('/sys/fs/cgroup/memory.max').read_text().strip(),
            'kernel_total_peak_bytes':int(Path('/sys/fs/cgroup/memory.peak').read_text()),
            'oom_kills':events['oom_kill']-self.events_start['oom_kill']}
