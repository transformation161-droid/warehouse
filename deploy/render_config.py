"""Generate non-secret production settings; reject shell/systemd/Caddy injection."""
import argparse
import re
from pathlib import Path


def render(hostname):
    if len(hostname) > 253 or not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', hostname):
        raise ValueError('Нужен DNS hostname без схемы, порта, пробелов и путей')
    return {
        'ksu.env': '\n'.join(['KSU_DB=/srv/ksu-data/ksu.sqlite3', f'KSU_ORIGIN=https://{hostname}',
                              'KSU_SECURE_COOKIES=1', 'KSU_REQUIRE_INITIALIZED=1', 'KSU_TRUST_LOCAL_PROXY=1', '']),
        'caddy.env': f'KSU_HOSTNAME={hostname}\n',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('hostname')
    parser.add_argument('output_directory')
    args = parser.parse_args()
    directory = Path(args.output_directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    for name, contents in render(args.hostname).items():
        target = directory/name
        # These files contain settings only; credentials are generated directly on the server.
        target.write_text(contents)
        target.chmod(0o640)
