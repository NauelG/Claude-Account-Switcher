import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

SCRIPT = Path(__file__).resolve().parents[1] / 'bin/ng-claude-account'


def run(keys, no_color=False, small=False, compact=False, resize_to=None, expected_alias=None):
    with tempfile.TemporaryDirectory() as home:
        master, slave = pty.openpty()
        size = (10, 30) if small else (18, 48) if compact else (34, 100)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', *size, 0, 0))
        original = termios.tcgetattr(slave)
        env = dict(os.environ, HOME=home, TERM='xterm-256color', XDG_DATA_HOME=home)
        for name in ['CLAUDE_CONFIG_DIR', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                     'CLAUDE_CODE_OAUTH_TOKEN', 'NO_COLOR']:
            env.pop(name, None)
        if no_color:
            env['NO_COLOR'] = '1'
        def attach_terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        process = subprocess.Popen([sys.executable, '-B', str(SCRIPT)], stdin=slave, stdout=slave, stderr=slave,
                                   env=env, preexec_fn=attach_terminal)
        output = bytearray()

        def collect(duration=0.2):
            until = time.monotonic() + duration
            while time.monotonic() < until:
                ready, _, _ = select.select([master], [], [], 0.03)
                if ready:
                    output.extend(os.read(master, 65536))

        def collect_until(marker, limit=8.0):
            # Wait for content rather than a fixed delay; startup time varies with load.
            start = len(output)
            until = time.monotonic() + limit
            while marker not in output[start:] and time.monotonic() < until:
                collect(0.05)
            collect(0.1)

        try:
            # The help line is drawn last, so it marks a complete first frame.
            collect_until(b'Esc' if small else b'q Quit')
            if resize_to:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', *resize_to, 0, 0))
                process.send_signal(signal.SIGWINCH)
                collect_until(b'CLAUDE ACCOUNT SWITCHER')
            for key in keys:
                if isinstance(key, (int, float)):
                    collect(key)
                    continue
                os.write(master, key)
                collect()
            process.wait(timeout=3)
            collect()
            assert termios.tcgetattr(slave) == original, 'Terminal attributes were not restored'
            store = Path(home) / 'claude-account'
            if expected_alias is None:
                assert not store.exists(), 'Navigation wrote account data'
            else:
                assert json.loads((store / 'principal.identity.json').read_text())['alias'] == expected_alias
                assert not (Path(home) / '.claude/.credentials.json').exists(), 'Alias edit touched credentials'
            return process.returncode, bytes(output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            os.close(slave)


# Enter opens the selected account directly. Switching a pending profile reports
# an inline error; Esc returns to the dashboard, where it remains visible.
code, output = run([b'\r', b'\r', b'\x1b', b'q'])
assert code == 0, output
assert b'Account actions' in output, output
assert b'No Claude credentials yet' in output, output
assert b'Error:' in output, output
assert b'5;173m' in output, 'Missing terracotta color sequences'
assert b'A C C O U N T   S W I T C H E R' in output
assert b'ACCOUNTS' in output and b'Manage accounts' in output
assert b'not registered' in output and b'r Refresh' in output
code, output = run([b'q'], no_color=True)
assert code == 0
assert b'5;173m' not in output
code, output = run([b'q'], compact=True)
assert code == 0 and b'CLAUDE ACCOUNT SWITCHER' in output
assert b'A C C O U N T' not in output
code, output = run([b'q'], resize_to=(18, 48))
assert code == 0 and b'A C C O U N T' in output and b'CLAUDE ACCOUNT SWITCHER' in output
code, output = run([b'\x1b'], small=True)
assert code == 0 and b'Enlarge terminal' in output
code, output = run([b'\x03'])
assert code == 130 and b'Cancelled' in output
code, output = run([b'\r', b'\x1bOB', b'\x1bOB', b'\r', 1.0, b' test', b'\x1b', b'\x1b', b'q'])
assert code == 0 and b'Alias for principal' in output, (code, output[-400:])
code, output = run([b'\r', b'\x1bOB', b'\x1bOB', b'\r', 1.0, b'\x7f' * 9, b'Personal',
                    1.0, b'\r', b'q'], expected_alias='Personal')
assert code == 0 and b'Alias saved:' in output, (code, output[-400:])
code, output = run([b'/', b'secundaria', b'\r', b'\x1b', b'q'])
assert code == 0 and b'Search accounts' in output, (code, output[-400:])
code, output = run([b'\r', b'\x1bOB', b'\x1bOB', b'\r', b'Wrok', b'\x1bOH', b'\x1bOC',
                    b'\x1b[3~', b'\x1bOC', b'r', b'\r', b'q'], expected_alias='Work')
assert code == 0, (code, output[-400:])
print('PASS: PTY navigation, terracotta, ASCII logo, active card, groups, compact mode, live resizing, NO_COLOR, errors, idle alias editing, save/cancel and terminal restoration')
