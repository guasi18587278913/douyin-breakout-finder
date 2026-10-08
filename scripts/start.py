#!/usr/bin/env python3
"""Launch a detached local wizard and return its browser address to the host agent."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description='打开本地小号爆款向导。')
    p.add_argument('--no-open', action='store_true')
    p.add_argument('--data-dir')
    a = p.parse_args()
    folder = Path(tempfile.mkdtemp(prefix='douyin-wizard-'))
    ready = folder / 'ready.json'
    command = [sys.executable, str(Path(__file__).with_name('wizard.py')), '--ready-file', str(ready)]
    if a.no_open:
        command.append('--no-open')
    if a.data_dir:
        command += ['--data-dir', a.data_dir]
    options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    for _ in range(100):
        if ready.exists():
            print(ready.read_text(encoding='utf-8'))
            return 0
        if child.poll() is not None:
            break
        time.sleep(.1)
    print('向导未启动，请在当前终端运行 scripts/wizard.py 查看结果。', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
