"""Credential reconciliation tests for ng-claude-account.

Network access is replaced by a fake Anthropic: tokens encode their account
("acc-<who>-<n>") and refreshes rotate both tokens, as the real endpoint does.
"""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'bin/ng-claude-account'
loader = importlib.machinery.SourceFileLoader('account', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
account: Any = importlib.util.module_from_spec(spec)
loader.exec_module(account)
account.UsageMonitor.start = lambda self, force=False: None

ACCOUNTS = {'a': {'uuid': 'uuid-a', 'email': 'a@example.test'},
            'b': {'uuid': 'uuid-b', 'email': 'b@example.test'},
            'c': {'uuid': 'uuid-c', 'email': 'c@example.test'}}
network = {'online': True, 'refreshes': []}


def fake_account_of(token):
    if not network['online']:
        raise account.IdentityUnavailable('network error')
    return dict(ACCOUNTS[token.split('-')[1]])


def fake_refresh(oauth):
    if not network['online']:
        raise ValueError('Token refresh failed (network error); nothing was changed.')
    network['refreshes'].append(oauth['refreshToken'])
    _, who, number = oauth['refreshToken'].split('-')
    number = int(number) + 1
    return {**oauth, 'accessToken': f'acc-{who}-{number}', 'refreshToken': f'ref-{who}-{number}',
            'expiresAt': int(time.time() * 1000 + 8 * 3600 * 1000)}


def no_network(*args, **kwargs):
    raise AssertionError('Unexpected network access')


REAL_REFRESH_OAUTH = account.refresh_oauth
REAL_ACCOUNT_OF = account.account_of
account.account_of = fake_account_of
account.refresh_oauth = fake_refresh
account.urllib.request.urlopen = no_network


def creds(who, number, expires_in=3600, mcp='shared'):
    return {'claudeAiOauth': {'accessToken': f'acc-{who}-{number}',
                              'refreshToken': f'ref-{who}-{number}',
                              'expiresAt': int(time.time() * 1000 + expires_in * 1000),
                              'scopes': ['user:inference']},
            'mcpOAuth': {'server': mcp}}


def token(path):
    return json.loads(Path(path).read_text())['claudeAiOauth']['accessToken']


def claude_cli_write(data):
    """Save the way Claude Code 2.1.x does: temporary file renamed over the path."""
    temporary = account.LIVE.parent / '.credentials.json.tmp'
    temporary.write_text(json.dumps(data))
    os.rename(temporary, account.LIVE)


def plugin_write(data):
    """Save the way the OpenCode plugin does: writeFileSync, in place."""
    with open(account.LIVE, 'w') as stream:
        stream.write(json.dumps(data))


def fresh():
    root = Path(tempfile.mkdtemp(prefix='ng-claude-credentials-'))
    account.STORE = root / 'accounts'
    account.LIVE = root / '.claude/.credentials.json'
    account.PLUGIN_LOCK_DIR = root / 'opencode'
    account.LAST_SYNC = {'status': 'unsynced', 'notes': []}
    account.LIVE.parent.mkdir()
    network.update(online=True, refreshes=[])
    return root


def run(*args):
    output = io.StringIO()
    with patch('sys.argv', ['ng-claude-account', *args]), \
            patch.dict('os.environ', {}, clear=True), contextlib.redirect_stdout(output):
        account.main()
    return output.getvalue()


def fails(*args):
    try:
        run(*args)
    except (ValueError, OSError) as error:
        return str(error)
    raise AssertionError(f'Expected failure: {args}')


def mode(path):
    return Path(path).stat().st_mode & 0o777


def fake_login(who, number):
    def login(command, env, check):
        assert command == ['claude', 'auth', 'login'] and check
        target = Path(env['CLAUDE_CONFIG_DIR'])
        assert target != account.LIVE.parent, 'Login must be isolated'
        (target / '.credentials.json').write_text(json.dumps(creds(who, number)))
        (target / '.claude.json').write_text(json.dumps({'oauthAccount': {
            'emailAddress': ACCOUNTS[who]['email'], 'accountUuid': ACCOUNTS[who]['uuid']}}))
    return login


# --------------------------------------- real HTTP layer, against a fake server
# These run the actual request code (only urlopen is replaced), so header and
# parsing mistakes cannot hide behind a mocked refresh_oauth().
import urllib.error
from email.message import Message


requests = []


def fake_server(responses):
    def urlopen(request, timeout):
        requests.append(request)
        reply = responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(json.dumps(reply).encode())  # BytesIO is a context manager.
    return urlopen


def http_error(code, body):
    return urllib.error.HTTPError('https://example.test', code, 'error', Message(),
                                  io.BytesIO(body))


oauth = {'accessToken': 'old-access', 'refreshToken': 'old-refresh', 'expiresAt': 1,
         'refreshTokenExpiresAt': 2, 'scopes': ['user:inference'], 'subscriptionType': 'max'}
with patch.object(account.urllib.request, 'urlopen', side_effect=fake_server([{
        'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 28800,
        'refresh_token_expires_in': 2445000, 'scope': 'user:inference user:profile',
        'account': {'uuid': 'uuid-a'}}])):
    updated = REAL_REFRESH_OAUTH(dict(oauth))
request = requests[-1]
user_agent = request.get_header('User-agent') or ''
assert user_agent.startswith('ng-claude-account/'), user_agent
assert 'Python-urllib' not in user_agent, 'Cloudflare rejects the default User-Agent (error 1010)'
assert request.full_url == account.OAUTH_TOKEN_URL and request.get_method() == 'POST'
form = dict(pair.split('=', 1) for pair in request.data.decode().split('&'))
assert form == {'grant_type': 'refresh_token', 'client_id': account.OAUTH_CLIENT_ID,
                'refresh_token': 'old-refresh'}
assert updated['accessToken'] == 'new-access' and updated['refreshToken'] == 'new-refresh'
assert abs(updated['expiresAt'] / 1000 - time.time() - 28800) < 5
assert abs(updated['refreshTokenExpiresAt'] / 1000 - time.time() - 2445000) < 5
assert updated['scopes'] == ['user:inference', 'user:profile']
assert updated['subscriptionType'] == 'max', 'Unrelated fields are kept'
# Without a reported session lifetime, the stale expiry is dropped, not kept.
with patch.object(account.urllib.request, 'urlopen', side_effect=fake_server([{
        'access_token': 'new-access', 'expires_in': 28800}])):
    updated = REAL_REFRESH_OAUTH(dict(oauth))
assert 'refreshTokenExpiresAt' not in updated and updated['refreshToken'] == 'old-refresh'
assert updated['scopes'] == ['user:inference']
# Failure messages: revoked grants ask for a login; others never leak bodies.
for error, expected in (
        (http_error(400, b'{"error": "invalid_grant"}'), 'log in to this account again'),
        (http_error(403, b'error code: 1010'), 'HTTP 403'),
        (OSError('down'), 'network error')):
    with patch.object(account.urllib.request, 'urlopen', side_effect=fake_server([error])):
        try:
            REAL_REFRESH_OAUTH(dict(oauth))
            raise AssertionError('Expected refresh failure')
        except ValueError as failure:
            assert expected in str(failure) and 'old-refresh' not in str(failure)
for reply in ({'expires_in': 28800}, {'access_token': 'x', 'expires_in': 30}, ['list']):
    with patch.object(account.urllib.request, 'urlopen', side_effect=fake_server([reply])):
        try:
            REAL_REFRESH_OAUTH(dict(oauth))
            raise AssertionError('Expected invalid response')
        except ValueError:
            pass
# The profile lookup identifies itself too, and caches per token.
account._ACCOUNTS.clear()
with patch.object(account.urllib.request, 'urlopen', side_effect=fake_server([
        {'account': {'uuid': 'uuid-z', 'email': 'z@example.test'}}])):
    assert REAL_ACCOUNT_OF('tok-z') == {'uuid': 'uuid-z', 'email': 'z@example.test'}
    assert REAL_ACCOUNT_OF('tok-z')['uuid'] == 'uuid-z'  # Cached: no second request.
assert requests[-1].get_header('User-agent').startswith('ng-claude-account/')
assert requests[-1].get_header('Authorization') == 'Bearer tok-z'
with patch.object(account.urllib.request, 'urlopen', side_effect=fake_server([{'account': {}}])):
    try:
        REAL_ACCOUNT_OF('tok-y')
        raise AssertionError('Expected IdentityUnavailable')
    except account.IdentityUnavailable:
        pass
account._ACCOUNTS.clear()

# ------------------------------------------------ first run and registration
root = fresh()
account.LIVE.write_text(json.dumps(creds('a', 1)))
settings = account.LIVE.parent / 'settings.json'
settings.write_text('{"test": "unchanged"}')
out = run('status')
assert 'Registered the current credentials as principal' in out
assert account.active() == 'principal'
assert account.LIVE.is_file() and not account.LIVE.is_symlink()
assert token(account.credential_path('principal')) == 'acc-a-1'
assert all(mode(p) == 0o600 for p in (account.credential_path('principal'), account.state_path()))
# A profile registered without login learns its account id from Anthropic.
assert 'accountUuid' not in account.identity('principal')
assert account.profile_uuid('principal') == 'uuid-a'
assert account.identity('principal')['email'] == 'a@example.test'

with patch.object(account.subprocess, 'run', side_effect=fake_login('b', 1)):
    out = run('login', 'secundaria')
assert 'Active account remains principal' in out
assert token(account.LIVE) == 'acc-a-1', 'Logging in an inactive profile must not touch ~/.claude'
assert account.identity('secundaria')['accountUuid'] == 'uuid-b'

out = run('switch')
assert account.active() == 'secundaria' and token(account.LIVE) == 'acc-b-1'
assert json.loads(account.LIVE.read_text())['mcpOAuth'] == {'server': 'shared'}
assert token(account.credential_path('principal')) == 'acc-a-1'
assert mode(account.LIVE) == 0o600

# ------------------------------- regression: Claude Code renews the token
# Today's failure: the CLI renewed the token and replaced the file. The renewal
# must be adopted, so switching away and back never restores a stale token.
claude_cli_write(creds('b', 2))
out = run('status')
assert 'Saved the token renewed outside this tool for secundaria' in out
assert token(account.credential_path('secundaria')) == 'acc-b-2'
run('switch')
assert token(account.LIVE) == 'acc-a-1'
run('switch')
assert token(account.LIVE) == 'acc-b-2', 'A renewal made by Claude Code was lost'
plugin_write(creds('b', 3))  # The OpenCode plugin's in-place writes are adopted too.
run('status')
assert token(account.credential_path('secundaria')) == 'acc-b-3'
assert network['refreshes'] == [], 'No renewal was needed'

# A renewal landing between reconciliation and the switch is not lost either.
with account.tool_lock():
    state = account.sync()
    claude_cli_write(creds('b', 4))
    with contextlib.redirect_stdout(io.StringIO()):
        account.switch('principal', state)
assert token(account.credential_path('secundaria')) == 'acc-b-4'
assert token(account.LIVE) == 'acc-a-1'
assert not list(account.STORE.glob('displaced-*.json')), 'Verified renewals need no backup'

# ------------------------------------------- direct login of the other account
run('switch', 'secundaria')
claude_cli_write(creds('a', 9))
out = run('status')
assert 'it is now the active account' in out
assert account.active() == 'principal'
assert token(account.credential_path('principal')) == 'acc-a-9'
assert token(account.credential_path('secundaria')) == 'acc-b-4'

# ---------------------------------------------- unregistered account in ~/.claude
claude_cli_write(creds('c', 1))
unknown = account.LIVE.read_bytes()
profiles = {n: account.credential_path(n).read_bytes() for n in account.profiles()}
out = run('status')
assert 'unregistered account (c@example.test)' in out
assert account.LIVE.read_bytes() == unknown, 'Unattributed credentials must be left in place'
assert profiles == {n: account.credential_path(n).read_bytes() for n in account.profiles()}
assert 'Cannot renew' in fails('renew')
out = run('switch', 'secundaria')
backups = list(account.STORE.glob('displaced-*.json'))
assert len(backups) == 1 and backups[0].read_bytes() == unknown and mode(backups[0]) == 0o600
assert 'Preserved the previous credentials file' in out
assert account.active() == 'secundaria' and token(account.LIVE) == 'acc-b-4'

# ----------------------------------------------- offline: change left untouched
claude_cli_write(creds('b', 5))
network['online'] = False
out = run('status')
assert 'could not be verified (network error)' in out and 'left untouched' in out
assert token(account.credential_path('secundaria')) == 'acc-b-4'
assert token(account.LIVE) == 'acc-b-5'
network['online'] = True
run('status')
assert token(account.credential_path('secundaria')) == 'acc-b-5'

# ------------------------------- expired external change: renewed, then verified
claude_cli_write(creds('b', 6, expires_in=-10))
run('status')
assert network['refreshes'] == ['ref-b-6']
assert token(account.LIVE) == 'acc-b-7' and token(account.credential_path('secundaria')) == 'acc-b-7'

# ------------------------------------------------------ renewing the active token
expired = creds('b', 8, expires_in=-10)
account.write_json(account.LIVE, expired)
account.write_json(account.credential_path('secundaria'), expired)
out = run('renew')
assert 'Renewed the token for secundaria' in out
assert token(account.LIVE) == 'acc-b-9' and token(account.credential_path('secundaria')) == 'acc-b-9'
count = len(network['refreshes'])
assert 'no renewal needed' in run('renew') and len(network['refreshes']) == count
assert not account.plugin_lock_path().exists(), 'The plugin lock must be released'

# The OpenCode plugin holds its lock and renews meanwhile: use its result.
expired = creds('b', 10, expires_in=-10)
account.write_json(account.LIVE, expired)
account.write_json(account.credential_path('secundaria'), expired)
account.plugin_lock_path().parent.mkdir(parents=True, exist_ok=True)
account.plugin_lock_path().write_text('{}')


def plugin_renews():
    time.sleep(0.6)
    plugin_write(creds('b', 11))
    account.plugin_lock_path().unlink()


worker = threading.Thread(target=plugin_renews)
worker.start()
out = run('renew')
worker.join()
assert 'Another process had just renewed the token' in out
assert len(network['refreshes']) == count, 'Must not renew twice with one refresh token'
assert token(account.credential_path('secundaria')) == 'acc-b-11'

# A stale lock left by a crashed process is taken over.
expired = creds('b', 12, expires_in=-10)
account.write_json(account.LIVE, expired)
account.write_json(account.credential_path('secundaria'), expired)
account.plugin_lock_path().write_text('{}')
old = time.time() - 120
os.utime(account.plugin_lock_path(), (old, old))
run('renew')
assert token(account.LIVE) == 'acc-b-13' and not account.plugin_lock_path().exists()
# A live lock that is never released times out instead of hanging.
account.plugin_lock_path().write_text('{}')
try:
    with account.plugin_refresh_lock(wait=0.3):
        raise AssertionError('Lock must not be acquired')
except ValueError as error:
    assert 'OpenCode is renewing' in str(error)
account.plugin_lock_path().unlink()
# The lock file is the one the plugin uses for file-based credentials.
assert account.plugin_lock_path().name == (
    'claude-auth-refresh-' + hashlib.sha256(b'file').hexdigest()[:16] + '.lock')

# --------------------------------------- failed renewal leaves everything as is
account.write_json(account.credential_path('principal'), creds('a', 9, expires_in=-10))
before = account.LIVE.read_bytes()
network['online'] = False
assert 'network error' in fails('switch', 'principal')
network['online'] = True
assert account.active() == 'secundaria' and account.LIVE.read_bytes() == before

# ------------------------------------------- doctor: offline and read-only
claude_cli_write(creds('b', 20))
snapshot = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
report = io.StringIO()
with contextlib.redirect_stdout(report), patch.object(account, 'account_of', side_effect=no_network):
    assert account.diagnose() is True, 'An external renewal is expected, not a problem'
text = report.getvalue()
assert 'regular file' in text and 'Changed outside this tool' in text
assert 'acc-' not in text and 'ref-' not in text, 'Diagnostics must never print tokens'
assert snapshot == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}

