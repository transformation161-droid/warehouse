import json
import os
import urllib.request


def main():
    if os.getuid()==0:
        os.setgroups([]);os.setgid(10001);os.setuid(10001)
    # Loopback only, preserve the public Host for TrustedHostMiddleware.
    from urllib.parse import urlsplit
    origin=os.environ['KSU_ORIGIN']
    host=urlsplit(origin).netloc
    request=urllib.request.Request('http://127.0.0.1:8000/api/health',headers={'Host':host})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=2) as response:
        if json.load(response).get('status')!='ok':raise SystemExit(1)


if __name__=='__main__':main()
