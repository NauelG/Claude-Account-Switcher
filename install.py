#!/usr/bin/env python3
"""Install Claude Account Switcher for the current user; automation is opt-in."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
TIMERS = {'renewal': 'ng-claude-account-renew.timer',
          'windows': 'ng-claude-account-window.timer'}


def replace_file(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.cas-install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], capture_output=True,
                          text=True, timeout=20)


def unit_bytes(source, executable):
    lines = source.read_text().splitlines()
    if executable == Path.home() / '.local/bin/ng-claude-account':
        return ('\n'.join(lines) + '\n').encode()
    # Unit ExecStart paths with spaces or '%' require systemd escaping.
    value = str(executable).replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"')
    for index, line in enumerate(lines):
        if line.startswith('ExecStart=%h/.local/bin/ng-claude-account '):
            args = line.removeprefix('ExecStart=%h/.local/bin/ng-claude-account ')
            lines[index] = f'ExecStart="{value}" {args}'
    return ('\n'.join(lines) + '\n').encode()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local')
    parser.add_argument('--unit-dir', type=Path,
                        default=Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
                        / 'systemd/user')
    parser.add_argument('--no-systemd', action='store_true', help='do not install or manage systemd units')
    parser.add_argument('--enable-renewal', action='store_true', help='enable daily token renewal')
    parser.add_argument('--enable-windows', action='store_true', help='enable quota-consuming window prompts')
    args = parser.parse_args(argv)
    if sys.platform != 'linux':
        parser.error('This installer currently supports Linux only.')
    if args.no_systemd and (args.enable_renewal or args.enable_windows):
        parser.error('--no-systemd cannot be combined with --enable-renewal or --enable-windows.')
    prefix, unit_dir = args.prefix.expanduser().resolve(), args.unit_dir.expanduser().resolve()
    if any(c in str(prefix) + str(unit_dir) for c in ('\n', '\r', '\x00')):
        parser.error('Install paths cannot contain control characters.')
    executable = prefix / 'bin/ng-claude-account'
    replace_file(executable, (ROOT / 'bin/ng-claude-account').read_bytes(), 0o700)
    docs = prefix / 'share/doc/claude-account-switcher'
    for relative in ('README.md', 'LICENSE', 'CHANGELOG.md', 'docs/REFERENCE.md',
                     'docs/DOTFILES.md', 'docs/assets/tui-preview.svg'):
        replace_file(docs / relative, (ROOT / relative).read_bytes(), 0o644)
    print(f'Installed {executable}')
    if args.no_systemd:
        return 0
    for source in sorted((ROOT / 'systemd/user').glob('ng-claude-account-*')):
        replace_file(unit_dir / source.name, unit_bytes(source, executable), 0o644)
    enabled = [TIMERS[k] for k, selected in (('renewal', args.enable_renewal),
                                            ('windows', args.enable_windows)) if selected]
    try:
        manager = systemctl('show-environment')
    except (OSError, subprocess.TimeoutExpired):
        manager = None
    if manager is None or manager.returncode:
        print('User systemd manager unavailable; units installed but no timers enabled.', file=sys.stderr)
        return 1 if enabled else 0
    result = systemctl('daemon-reload')
    if result.returncode:
        print('Failed to reload the user systemd manager.', file=sys.stderr)
        return 1
    if enabled:
        result = systemctl('enable', '--now', *enabled)
        if result.returncode:
            print('Failed to enable requested timers; inspect systemctl --user status.', file=sys.stderr)
            return 1
        print('Enabled: ' + ', '.join(enabled))
    else:
        print('Automation activation is unchanged. Enable it explicitly with ng-claude-account schedule.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f'Installation failed: {error}', file=sys.stderr)
        sys.exit(1)