# --------------------------------------------- login into the active profile
with patch.object(account.subprocess, 'run', side_effect=fake_login('b', 30)):
    out = run('login', 'secundaria')
assert 'it is the active account' in out
assert token(account.LIVE) == 'acc-b-30' and account.active() == 'secundaria'
with patch.object(account.subprocess, 'run', side_effect=fake_login('b', 40)):
    out = run('login', 'principal')
assert 'same Claude account as secundaria' in out
assert settings.read_text() == '{"test": "unchanged"}'

# ------------------------------------------ migration from the symlink layout
root = fresh()
account.STORE.mkdir()
account.write_json(account.credential_path('principal'), creds('a', 1))
account.write_json(account.credential_path('secundaria'), creds('b', 1))
for who, name in (('a', 'principal'), ('b', 'secundaria')):
    account.write_json(account.identity_path(name), {'alias': name, 'accountUuid': ACCOUNTS[who]['uuid']})
account.LIVE.symlink_to(account.credential_path('secundaria'))
assert account.active() == 'secundaria'
report = io.StringIO()
with contextlib.redirect_stdout(report):
    assert account.diagnose() is True
assert 'link (older version)' in report.getvalue()
out = run('status')
assert 'Converted the credentials link into a regular file' in out
assert account.LIVE.is_file() and not account.LIVE.is_symlink()
assert token(account.LIVE) == 'acc-b-1' and account.active() == 'secundaria'
assert account.state_path().read_text().strip() == 'secundaria'
claude_cli_write(creds('b', 2))  # After migration, CLI writes are adopted as usual.
run('status')
assert token(account.credential_path('secundaria')) == 'acc-b-2'

