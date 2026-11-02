# Explicit allowlist: no databases, photos, credentials, Git checkout or local var/.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent.parent
assets = [(str(root / name), '.') for name in ('index.html', 'app.js', 'sync.js', 'styles.css')]
assets += [(str(root / 'server/seed.json'), 'server'),
           (str(root / 'deploy/windows/assets/DejaVuSans.ttf'), 'fonts'),
           (str(root / 'deploy/windows/assets/FONT-LICENSE.txt'), 'fonts')]
a = Analysis([str(root / 'portable/launcher.py')], pathex=[str(root)], datas=assets,
             hiddenimports=collect_submodules('uvicorn') + ['PIL.JpegImagePlugin', 'PIL.PngImagePlugin'],
             excludes=['playwright', 'pytest', 'tests', 'httpx', 'pip'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='KSU-Restore',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='KSU-Restore')
