"""Reproducible local Windows build. Requires an installed PyInstaller."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
RUNTIME = (
    'awb_config.py', 'awb_gui.py', 'awb_gui.pyw', 'awb_links.py', 'awb_sessions.py',
    'awb_session_panel.py', 'share_workspace.py', 'awb_memory_backup.py', 'sync-agent-memory.ps1', 'run_gui.bat',
    'config.example.json', 'README.md', 'CHANGELOG.md', 'configs/agent-memory-snippet.md',
    'experiments/native_session_sync/README.md', 'experiments/native_session_sync/workspace_sync.py',
    'experiments/native_session_sync/native_sync.py', 'experiments/native_session_sync/offline_sync.py',
    'experiments/native_session_sync/codex_rollout.py', 'experiments/native_session_sync/backup_lab.py',
    'experiments/native_session_sync/session_lifecycle.py', 'experiments/native_session_sync/probe_codex.py',
    'experiments/native_session_sync/storage_schema.py',
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'dist'))
    args = parser.parse_args()
    out = Path(args.out).resolve()
    build = ROOT / 'build' / 'release'
    if out.exists() and any(out.iterdir()):
        parser.error("--out must be an empty directory")
    out.mkdir(parents=True, exist_ok=True)
    build.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(build / 'cache'))
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
                    '--name', 'AWBConsole', '--distpath', str(out), '--workpath', str(build / 'work'),
                    '--specpath', str(build), str(ROOT / 'awb_gui.py')], env=env, cwd=ROOT, check=True)
    for name in RUNTIME:
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, out / name)
    # Existing machine configuration is deliberately not part of the build inputs.
    exe = out / 'AWBConsole.exe'
    print('BUILT', exe)
    print('SHA256', hashlib.sha256(exe.read_bytes()).hexdigest())
    return 0


if __name__ == '__main__':
    sys.exit(main())