# Links owned by another tool are refused and left alone.
account.LIVE.unlink()
foreign = root / 'foreign.json'
foreign.write_text(json.dumps(creds('c', 1)))
account.LIVE.symlink_to(foreign)
assert 'another manager' in fails('status')
assert account.LIVE.is_symlink() and account.LIVE.resolve() == foreign

# A malformed file is reported, then backed up exactly by a restore.
account.LIVE.unlink()
account.LIVE.write_bytes(b'{bad-json')
assert 'unreadable' in run('status')
run('repair', 'secundaria')
assert any(p.read_bytes() == b'{bad-json' for p in account.STORE.glob('displaced-*.json'))
assert token(account.LIVE) == 'acc-b-2'
assert 'Already in place' in run('repair', 'secundaria')

# ------------------------------------------------ aliases and identity labels
run('alias', 'secundaria', 'Work')
assert 'Work (secundaria)' in account.profile_label('secundaria')
account.identity_path('principal').unlink()
assert 'identity pending' in account.profile_label('principal')
account.write_usage_cache('secundaria', {}, 1.0, time.time() + 3600)
with patch.object(account.subprocess, 'run', side_effect=fake_login('b', 50)):
    run('login', 'secundaria')
assert not account.usage_cache_path('secundaria').exists(), 'Login must clear stale usage'
assert account.identity('secundaria')['alias'] == 'Work'
with tempfile.TemporaryDirectory() as isolated:
    account.capture_identity('secundaria', Path(isolated))
