"""Synthetic-only static prototype. No application imports, database or write API."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import struct
import time
from urllib.parse import urlsplit
import zlib

ROOT=Path(__file__).resolve().parent

def photo():
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    rows=b''.join(b'\0'+b''.join(bytes([x%256,y*2,120]) for x in range(128)) for y in range(96))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',128,96,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')

PHOTO=photo()
TEMPLATE={'id':'demo-checklist-v1','version':1,'questions':[
    {'id':'demo-q1','section':'Складской учёт','title':'ДЕМО · На инструменте есть маркировка'},
    {'id':'demo-q2','section':'Хранение','title':'ДЕМО · Материалы размещены безопасно'},
    {'id':'demo-q3','section':'Документы','title':'ДЕМО · Документы доступны для проверки'}]}
TEMPLATE_JSON=json.dumps(TEMPLATE,ensure_ascii=False,sort_keys=True,separators=(',',':'))

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):self.respond(405,b'No write API in stage 0','text/plain')
    do_PUT=do_POST
    do_DELETE=do_POST
    def respond(self,status,body,kind):
        self.send_response(status)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store' if kind=='application/json' else 'no-cache')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)
    def do_GET(self):
        if self.headers.get('Host','').split(':')[0] not in self.server.allowed_hosts:
            return self.respond(403,b'Unexpected host','text/plain')
        if self.headers.get('Cookie'):return self.respond(400,b'Use an isolated test profile or 127.0.0.1; this prototype does not accept cookies','text/plain')
        path=urlsplit(self.path).path
        if path==self.server.fail_asset:return self.respond(503,b'DEMO install failure','text/plain')
        if path=='/demo-package.json':
            now=int(time.time()*1000)
            value={'demo':True,'deploymentId':'ksu-stage0-synthetic','userId':'demo-inspector',
                   'site':'ДЕМО · Нова','visitId':'demo-visit-v1','issuedAt':now,'expiresAt':now+86400000,
                   'templateJson':TEMPLATE_JSON,'templateSha256':hashlib.sha256(TEMPLATE_JSON.encode()).hexdigest(),
                   'photo':{'id':'demo-reference-photo-v1','url':'/demo-photo.png','sha256':hashlib.sha256(PHOTO).hexdigest()}}
            return self.respond(200,json.dumps(value,ensure_ascii=False).encode(),'application/json')
        if path=='/demo-photo.png':return self.respond(200,PHOTO,'image/png')
        if path=='/build.json':return self.respond(200,json.dumps({'build':self.server.build}).encode(),'application/json')
        routes={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/store.js':'store.js',
                '/prototype.css':'prototype.css','/sw.js':'sw.js','/manifest.webmanifest':'manifest.webmanifest','/icon.svg':'icon.svg'}
        if path=='/ksu.css':target=self.server.style
        elif path in routes:target=ROOT/routes[path]
        else:return self.respond(404,b'Not found','text/plain')
        content=target.read_bytes()
        if path=='/sw.js':content=content.replace(b'__BUILD__',self.server.build.encode())
        kind={'.js':'text/javascript','.css':'text/css','.html':'text/html; charset=utf-8','.svg':'image/svg+xml','.webmanifest':'application/manifest+json'}.get(target.suffix,'text/plain')
        self.respond(200,content,kind)
    do_HEAD=do_GET


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8010)
    parser.add_argument('--build',default='stage0-v1');parser.add_argument('--allowed-host',action='append',default=[])
    parser.add_argument('--style',type=Path,default=ROOT.parent.parent/'styles.css')
    parser.add_argument('--fail-asset',choices=['/prototype.css','/demo-photo.png'])
    parser.add_argument('--allow-container-http',action='store_true',help='Loopback Docker publishing only; launcher enforces host binding')
    parser.add_argument('--cert');parser.add_argument('--key')
    args=parser.parse_args()
    if not args.build.replace('-','').replace('_','').isalnum():parser.error('Invalid build')
    if bool(args.cert)!=bool(args.key):parser.error('Certificate and key required together')
    if args.host not in ('127.0.0.1','localhost') and not args.cert and not args.allow_container_http:parser.error('LAN serving requires trusted HTTPS')
    server=ThreadingHTTPServer((args.host,args.port),Handler)
    server.allowed_hosts={'localhost','127.0.0.1',*args.allowed_host};server.style=args.style;server.build=args.build;server.fail_asset=args.fail_asset
    if args.cert:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert,args.key);server.socket=context.wrap_socket(server.socket,server_side=True)
    print(f'SYNTHETIC STAGE 0 ONLY: {args.host}:{args.port}',flush=True)
    server.serve_forever()

if __name__=='__main__':main()
