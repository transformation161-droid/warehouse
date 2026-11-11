"""Synthetic PostgreSQL SSLRequest terminator: TLS client -> plaintext test PG.

Only test traffic on a disposable Docker bridge. No protocol/credential logging.
"""
import asyncio
import ssl
import struct


async def handle(reader,writer):
    backend=None
    try:
        request=await reader.readexactly(8)
        if request!=struct.pack('!II',8,80877103):return
        writer.write(b'S');await writer.drain()
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain('/tls/cert.pem','/tls/key.pem')
        await writer.start_tls(context)
        incoming,backend=await asyncio.open_connection('ksu-managed-source',5432)
        async def relay(src,dst):
            try:
                while data:=await src.read(65536):dst.write(data);await dst.drain()
            finally:dst.close()
        await asyncio.gather(relay(reader,backend),relay(incoming,writer))
    except (ConnectionError,OSError,ssl.SSLError,asyncio.IncompleteReadError):pass
    finally:
        writer.close()
        if backend:backend.close()


async def main():
    server=await asyncio.start_server(handle,'0.0.0.0',5432)
    async with server:await server.serve_forever()


if __name__=='__main__':asyncio.run(main())
