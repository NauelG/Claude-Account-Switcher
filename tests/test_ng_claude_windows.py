"""Scheduled prompts must never switch accounts, leak tokens, or send duplicates."""
import configparser
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.machinery
import importlib.util
import json
import io
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import call as expected_call, patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    'account', str(ROOT / 'bin/ng-claude-account'))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
account = importlib.util.module_from_spec(spec)
loader.exec_module(account)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.patch = patch.multiple(account, STORE=root / 'accounts',
                                    LIVE=root / '.claude/.credentials.json')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        account.STORE.mkdir()
        account.LIVE.parent.mkdir()
        self.data = {'claudeAiOauth': {'accessToken': 'fixture-secret',
                                     'refreshToken': 'fixture-refresh',
                                     'expiresAt': (time.time() + 3600) * 1000},
                     'mcpOAuth': {'fixture': 'preserve'}}
        for name in account.profiles():
            account.write_json(account.credential_path(name), self.data)
        account.write_json(account.LIVE, self.data)
        account.set_active('principal')
        self.state = {'active': 'principal', 'status': 'ok', 'notes': []}
        self.network = patch.object(account.urllib.request, 'urlopen',
                                    side_effect=AssertionError('Unexpected network call'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def windows(self, hours):
        return {'five_hour': {'used': 0.0, 'resets': datetime.now(timezone.utc) + timedelta(hours=hours)},
                'seven_day': {'used': 10.0, 'resets': datetime.now(timezone.utc) + timedelta(days=2)}}

    def test_open_window_skips_prompt(self):
        with patch.object(account, 'fetch_usage', return_value=self.windows(2)), \
                patch.object(account, 'run_minimal_prompt') as prompt:
            self.assertIn('already open', account.prime_profile('secundaria', self.state))
            prompt.assert_not_called()

    def test_expired_window_opens_and_confirms_without_switching(self):
        before = account.LIVE.read_bytes()
        with patch.object(account, 'fetch_usage', side_effect=[self.windows(-1), self.windows(5)]), \
                patch.object(account, 'run_minimal_prompt') as prompt:
            self.assertIn('window confirmed', account.prime_profile('secundaria', self.state))
            prompt.assert_called_once_with('fixture-secret')
        self.assertEqual(account.active(), 'principal')
        self.assertEqual(account.LIVE.read_bytes(), before)

    def test_explicit_idle_window(self):
        idle = {'five_hour': {'used': 0, 'resets': None, 'idle': True}}
        with patch.object(account, 'fetch_usage', side_effect=[idle, self.windows(5)]), \
                patch.object(account, 'run_minimal_prompt') as prompt:
            self.assertIn('confirmed', account.prime_profile('secundaria', self.state))
            prompt.assert_called_once()

    def test_missing_reset_and_weekly_limit_skip(self):
        unknown = {'five_hour': {'used': 0, 'resets': None}}
        weekly = self.windows(-1)
        weekly['seven_day']['used'] = 100
        with patch.object(account, 'run_minimal_prompt') as prompt:
            for value in [unknown, weekly]:
                with patch.object(account, 'fetch_usage', return_value=value):
                    self.assertIn('skipped', account.prime_profile('secundaria', self.state))
            prompt.assert_not_called()

    def test_rate_limit_persists_cooldown(self):
        with patch.object(account, 'fetch_usage', side_effect=account.UsageUnavailable('rate limited', 600)), \
                patch.object(account, 'run_minimal_prompt') as prompt:
            self.assertIn('skipped', account.prime_profile('secundaria', self.state))
            self.assertGreater(account.read_usage_cache('secundaria')['retry_at'], time.time())
            prompt.assert_not_called()
        with patch.object(account, 'fetch_usage') as fetch:
            self.assertIn('rate limited', account.prime_profile('secundaria', self.state))
            fetch.assert_not_called()

    def test_cursor_survives_failure_and_restart_no_duplicate(self):
        with patch.object(account, 'sync', return_value=self.state), \
                patch.object(account, 'prime_profile', side_effect=ValueError('failed')) as prime:
            self.assertFalse(account.prime_next())
            prime.assert_called_once_with('principal', self.state)
        self.assertEqual(json.loads(account.prime_state_path().read_text())['next'], 'secundaria')
        with patch.object(account, 'prime_profile') as prime:
            self.assertTrue(account.prime_next())
            prime.assert_not_called()
        schedule = json.loads(account.prime_state_path().read_text())
        schedule['attempted_at'] -= account.prime_interval() + 1
        account.write_json(account.prime_state_path(), schedule)
        with patch.object(account, 'sync', return_value=self.state), \
                patch.object(account, 'prime_profile', return_value='skipped: already open') as prime:
            self.assertTrue(account.prime_next())
            prime.assert_called_once_with('secundaria', self.state)
        self.assertEqual(json.loads(account.prime_state_path().read_text())['next'], 'principal')
        self.assertEqual(account.prime_state_path().stat().st_mode & 0o777, 0o600)

    def test_cli_isolation_token_not_in_argv_or_output(self):
        def invoke(argv, **kwargs):
            self.assertNotIn('fixture-secret', str(argv))
            self.assertEqual(kwargs['env']['CLAUDE_CODE_OAUTH_TOKEN'], 'fixture-secret')
            self.assertNotEqual(kwargs['env']['CLAUDE_CONFIG_DIR'], str(account.LIVE.parent))
            self.assertEqual(kwargs['cwd'], kwargs['env']['CLAUDE_CONFIG_DIR'])
            self.assertIn('--safe-mode', argv)
            self.assertIn('--no-session-persistence', argv)
            self.assertEqual(argv[argv.index('--tools') + 1], '')
            return subprocess.CompletedProcess(argv, 0, '{"subtype":"success","is_error":false}', '')
        with patch.object(account.shutil, 'which', return_value='/fake/claude'), \
                patch.object(account.subprocess, 'run', side_effect=invoke):
            account.run_minimal_prompt('fixture-secret')
        self.assertFalse(list(account.STORE.glob('.window-*')))

    def test_activation_timestamp_skew_does_not_skip_a_turn(self):
        account.write_json(account.prime_state_path(), {
            'next': 'secundaria', 'attempted_at': time.time() - account.prime_interval() + 1})
        with patch.object(account, 'sync', return_value=self.state), \
                patch.object(account, 'prime_profile', return_value='skipped: already open') as prime:
            account.prime_next()
            prime.assert_called_once_with('secundaria', self.state)

    def test_cli_error_does_not_leak_and_never_retries(self):
        bad = subprocess.CompletedProcess([], 1, 'fixture-secret', 'fixture-refresh')
        with patch.object(account.shutil, 'which', return_value='/fake/claude'), \
                patch.object(account.subprocess, 'run', return_value=bad) as run:
            with self.assertRaisesRegex(ValueError, r'CLI exit 1') as error:
                account.run_minimal_prompt('fixture-secret')
            self.assertNotIn('fixture', str(error.exception))
            run.assert_called_once()

    def test_systemd_interval_crosses_midnight_without_daily_reset(self):
        units = ROOT / 'systemd/user'
        config = configparser.ConfigParser(interpolation=None)
        config.read(units / 'ng-claude-account-window.timer')
        self.assertEqual(config['Timer']['OnUnitActiveSec'], '60s')
        self.assertNotIn('OnCalendar', config['Timer'])
        config.read(units / 'ng-claude-account-window.service')
        self.assertEqual(config['Service']['ExecStart'], '%h/.local/bin/ng-claude-account prime-next')

    def test_timer_controls_both_units_and_reports_actual_state(self):
        def control(*args):
            if args[0] == 'show':
                return 'LoadState=loaded\nActiveState=active\nUnitFileState=enabled'
            if args[0] == 'list-timers':
                return 'Thu 2026-11-12 12:14:55 CET 1h left ...'
            return ''
        with patch.object(account, 'systemctl', side_effect=control) as call, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            account.schedule_control('enable', 'all')
            account.schedule_control('disable', 'windows')
        self.assertIn('Timer: active | Startup: enabled', output.getvalue())
        self.assertIn('12:14:55', output.getvalue())
        self.assertIn('already running turn may still finish', output.getvalue())
        for unit in account.TIMERS.values():
            self.assertIn(expected_call('enable', '--now', unit), call.call_args_list)
        self.assertIn(expected_call('disable', '--now', account.TIMERS['windows']), call.call_args_list)

    def test_schedule_cli_does_not_require_credentials_or_clean_auth_env(self):
        with patch.object(account, 'schedule_control') as control, \
                patch.object(account, 'check_environment', side_effect=AssertionError('Auth not needed')), \
                patch('sys.argv', ['ng-claude-account', 'schedule', 'status', '--timer', 'renewal']):
            account.main()
            control.assert_called_once_with('status', 'renewal')

    def test_automation_menu_routes_enable_disable_and_refresh(self):
        choices = [0, 1, 2, 0, 3, 1, 3, 2]
        with patch.object(account, 'select', side_effect=choices), \
                patch.object(account, 'timer_status', return_value=['Timer: active']) as status, \
                patch.object(account, 'schedule_control') as control:
            account.automation_menu()
        self.assertEqual(control.call_args_list, [expected_call('enable', 'windows'),
                                                  expected_call('disable', 'windows'),
                                                  expected_call('status', 'windows')])
        self.assertIn(expected_call('renewal'), status.call_args_list)

    def test_systemd_failure_is_actionable_and_not_silently_accepted(self):
        with patch.object(account.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 1, '', 'Failed to connect to bus')):
            with self.assertRaisesRegex(ValueError, 'user systemd manager'):
                account.systemctl('enable', '--now', account.TIMERS['windows'])
        with patch.object(account.subprocess, 'run', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ValueError, 'not installed'):
                account.systemctl('show')


if __name__ == '__main__':
    unittest.main()
