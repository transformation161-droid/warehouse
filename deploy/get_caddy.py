"""Install a pinned upstream release after verification of its official SHA512 digest."""
import argparse
import hashlib
import io
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = '2.11.7'
# https://github.com/caddyserver/caddy/releases/download/v2.11.7/caddy_2.11.7_checksums.txt
SHA512 = {
    'amd64': 'a7a433a1b133efc3c8d10eb0b99d52a24b5ef5c322dc77f5282182b1c0402139ab83f3a99f0c52409df77d20123fb0b523edad8a66d8f5e49136197bf61ef0e7',
    'arm64': '3db36ba90c7a6e8dda40ee3dd71fa08844c76b5fb08f61b31e5e78d2ed38e71c51dc7baed875e50d1ca1279196e84302967237386ae87c91ae9f2aaceada682e',
}


def install(output):
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if not arch or platform.system() != 'Linux':
        raise ValueError('Supported platforms: Linux amd64/arm64')
    target = Path(output)
    if target.exists() or target.is_symlink():
        raise FileExistsError('Refusing to overwrite an existing binary')
    url = f'https://github.com/caddyserver/caddy/releases/download/v{VERSION}/caddy_{VERSION}_linux_{arch}.tar.gz'
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read(100_000_001)
    if len(archive) > 100_000_000 or hashlib.sha512(archive).hexdigest() != SHA512[arch]:
        raise ValueError('Official Caddy SHA512 verification failed; binary was not installed')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
        member = bundle.getmember('caddy')
        if not member.isfile():
            raise ValueError('Unexpected Caddy archive member')
        binary = bundle.extractfile(member).read()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o755)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(binary)
    target.chmod(0o755)
    print(f'Caddy {VERSION}: official SHA512 verified, installed at {target}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    install(parser.parse_args().output)