assert 'email' not in account.identity('secundaria'), 'Missing metadata must clear a stale email'

# ------------------------------------------------------------ renew --all
def renew_all():
    """Run `renew --all`; returns (exit code, output)."""
    output = io.StringIO()
    with patch('sys.argv', ['ng-claude-account', 'renew', '--all']), \
            patch.dict('os.environ', {}, clear=True), contextlib.redirect_stdout(output):
        try:
            account.main()
        except SystemExit as error:
            return error.code, output.getvalue()
    return 0, output.getvalue()


assert account.active() == 'secundaria'
account.write_json(account.credential_path('principal'), creds('a', 60))
count = len(network['refreshes'])
code, out = renew_all()
assert code == 0 and out.count('still valid') == 2, out
assert len(network['refreshes']) == count, 'Valid tokens must not be rotated'

# Both expired: the inactive one is renewed in its profile, the active one in
# ~/.claude (under the plugin lock) and saved to its profile.
expired = creds('b', 70, expires_in=-10)
account.write_json(account.LIVE, expired)
account.write_json(account.credential_path('secundaria'), expired)
account.write_json(account.credential_path('principal'), creds('a', 70, expires_in=-10))
code, out = renew_all()
assert code == 0 and out.count(': renewed,') == 2, out
assert network['refreshes'][-2:] in (['ref-a-70', 'ref-b-70'], ['ref-b-70', 'ref-a-70'])
assert token(account.credential_path('principal')) == 'acc-a-71'
assert token(account.LIVE) == 'acc-b-71' == token(account.credential_path('secundaria'))
assert 'Work (active)' in out and not account.plugin_lock_path().exists()
assert 'session expiry unknown' in out  # The fake refresh reports no session lifetime.

