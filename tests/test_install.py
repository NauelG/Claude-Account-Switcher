"""Independent user installation must not mutate credentials or enable prompts implicitly."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'install.py')
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefix = self.root / 'local prefix %'
        self.unit_dir = self.root / 'units'
        self.args = ['--prefix', str(self.prefix), '--unit-dir', str(self.unit_dir)]

    def install(self, *flags):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return installer.main([*self.args, *flags])

    def test_manual_install_needs_no_systemd_and_keeps_tokens_untouched(self):
        store = self.prefix / 'share/claude-account'
        store.mkdir(parents=True)
        credentials = store / 'principal.json'
        credentials.write_text('private fixture: must remain unchanged')
        with patch.object(installer, 'systemctl', side_effect=AssertionError('No systemd allowed')):
            self.assertEqual(self.install('--no-systemd'), 0)
        executable = self.prefix / 'bin/ng-claude-account'
        self.assertEqual(executable.read_bytes(), (ROOT / 'bin/ng-claude-account').read_bytes())
        self.assertEqual(executable.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.unit_dir.exists())
        self.assertEqual(credentials.read_text(), 'private fixture: must remain unchanged')
        self.assertTrue((self.prefix / 'share/doc/claude-account-switcher/README.md').is_file())
        self.assertTrue((self.prefix / 'share/doc/claude-account-switcher/LICENSE').is_file())
        self.assertTrue((self.prefix / 'share/doc/claude-account-switcher/docs/assets/tui-preview.svg').is_file())

    def test_default_install_does_not_enable_or_disable_any_timer(self):
        with patch.object(installer, 'systemctl', return_value=subprocess.CompletedProcess([], 0)) as control:
            self.assertEqual(self.install(), 0)
        self.assertEqual(control.call_args_list, [call('show-environment'), call('daemon-reload')])
        self.assertEqual(len(list(self.unit_dir.glob('ng-claude-account-*'))), 4)
        unit = (self.unit_dir / 'ng-claude-account-window.service').read_text()
        self.assertIn(f'ExecStart="{self.prefix / "bin/ng-claude-account"}'.replace('%', '%%'), unit)
        self.assertIn(' prime-next', unit)

    def test_explicit_opt_in_enables_only_requested_timers(self):
        with patch.object(installer, 'systemctl', return_value=subprocess.CompletedProcess([], 0)) as control:
            self.assertEqual(self.install('--enable-renewal'), 0)
        self.assertEqual(control.call_args_list[-1],
                         call('enable', '--now', 'ng-claude-account-renew.timer'))
        with patch.object(installer, 'systemctl', return_value=subprocess.CompletedProcess([], 0)) as control:
            self.assertEqual(self.install('--enable-renewal', '--enable-windows'), 0)
        self.assertEqual(control.call_args_list[-1], call('enable', '--now',
                         'ng-claude-account-renew.timer', 'ng-claude-account-window.timer'))

    def test_unavailable_systemd_allows_install_but_cannot_claim_enabled(self):
        with patch.object(installer, 'systemctl', side_effect=FileNotFoundError):
            self.assertEqual(self.install(), 0)
            self.assertEqual(self.install('--enable-windows'), 1)

    def test_conflicting_flags_write_nothing(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.install('--no-systemd', '--enable-windows')
        self.assertFalse(self.prefix.exists())


if __name__ == '__main__':
    unittest.main()
