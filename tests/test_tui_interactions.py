"""Text editing and human-readable automation views use no real user state."""
from datetime import datetime
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