# One revoked account does not stop the others, and the exit code reports it.
account.write_json(account.credential_path('principal'), creds('a', 80, expires_in=-10))
expired = creds('b', 80, expires_in=-10)
account.write_json(account.LIVE, expired)
account.write_json(account.credential_path('secundaria'), expired)


def revoked_a(oauth):
    if oauth['refreshToken'].startswith('ref-a-'):
        raise ValueError('The saved refresh token was rejected; log in to this account again.')
    return fake_refresh(oauth)


with patch.object(account, 'refresh_oauth', side_effect=revoked_a):
    code, out = renew_all()
assert code == 1, out
assert 'principal: The saved refresh token was rejected' in out
assert token(account.LIVE) == 'acc-b-81', 'The active account is still renewed'
assert token(account.credential_path('principal')) == 'acc-a-80', 'Failed renewals change nothing'

# An unregistered account in ~/.claude: the active slot is skipped, others renewed.
account.write_json(account.credential_path('principal'), creds('a', 90, expires_in=-10))
claude_cli_write(creds('c', 1))
code, out = renew_all()
assert code == 1 and 'skipped: unregistered account in ~/.claude' in out, out
assert token(account.credential_path('principal')) == 'acc-a-91'
assert token(account.LIVE) == 'acc-c-1', 'Unattributed credentials are left in place'
run('switch', 'secundaria')  # Back to a clean state (the unknown file is backed up).

