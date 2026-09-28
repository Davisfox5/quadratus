"""Reproduce a TERM-responsive parent with a TERM-resistant child safely.

No vendor calls. A finally block kills only this probe's own process group.
The ten-second observation exceeds timeout plus the current TERM/KILL grace.
"""
import json
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from quadratus.cli_providers import _launch  # noqa: E402

root = Path(tempfile.mkdtemp(prefix='quadratus-cleanup-proof-'))
state = root / 'child.json'
child = (
    'import os,signal,time,json; from pathlib import Path; '
    'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
    f'Path({str(state)!r}).write_text(json.dumps('
    '{"pid":os.getpid(),"pgid":os.getpgrp()})); time.sleep(60)'
)
parent = (
    'import subprocess,sys,time; '
    f'subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(60)'
)
result = {}


def call():
    try:
        _launch([sys.executable, '-c', parent], timeout=1)
        result['outcome'] = 'returned'
    except BaseException as exc:
        result['outcome'] = type(exc).__name__


thread = threading.Thread(target=call, daemon=True)
started = time.monotonic()
thread.start()
try:
    thread.join(10)
    result['returned_within_10_seconds'] = not thread.is_alive()
    if state.exists():
        ids = json.loads(state.read_text())
        assert ids['pgid'] != os.getpgrp()
        try:
            os.kill(ids['pid'], 0)
            result['child_alive_after_timeout'] = True
        except ProcessLookupError:
            result['child_alive_after_timeout'] = False
finally:
    if state.exists():
        ids = json.loads(state.read_text())
        if ids['pgid'] != os.getpgrp():
            try:
                os.killpg(ids['pgid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
    thread.join(3)
result['finished_after_explicit_cleanup'] = not thread.is_alive()
result['seconds'] = time.monotonic() - started
result['note'] = ('Parent responds to TERM; descendant ignores TERM and inherits '
                  'output pipes. Probe kills its own group in finally.')
print(json.dumps(result, indent=2))
