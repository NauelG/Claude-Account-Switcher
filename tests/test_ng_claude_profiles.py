"""Dynamic registry and scheduling tests, entirely isolated from real credentials."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    'account', str(ROOT / 'bin/ng-claude-account'))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
account = importlib.util.module_from_spec(spec)
loader.exec_module(account)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for change in [patch.multiple(account, STORE=root / 'accounts',
                                     LIVE=root / '.claude/.credentials.json',
                                     PLUGIN_LOCK_DIR=root / 'locks'),
                       patch.object(account.urllib.request, 'urlopen',
                                    side_effect=AssertionError('Unexpected real network'))]:
            change.start()
            self.addCleanup(change.stop)
        account.STORE.mkdir()
        account.LIVE.parent.mkdir()
        for name in account.profiles():
            self.save(name)
        account.write_json(account.LIVE, account.load(account.credential_path('principal')))
        account.set_active('principal')

    def save(self, name):
        data = {'claudeAiOauth': {'accessToken': 'fixture-' + name,
                                 'refreshToken': 'refresh-' + name,
                                 'expiresAt': (time.time() + 3600) * 1000},
                'mcpOAuth': {'shared': 'preserve'}}
        account.save_profile(name, data)
        account.write_json(account.identity_path(name), {'accountUuid': 'uuid-' + name,
                                                       'email': name + '@example.test'})

    def fake_login(self, name):
        def login(argv, env, check):
            target = Path(env['CLAUDE_CONFIG_DIR'])
            data = account.load(account.LIVE)
            data['claudeAiOauth']['accessToken'] = 'fixture-' + name
            data['claudeAiOauth']['refreshToken'] = 'refresh-' + name
            account.write_json(target / '.credentials.json', data)
            account.write_json(target / '.claude.json', {'oauthAccount': {
                'accountUuid': 'uuid-' + name, 'emailAddress': name + '@example.test'}})
        return login

    def add(self, name):
        with patch.object(account.subprocess, 'run', side_effect=self.fake_login(name)), \
                contextlib.redirect_stdout(io.StringIO()):
            account.manage_accounts('add', name)

    def test_migration_keeps_credentials_and_order(self):
        before = account.LIVE.read_bytes()
        saved = {n: account.credential_path(n).read_bytes() for n in account.profiles()}
        with account.tool_lock():
            account.sync()
        self.assertEqual(json.loads((account.STORE / 'profiles.json').read_text()),
                         ['principal', 'secundaria'])
        self.assertEqual(account.LIVE.read_bytes(), before)
        for name, payload in saved.items():
            self.assertEqual(account.credential_path(name).read_bytes(), payload)

    def test_add_n_accounts_and_round_robin_switch(self):
        before = account.LIVE.read_bytes()
        self.add('work')
        self.assertEqual(account.profiles(), ['principal', 'secundaria', 'work'])
        self.assertEqual(account.prime_interval(), 6000)
        self.assertEqual(account.LIVE.read_bytes(), before)
        self.assertEqual(account.identity('work')['email'], 'work@example.test')
        self.add('other')
        self.assertEqual(account.prime_interval(), 4500)
        for expected in ['secundaria', 'work', 'other', 'principal']:
            with contextlib.redirect_stdout(io.StringIO()):
                account.execute('switch')
            self.assertEqual(account.active(), expected)
            self.assertEqual(account.load(account.LIVE)['claudeAiOauth']['accessToken'],
                             'fixture-' + expected)
            self.assertEqual(account.load(account.LIVE)['mcpOAuth'], {'shared': 'preserve'})

    def test_remove_archives_inactive_and_rejects_active(self):
        self.add('work')
        original = account.credential_path('work').read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            account.manage_accounts('remove', 'work')
        self.assertNotIn('work', account.profiles())
        self.assertEqual(account.prime_interval(), 9000)
        self.assertFalse(account.credential_path('work').exists())
        archive = next(account.STORE.glob('removed-work-*'))
        self.assertEqual((archive / 'work.json').read_bytes(), original)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(ValueError, 'active account'):
            account.manage_accounts('remove', 'principal')

    def test_pending_profile_does_not_change_interval_and_login_can_retry(self):
        with patch.object(account.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'claude')):
            with self.assertRaises(subprocess.CalledProcessError):
                account.manage_accounts('add', 'work')
        self.assertIn('work', account.profiles())
        self.assertNotIn('work', account.registered_profiles())
        self.assertEqual(account.prime_interval(), 9000)
        self.add('work')
        self.assertEqual(account.prime_interval(), 6000)

    def test_rejects_invalid_names_collisions_and_corrupt_registry(self):
        for name in ['../escape', 'profiles', 'window-schedule', 'A', 'bad.name', 'a' * 41]:
            with self.assertRaises(ValueError):
                account.manage_accounts('add', name)
        before = account.credential_path('principal').read_bytes()
        with self.assertRaisesRegex(ValueError, 'already has credentials'):
            account.manage_accounts('add', 'principal')
        self.assertEqual(account.credential_path('principal').read_bytes(), before)
        account.write_json(account.STORE / 'profiles.json', ['principal', 'principal'])
        with self.assertRaisesRegex(ValueError, 'Invalid profile registry'):
            account.profiles()

    def test_scheduler_recomputes_interval_and_round_robins_all_accounts(self):
        self.add('work')
        with patch.object(account, 'prime_profile', return_value='skipped: already open') as prime:
            for expected in ['principal', 'secundaria', 'work', 'principal']:
                if account.prime_state_path().exists():
                    schedule = json.loads(account.prime_state_path().read_text())
                    schedule['attempted_at'] = time.time() - account.prime_interval() - 1
                    account.write_json(account.prime_state_path(), schedule)
                with account.tool_lock(), contextlib.redirect_stdout(io.StringIO()):
                    account.prime_next()
                self.assertEqual(prime.call_args.args[0], expected)
        # Removing the account that was due is handled on the next eligible turn.
        schedule = {'next': 'work', 'attempted_at': time.time() - 20000}
        account.write_json(account.prime_state_path(), schedule)
        with contextlib.redirect_stdout(io.StringIO()):
            account.manage_accounts('remove', 'work')
        with patch.object(account, 'prime_profile', return_value='skipped') as prime:
            with account.tool_lock(), contextlib.redirect_stdout(io.StringIO()):
                account.prime_next()
            self.assertEqual(prime.call_args.args[0], 'principal')

    def test_single_account_uses_five_hour_interval(self):
        with contextlib.redirect_stdout(io.StringIO()):
            account.manage_accounts('remove', 'secundaria')
        self.assertEqual(account.registered_profiles(), ['principal'])
        self.assertEqual(account.prime_interval(), 18000)
        with contextlib.redirect_stdout(io.StringIO()):
            account.execute('switch')
        self.assertEqual(account.active(), 'principal')

    def test_cli_dynamic_choices_and_account_list(self):
        self.add('work')
        with patch('sys.argv', ['ng-claude-account', 'accounts', 'list']), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            account.main()
        self.assertIn('3 authenticated account(s)', output.getvalue())
        with patch('sys.argv', ['ng-claude-account', 'switch', 'work']), \
                contextlib.redirect_stdout(io.StringIO()):
            account.main()
        self.assertEqual(account.active(), 'work')


if __name__ == '__main__':
    unittest.main()
