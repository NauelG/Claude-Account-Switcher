"""Text editing and human-readable automation views use no real user state."""
from datetime import datetime
import contextlib
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('account', str(ROOT / 'bin/ng-claude-account'))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
account = importlib.util.module_from_spec(spec)
loader.exec_module(account)


class EditorScreen:
    def __init__(self, keys):
        self.keys, self.frames, self.frame, self.cursor = list(keys), [], [], None

    def keypad(self, value): pass
    def timeout(self, value): self.wait = value
    def getmaxyx(self): return 12, 48
    def erase(self): self.frame = []
    def addnstr(self, y, x, value, limit, style=0): self.frame.append(value[:limit])
    def refresh(self): self.frames.append(list(self.frame))
    def move(self, y, x): self.cursor = (y, x)
    def get_wch(self): return self.keys.pop(0)


class InteractionTests(unittest.TestCase):
    def test_replace_selection_edit_middle_and_delete(self):
        buffer = account.TextBuffer('Personal')
        buffer.key('W')
        buffer.key('r')
        buffer.key('k')
        buffer.key(account.curses.KEY_LEFT)
        buffer.key(account.curses.KEY_LEFT)
        buffer.key('o')
        self.assertEqual(buffer.text, 'Work')
        buffer.key(account.curses.KEY_HOME)
        buffer.key(account.curses.KEY_DC)
        self.assertEqual(buffer.text, 'ork')
        buffer.key(account.curses.KEY_END)
        buffer.key('\x7f')
        self.assertEqual(buffer.text, 'or')
        buffer.key('\x15')
        self.assertEqual((buffer.text, buffer.cursor), ('', 0))

    def test_unicode_and_input_length(self):
        buffer = account.TextBuffer('', limit=4)
        for char in 'CaféX':
            buffer.key(char)
        self.assertEqual(buffer.text, 'Café')
        self.assertEqual(account.terminal_width('Café'), 4)
        self.assertEqual(account.terminal_width('界'), 2)
        self.assertEqual(account.terminal_width('e\u0301'), 1)

    def test_validation_is_inline_and_retains_text(self):
        screen = EditorScreen(['\n', '\x15', 'w', 'o', 'r', 'k', '\n'])
        validator = lambda value: 'Invalid name; fix it here.' if value == 'BAD' else None
        with patch.object(account.curses, 'wrapper', side_effect=lambda draw: draw(screen)), \
                patch.object(account.curses, 'set_escdelay'), patch.object(account.curses, 'curs_set'):
            self.assertEqual(account.edit_text('Name', 'BAD', validator=validator), 'work')
        self.assertTrue(any('Invalid name; fix it here.' in frame and 'BAD' in frame
                            for frame in screen.frames))
        self.assertEqual(screen.wait, -1)
        self.assertIsNotNone(screen.cursor)

    def test_automation_distinguishes_turn_from_timer_poll_and_summarizes_result(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(account, 'STORE', Path(tmp)), \
                patch.object(account, 'registered_profiles', return_value=['one', 'two', 'three']), \
                patch.object(account, 'display_name', side_effect=lambda name: name.title()):
            now = time.time()
            account.write_json(account.prime_state_path(), {
                'next': 'two', 'attempted_at': now - 60, 'result': 'skipped: already open'})
            start = datetime.fromtimestamp(now - 60).strftime('%a %Y-%m-%d %H:%M:%S UTC')

            def systemctl(*args):
                if args[1].endswith('.timer'):
                    return 'LoadState=loaded\nActiveState=active\nUnitFileState=enabled'
                return 'ActiveState=inactive\nResult=success\nExecMainStartTimestamp=' + start

            with patch.object(account, 'systemctl', side_effect=systemctl):
                details = '\n'.join(account.automation_details('windows'))
            self.assertIn('Enabled | running', details)
            self.assertIn('Last result    Success', details)
            self.assertIn('Next account   Two', details)
            self.assertIn('1 h 40 min | 3 account(s)', details)
            self.assertIn('Next turn', details)
            self.assertNotIn('Next timer check /', details)

    def test_authentication_state_is_separate_from_quota_state(self):
        row = {'name': 'work', 'active': False, 'state': 'Saved', 'token': 'expired'}
        cached = {'state': 'ok', 'cached_at': time.time() - 300, 'windows': {}}
        self.assertEqual(account.ui_account_state(row, cached), 'Saved')
        self.assertIn('Cached ', account.quota_status(cached))
        self.assertIn('old', account.quota_status(cached))
        self.assertEqual(account.ui_account_state(row, {'state': 'token rejected - log in again'}),
                         'Login required')
        self.assertEqual(account.ui_account_state(row, {'state': 'token renewal failed'}),
                         'Needs attention')
        self.assertEqual(account.quota_status(None, True), 'Loading...')
        self.assertEqual(account.quota_status({'state': 'network unavailable'}), 'network unavailable')
        self.assertIn('Cached ', account.quota_status({'state': 'ok', 'fetched_at': time.time() - 300}))
        self.assertEqual(account.quota_status({'state': 'ok', 'fetched_at': time.time()}), 'Live reading')

    def test_contextual_rename_targets_selected_account_without_switch_or_ack(self):
        with patch.object(account, 'profiles', return_value=['personal', 'work']), \
                patch.object(account, 'select', return_value=2) as choose, \
                patch.object(account, 'account_details', return_value=['Work']), \
                patch.object(account, 'edit_alias', return_value='Office'), \
                patch.object(account, 'execute') as execute:
            self.assertEqual(account.account_actions('work', None), ('Alias saved: Office.', False))
        execute.assert_called_once_with('alias', 'work', 'Office')
        choose.assert_called_once()  # No success acknowledgement screen.

    def test_contextual_failure_keeps_actions_open_for_login(self):
        usage = Mock()
        with patch.object(account, 'profiles', return_value=['work']), \
                patch.object(account, 'select', side_effect=[3, 4]) as choose, \
                patch.object(account, 'account_details', return_value=['Work']), \
                patch.object(account, 'renew_profile', side_effect=ValueError('Refresh rejected; log in again.')), \
                patch.object(account, 'execute') as execute, \
                patch.object(account, 'display_name', return_value='Work'):
            result = account.account_actions('work', usage)
        self.assertEqual(result, ('Login saved for Work.', False))
        execute.assert_called_once_with('login', 'work')
        self.assertIn('Error: Refresh rejected; log in again.', choose.call_args_list[1].args[2])
        self.assertTrue(choose.call_args_list[1].kwargs['error'])
        usage.start.assert_called_once_with(force=True)

    def test_main_menu_returns_to_dashboard_with_toast(self):
        with patch.object(account, 'startup_sync', return_value=[]), \
                patch.object(account.UsageMonitor, 'start'), \
                patch.object(account, 'account_actions', return_value=('Alias saved: Work.', False)), \
                patch.object(account, 'select', side_effect=[('account', 'work'), None]) as choose:
            account.menu()
        second = choose.call_args_list[1]
        self.assertEqual(second.args[2], ['Alias saved: Work.'])
        self.assertFalse(second.kwargs['notice_error'])
        self.assertGreater(second.kwargs['notice_until'], time.monotonic())

    def test_profile_renewal_refuses_unverified_live_credentials(self):
        with patch.object(account, 'tool_lock', return_value=contextlib.nullcontext()), \
                patch.object(account, 'sync', return_value={'status': 'unverified', 'active': 'personal'}), \
                patch.object(account, 'freshen') as refresh:
            with self.assertRaisesRegex(ValueError, 'Reconcile the live credentials'):
                account.renew_profile('work')
            refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