# Session lifetime reporting.
soon = creds('a', 95)
soon['claudeAiOauth']['refreshTokenExpiresAt'] = int((time.time() + 3 * 86400) * 1000)
account.write_json(account.credential_path('principal'), soon)
assert 'renew soon' in account.session_summary(soon)
report = io.StringIO()
with contextlib.redirect_stdout(report):
    account.diagnose()
assert 'login session of principal ends in 3 days' in report.getvalue()
soon['claudeAiOauth']['refreshTokenExpiresAt'] = int((time.time() - 60) * 1000)
account.write_json(account.credential_path('principal'), soon)
report = io.StringIO()
with contextlib.redirect_stdout(report):
    assert account.diagnose() is False
assert 'login session of principal has expired' in report.getvalue()
soon['claudeAiOauth']['refreshTokenExpiresAt'] = int((time.time() + 20 * 86400) * 1000)
assert account.session_summary(soon).startswith('session until ')
assert 'renew soon' not in account.session_summary(soon)
account.write_json(account.credential_path('principal'), creds('a', 99))

# --------------------------------------------------- systemd timer units
import configparser

units = SCRIPT.parents[1] / 'systemd/user'
service = configparser.ConfigParser(interpolation=None)
service.read(units / 'ng-claude-account-renew.service')
assert service['Service']['Type'] == 'oneshot'
assert service['Service']['ExecStart'] == '%h/.local/bin/ng-claude-account renew --all'
assert service['Service']['UMask'] == '0077'
timer = configparser.ConfigParser(interpolation=None)
timer.read(units / 'ng-claude-account-renew.timer')
assert timer['Timer']['OnCalendar'] == 'daily' and timer['Timer']['Persistent'] == 'true'
assert timer['Install']['WantedBy'] == 'timers.target'

# ---------------------------------------------------------- menu routing
# Select principal directly, switch from its contextual actions, then exit.
with patch.object(account, 'select', side_effect=[('account', 'principal'), 0, None]):
    account.menu()
assert account.active() == 'principal'
with patch('sys.argv', ['ng-claude-account']), patch.dict('os.environ', {}, clear=True), \
        patch.object(account.sys.stdin, 'isatty', return_value=False):
    try:
        account.main()
        raise AssertionError('Expected non-interactive menu failure')
    except SystemExit as error:
        assert error.code == 2

print('PASS: real refresh and profile requests (User-Agent, form, session expiry, '
      'scopes, failures), renew --all (valid, expired, revoked, unregistered), session '
      'reporting, systemd units,')
print('PASS: registration, regular-file layout, Claude CLI and plugin renewals adopted, '
      'renewal race, direct logins, unregistered and unverified credentials, expired '
      'external changes, active-token renewal with plugin lock, stale and live locks, '
      'failed renewals, offline read-only doctor, symlink migration, foreign links, '
      'restores, aliases, identity and menu routing')
