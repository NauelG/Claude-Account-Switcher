import copy
import importlib.machinery
import importlib.util
import json
import contextlib
import io
from pathlib import Path
import tempfile
import time
from typing import Any
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'bin/ng-claude-account'
loader = importlib.machinery.SourceFileLoader('account', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
account: Any = importlib.util.module_from_spec(spec)
loader.exec_module(account)

# Never contact the real usage endpoint from tests. Background monitors are
# disabled here; usage lookups are exercised synchronously further below.
account.UsageMonitor.start = lambda self, force=False: None
REAL_FETCH_USAGE = account.fetch_usage


def network_forbidden(*args, **kwargs):
    raise AssertionError('Unexpected network access')


account.fetch_usage = network_forbidden

# Credential reconciliation, switching and login are covered by
# test_ng_claude_credentials.py; this suite covers the dashboard and usage.

# Render the dashboard on a fake screen and check the signature placement.
class FakeScreen:
    def __init__(self, height, width):
        self.size = (height, width)
        self.cells = {}
        self.styles = {}
        self.keys = []

    def getmaxyx(self):
        return self.size

    def addnstr(self, y, x, text, limit, style=0):
        for offset, char in enumerate(text[:limit]):
            self.cells[(y, x + offset)] = char
            self.styles[(y, x + offset)] = style

    def row(self, y):
        width = self.size[1]
        return ''.join(self.cells.get((y, x), ' ') for x in range(width)).rstrip()

    def keypad(self, flag): pass
    def timeout(self, delay): pass
    def erase(self):
        self.cells, self.styles = {}, {}
    def refresh(self): pass

    def getch(self):
        return self.keys.pop(0) if self.keys else 27


OVERVIEW = ([{'name': 'principal', 'alias': 'Personal', 'email': 'a@example.test', 'active': True},
             {'name': 'secundaria', 'alias': 'Work', 'email': 'b@example.test', 'active': False}],
            None)


def render(height, width, usage=None, keys=None, details=(), overview=OVERVIEW):
    screen = FakeScreen(height, width)
    screen.keys = list(keys or [])
    with patch.object(account.curses, 'wrapper', side_effect=lambda draw: draw(screen)), \
         patch.object(account.curses, 'set_escdelay', create=True), \
         patch.object(account.curses, 'curs_set'), \
         patch.object(account.curses, 'has_colors', return_value=False), \
         patch.object(account, 'account_overview', return_value=overview):
        account.select('Your accounts', [label for label, _ in account.MENU], details,
                       dashboard=True, usage=usage, groups=account.MENU_GROUPS)
    return screen

full = render(33, 100)
subtitle_row = next(y for y in range(33) if 'A C C O U N T' in full.row(y))
subtitle = full.row(subtitle_row)
signature = full.row(subtitle_row + 1)
assert signature.strip() == 'ng', signature
assert len(signature) == len(subtitle), 'Signature must be right-aligned under the subtitle'
compact = render(18, 48)
assert compact.row(1).strip() == 'CLAUDE ACCOUNT SWITCHER  ng', compact.row(1)
# Every menu entry fits at the minimum full-layout size, with its group headings.
menu_text = '\n'.join(full.row(y) for y in range(33))
assert all(label in menu_text for label, _ in account.MENU) and '-- Manage --' in menu_text
assert 'Renew expired tokens' in menu_text
help_row = next(y for y in range(33) if 'Esc Back' in full.row(y))
exit_row = next(y for y in range(33) if full.row(y).strip() == 'Exit')
assert exit_row < help_row
# Sync notices appear above the help line when there is room, and never overlap items.
notice = 'Saved the token renewed outside this tool for Work.'
roomy = render(34, 100, details=[notice])
rows = [roomy.row(y) for y in range(34)]
assert notice in rows[34 - 3] and 'Esc Back' in rows[34 - 2]
assert not any(notice in row for row in [render(32, 100, details=[notice]).row(y) for y in range(28)])
# Unresolved credential problems are named in the card header.
troubled = render(34, 100, overview=(OVERVIEW[0], 'unregistered account in ~/.claude'))
assert 'unregistered account in ~/.claude' in troubled.row(9)

# N accounts fit a tall terminal, or page through a compact panel with [ and ].
many = [{'name': f'account{i}', 'alias': f'Account{i}', 'email': f'a{i}@example.test',
         'active': i == 0} for i in range(12)]
page = render(18, 60, overview=(many, None), keys=[ord(']'), 27])
page_text = '\n'.join(page.row(y) for y in range(18))
assert 'Accounts 3-4/12' in page_text and 'Account2' in page_text and 'Account3' in page_text
assert 'Account0' not in page_text
assert all(label in page_text for label, _ in account.MENU), 'Account pages must not hide menu options'
tall = render(40, 100, overview=(many[:3], None))
tall_text = '\n'.join(tall.row(y) for y in range(40))
assert 'A C C O U N T' in tall_text and all(f'Account{i}' in tall_text for i in range(3))

# Profile pickers scroll their options rather than requiring an N-row terminal.
picker = FakeScreen(12, 60)
picker.keys = [account.curses.KEY_END, 10]
with patch.object(account.curses, 'wrapper', side_effect=lambda draw: draw(picker)), \
     patch.object(account.curses, 'set_escdelay', create=True), \
     patch.object(account.curses, 'curs_set'), \
     patch.object(account.curses, 'has_colors', return_value=False):
    chosen = account.select('Choose account', [f'Profile {i}' for i in range(30)])
assert chosen == 29 and 'Profile 29' in '\n'.join(picker.row(y) for y in range(12))

print('PASS: signature placement (full and compact), menu layout, groups and notices,')

# ---------------------------------------------------------------- usage
from datetime import datetime, timedelta, timezone
from email.message import Message
import urllib.error

FUTURE = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers['Retry-After'] = retry_after
    return urllib.error.HTTPError('https://example.test', code, 'error', headers,
                                  io.BytesIO(b'{"error": "tok-secret"}'))


# fetch_usage: request shape, parsing, clamping and error mapping.
seen = []


def fake_urlopen(request, timeout):
    seen.append(request)
    return io.BytesIO(json.dumps({
        'five_hour': {'utilization': 46.0, 'resets_at': FUTURE},
        'seven_day': {'utilization': 140, 'resets_at': 'not-a-date'},
        'seven_day_opus': None,
    }).encode())


with patch.object(account.urllib.request, 'urlopen', side_effect=fake_urlopen):
    windows = REAL_FETCH_USAGE('tok-secret')
assert seen[0].get_header('Authorization') == 'Bearer tok-secret'
assert seen[0].get_header('Anthropic-beta') == 'oauth-2025-04-20'
assert seen[0].get_header('User-agent').startswith('ng-claude-account/')
assert windows['five_hour']['used'] == 46.0 and windows['five_hour']['resets'] is not None
assert windows['seven_day']['used'] == 100.0 and windows['seven_day']['resets'] is None

for error, expected, retry in ((http_error(429, '120'), 'rate limited', 120.0),
                               (http_error(429, 'soon'), 'rate limited', 300.0),
                               (http_error(401), 'token rejected', None),
                               (OSError('down'), 'network', None)):
    with patch.object(account.urllib.request, 'urlopen', side_effect=error):
        try:
            REAL_FETCH_USAGE('tok-secret')
            raise AssertionError('Expected UsageUnavailable')
        except account.UsageUnavailable as failure:
            assert expected in str(failure) and 'tok-secret' not in str(failure)
            assert failure.retry_after == retry
with patch.object(account.urllib.request, 'urlopen',
                  return_value=io.BytesIO(b'{"five_hour": null}')):
    try:
        REAL_FETCH_USAGE('tok')
        raise AssertionError('Expected UsageUnavailable')
    except account.UsageUnavailable as failure:
        assert 'no quota windows' in str(failure)

assert account.format_reset(datetime.fromisoformat(PAST)) == 'window reset since'
assert account.format_reset(datetime.fromisoformat(FUTURE)).startswith('resets ')
assert account.usage_level(59) == 'low' and account.usage_level(60) == 'medium'
assert account.usage_level(85) == 'high'
assert len(account.usage_bar(46, 20)) == 20

# profile_usage: caching, cooldowns and refresh ownership rules.
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    account.STORE = root / 'accounts'
    account.LIVE = root / '.claude/.credentials.json'
    account.STORE.mkdir()
    account.LIVE.parent.mkdir()
    now_ms = time.time() * 1000

    def save(name, token, expires):
        account.write_json(account.credential_path(name), {'claudeAiOauth': {
            'accessToken': token, 'refreshToken': 'refresh-' + token, 'expiresAt': expires}})

    save('principal', 'tok-a', now_ms + 3600000)
    save('secundaria', 'tok-b', now_ms + 3600000)
    account.set_active('principal')
    account.write_json(account.LIVE, account.load(account.credential_path('principal')))
    account.LAST_SYNC = {'status': 'ok', 'notes': []}
    calls = []
    behaviour = {}

    def fake_fetch(token):
        calls.append(token)
        outcome = behaviour.get(token)
        if isinstance(outcome, Exception):
            raise outcome
        return {'five_hour': {'used': 46.0, 'resets': datetime.fromisoformat(FUTURE)},
                'seven_day': {'used': 61.0, 'resets': None}}

    account.fetch_usage = fake_fetch
    result = account.profile_usage('principal')
    assert result['state'] == 'ok' and result['windows']['five_hour']['used'] == 46.0
    assert account.usage_cache_path('principal').stat().st_mode & 0o777 == 0o600
    assert 'tok-a' not in account.usage_cache_path('principal').read_text()
    # Fresh cache is reused without a request; force bypasses freshness.
    assert account.profile_usage('principal')['state'] == 'ok' and calls == ['tok-a']
    account.profile_usage('principal', force=True)
    assert calls == ['tok-a', 'tok-a']
    # 429 without cache: retry time shown, cooldown persisted and honoured even when forced.
    behaviour['tok-b'] = account.UsageUnavailable('rate limited', 600)
    result = account.profile_usage('secundaria')
    assert result['state'].startswith('rate limited - retry after'), result
    count = len(calls)
    assert account.profile_usage('secundaria', force=True)['state'].startswith('rate limited')
    assert len(calls) == count, 'Cooldown must prevent further requests'
    # 429 with cache: last known usage shown with a note.
    behaviour['tok-a'] = account.UsageUnavailable('rate limited', 600)
    result = account.profile_usage('principal', force=True)
    assert result['state'] == 'ok' and result['windows']['seven_day']['used'] == 61.0
    note = account.usage_note(result)
    assert note.startswith('cached ') and ', retry ' in note
    # Other failures fall back to cache without a cooldown.
    account.usage_cache_path('principal').unlink()
    behaviour['tok-a'] = account.UsageUnavailable('usage unavailable (network or response error)')
    assert 'network' in account.profile_usage('principal', force=True)['state']
    assert account.read_usage_cache('principal')['retry_at'] == 0.0
    # Corrupt cache is ignored rather than fatal.
    account.usage_cache_path('principal').write_text('{broken')
    behaviour.pop('tok-a')
    assert account.profile_usage('principal')['state'] == 'ok'
    # The active account is looked up with the live token in ~/.claude, which
    # others may have renewed since it was saved.
    for path in account.STORE.glob('*.usage.json'):
        path.unlink()
    live = account.load(account.LIVE)
    live['claudeAiOauth']['accessToken'] = 'tok-live'
    account.write_json(account.LIVE, live)
    assert account.profile_usage('principal')['state'] == 'ok' and calls[-1] == 'tok-live'
    # ...but only once its owner has been verified.
    account.LAST_SYNC = {'status': 'unknown', 'notes': []}
    count = len(calls)
    assert 'need attention' in account.profile_usage('principal', force=True)['state']
    assert len(calls) == count
    account.LAST_SYNC = {'status': 'ok', 'notes': []}
    # An expired active token is renewed through ~/.claude (plugin lock) and saved.
    for path in account.STORE.glob('*.usage.json'):
        path.unlink()
    save('principal', 'tok-a', now_ms - 1000)
    account.write_json(account.LIVE, account.load(account.credential_path('principal')))
    renewed = {'claudeAiOauth': {'accessToken': 'tok-renewed', 'refreshToken': 'r2',
                                 'expiresAt': now_ms + 3600000}}
    with patch.object(account, 'refresh_live', return_value=(renewed, True)) as refresh_live, \
            patch.object(account, 'freshen', side_effect=AssertionError('active uses refresh_live')):
        assert account.profile_usage('principal')['state'] == 'ok'
    assert refresh_live.called and calls[-1] == 'tok-renewed'
    assert account.load(account.credential_path('principal'))['claudeAiOauth']['accessToken'] == 'tok-renewed'
    # Inactive expired token: refreshed under the lock, then queried with the new token.
    save('secundaria', 'tok-b', now_ms - 1000)
    refreshed = {'claudeAiOauth': {'accessToken': 'tok-new', 'refreshToken': 'r', 'expiresAt': now_ms + 3600000}}
    with patch.object(account, 'freshen', return_value=refreshed) as freshen:
        assert account.profile_usage('secundaria')['state'] == 'ok'
    assert freshen.called and calls[-1] == 'tok-new'
    # A held lock reports busy instead of blocking.
    for path in account.STORE.glob('*.usage.json'):
        path.unlink()
    with (account.STORE / '.lock').open('a') as held:
        account.fcntl.flock(held, account.fcntl.LOCK_EX)
        with patch.object(account, 'freshen', side_effect=AssertionError('lock ignored')):
            assert 'busy' in account.profile_usage('secundaria')['state']
    # Failed refresh is reported, not raised.
    with patch.object(account, 'freshen', side_effect=ValueError('HTTP 400')):
        assert 'renewal failed' in account.profile_usage('secundaria')['state']
    # No active profile recorded: refresh nothing.
    account.state_path().unlink()
    account.LIVE.unlink()
    account.LIVE.write_text('{}')
    with patch.object(account, 'freshen', side_effect=AssertionError('must not refresh')):
        assert 'renewal pending' in account.profile_usage('secundaria')['state']
    account.credential_path('secundaria').unlink()
    assert account.profile_usage('secundaria') == {'state': 'not registered'}

    # Monitor never stays stuck loading, even if the lookup itself crashes.
    monitor = account.UsageMonitor()
    with patch.object(account, 'collect_usage', side_effect=RuntimeError('boom')):
        monitor._run()
    results, loading = monitor.snapshot()
    assert not loading and results['principal']['state'] == 'usage unavailable'
    account.fetch_usage = network_forbidden


# Rendering: bars, percentages, notes, loading state and the r key.
class StubUsage:
    def __init__(self, results, loading=False):
        self.results, self.loading, self.started = results, loading, []

    def snapshot(self):
        return self.results, self.loading

    def start(self, force=False):
        self.started.append(force)


sample = {'principal': {'state': 'ok', 'windows': {
              'five_hour': {'used': 46.0, 'resets': datetime.fromisoformat(FUTURE)},
              'seven_day': {'used': 61.0, 'resets': None}},
              'cached_at': time.time(), 'retry_at': time.time() + 600},
          'secundaria': {'state': 'rate limited - retry after 16:01'}}
stub = StubUsage(sample)
screen = render(33, 100, usage=stub, keys=[-1, ord('r'), 27])
text = '\n'.join(screen.row(y) for y in range(33))
assert stub.started == [True], 'r must force a refresh'
assert ' 46 %' in text and ' 61 %' in text and 'resets ' in text
assert 'ACTIVE' in text and 'cached ' in text and 'retry ' in text
assert 'rate limited - retry after 16:01' in text
assert 'r Refresh usage' in text
bar_row = next(screen.row(y) for y in range(33) if ' 46 %' in screen.row(y))
assert account.GLYPHS['full'] in bar_row and account.GLYPHS['empty'] in bar_row
assert len(bar_row) <= 2 + min(100 - 4, 72), 'Bar row must stay inside the card'
# Weekly resets show weekday and full time without truncation.
weekly = {'principal': {'state': 'ok', 'windows': {'seven_day': {
    'used': 100.0, 'resets': datetime.now(timezone.utc) + timedelta(days=3)}}}}
week_screen = render(33, 100, usage=StubUsage(weekly))
week_row = next(week_screen.row(y) for y in range(33) if '100 %' in week_screen.row(y))
expected = account.format_reset(weekly['principal']['windows']['seven_day']['resets'])
assert expected in week_row, (expected, week_row)
loading = '\n'.join(render(33, 100, usage=StubUsage({}, loading=True)).row(y) for y in range(33))
assert 'loading usage' in loading
compact = '\n'.join(render(20, 60, usage=stub).row(y) for y in range(20))
assert '46%' in compact and '61%' in compact and 'rate limited' in compact
# ------------------------------------------------------- bar color gradient
pairs = {}


def render_colored(height, width, usage, colors=256):
    """Render with a fake color terminal; color_pair(n) is encoded as n << 8."""
    screen = FakeScreen(height, width)
    with patch.object(account.curses, 'wrapper', side_effect=lambda draw: draw(screen)), \
         patch.object(account.curses, 'set_escdelay', create=True), \
         patch.object(account.curses, 'curs_set'), \
         patch.object(account.curses, 'has_colors', return_value=True), \
         patch.object(account.curses, 'start_color'), \
         patch.object(account.curses, 'use_default_colors'), \
         patch.object(account.curses, 'init_pair', side_effect=lambda n, fg, bg: pairs.__setitem__(n, fg)), \
         patch.object(account.curses, 'color_pair', side_effect=lambda n: n << 8), \
         patch.object(account.curses, 'COLORS', colors, create=True), \
         patch.object(account.curses, 'COLOR_PAIRS', 256, create=True), \
         patch.dict('os.environ', {}, clear=True), \
         patch.object(account, 'account_overview', return_value=OVERVIEW):
        account.select('Your accounts', [label for label, _ in account.MENU], (),
                       dashboard=True, usage=usage, groups=account.MENU_GROUPS)
    return screen


def bar_colors(screen, row):
    """Foreground color of each filled bar cell, left to right."""
    full = account.GLYPHS['full']
    cells = sorted(x for (y, x), char in screen.cells.items() if y == row and char == full)
    return [pairs.get((screen.styles[(row, x)] >> 8) & 0xff) for x in cells]


def text_color(screen, row, text):
    line = screen.row(row)
    return pairs.get((screen.styles[(row, line.index(text))] >> 8) & 0xff)


gradient = StubUsage({'principal': {'state': 'ok', 'windows': {
    'five_hour': {'used': 93.0, 'resets': None}, 'seven_day': {'used': 12.0, 'resets': None}}}})
screen = render_colored(34, 100, gradient)
high = next(y for y in range(34) if ' 93 %' in screen.row(y))
low = next(y for y in range(34) if ' 12 %' in screen.row(y))
high_colors, low_colors = bar_colors(screen, high), bar_colors(screen, low)
ramp = list(account.GRADIENT_256)
# A high reading walks the whole ramp, in order, from green to red.
assert high_colors[0] == ramp[0] and high_colors[-1] == ramp[-1], high_colors
assert all(ramp.index(a) <= ramp.index(b) for a, b in zip(high_colors, high_colors[1:]))
assert set(high_colors) == set(ramp), 'Every gradient step is used'
# A low reading stays at the green end: its cells match the same positions.
assert low_colors == high_colors[:len(low_colors)] and set(low_colors) <= set(ramp[:2])
# Percentages take the color where their bar ends; reset times stay muted.
assert text_color(screen, high, '93 %') == ramp[account.gradient_step(0.93)]
assert text_color(screen, low, '12 %') == ramp[account.gradient_step(0.12)]
# Empty cells are not colored.
empty = [x for (y, x), char in screen.cells.items() if y == low and char == account.GLYPHS['empty']]
assert empty and all(not (screen.styles[(low, x)] >> 8) & 0xff for x in empty)
# The compact layout uses the same gradient.
compact = render_colored(20, 60, gradient)
compact_row = next(y for y in range(20) if '93%' in compact.row(y))
assert len(set(bar_colors(compact, compact_row))) >= 4
# 8-color terminals fall back to green, yellow and red by position.
basic = render_colored(34, 100, gradient, colors=8)
basic_colors = bar_colors(basic, high)
expected = [account.curses.COLOR_GREEN, account.curses.COLOR_YELLOW, account.curses.COLOR_RED]
assert [c for i, c in enumerate(basic_colors) if i == 0 or c != basic_colors[i - 1]] == expected
# Gradient helpers: bounds and plain-text bars.
assert account.gradient_step(0) == 0 and account.gradient_step(1) == len(ramp) - 1
assert account.gradient_step(-1) == 0 and account.gradient_step(2) == len(ramp) - 1
with patch.dict('os.environ', {'TERM': 'xterm-256color'}, clear=True), \
        patch.object(account.sys.stdout, 'isatty', return_value=True):
    assert account.ansi_colors() == 256
    with patch.dict('os.environ', {'NO_COLOR': '1'}):
        assert account.ansi_colors() == 0
with patch.object(account.sys.stdout, 'isatty', return_value=False):
    assert account.ansi_colors() == 0
plain = account.ansi_bar(50, 10, 0)
assert plain == account.usage_bar(50, 10) and '\x1b' not in plain
colored = account.ansi_bar(93, 20, 256)
assert f'38;5;{ramp[0]}m' in colored and f'38;5;{ramp[-1]}m' in colored
assert colored.endswith('\x1b[0m') and colored.count(account.GLYPHS['full']) == 19
print('PASS: positional bar gradient (256-color ramp, 8-color fallback, compact layout, '
      'percent colors, plain-text bars),')

print('PASS: usage parsing, error mapping, caching, cooldowns, live-token lookups, active '
      'renewal, refresh ownership, lock, monitor resilience and progress-bar rendering')
