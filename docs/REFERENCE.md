# Claude Account Switcher reference

Linux account selector for Claude credential files. Requires Python 3 and the
Claude CLI for authentication. Run `ng-claude-account` to open the terminal menu.

## Commands

- `ng-claude-account`: interactive menu.
- `ng-claude-account status`: reconcile credentials and show the active account.
- `ng-claude-account switch`: select the next authenticated account in registry order.
- `ng-claude-account switch principal`: select a specific saved account.
- `ng-claude-account login secundaria`: authenticate a profile and capture its email.
- `ng-claude-account init`: register existing credentials as `principal`.
- `ng-claude-account alias principal Personal`: set a display alias.
- `ng-claude-account renew`: renew the active account's expired token (no browser).
- `ng-claude-account renew --all`: renew every account whose token is expired.
- `ng-claude-account doctor`: offline, read-only diagnosis of the credential layout.
- `ng-claude-account repair principal`: restore a saved account into `~/.claude`.
- `ng-claude-account usage`: 5-hour and 7-day quota usage for every account.
- `ng-claude-account prime-next`: run one due alternating usage-window slot.
- `ng-claude-account schedule status`: inspect the window timer and its next run.
- `ng-claude-account schedule enable`: enable and start the window timer.
- `ng-claude-account schedule disable`: disable and stop future window timer runs.
- Add `--timer renewal` for daily OAuth renewal, or `--timer all` for both timers.
- `ng-claude-account accounts list`: list profiles and the calculated window interval.
- `ng-claude-account accounts add work`: register a new profile and run its isolated login.
- `ng-claude-account accounts remove work`: remove an inactive profile, archiving its credentials.

## Multiple account profiles

Profiles are no longer limited to two. `profiles.json` in the private credential
store holds their ordered names. Existing `principal` and `secundaria` profiles are
registered automatically without renaming, moving, or replacing their credentials.
New names must start with a lowercase letter, contain only lowercase letters,
digits, `_` or `-`, and be at most 40 characters. Internal filenames (`profiles`
and `window-schedule`) are reserved. Aliases remain independent display names.

The menu's **Manage accounts** entry supports adding accounts, removing inactive
accounts, and registering the current credentials. Browser login is isolated as
before. If login is cancelled, the new profile stays pending and can be retried;
pending profiles do not participate in scheduling. An active account cannot be
removed: switch to another one first. Removed credentials, identity metadata and
quota cache move to an owner-only `removed-<name>-*` archive in the same private
store, outside version control. Registry and credential operations share the lock
used by manual and scheduled work.

`switch` without a name cycles through all authenticated profiles in order. Renewal,
usage, reconciliation and scheduled turns use the registry rather than a fixed pair.

The full-screen menu uses Python's standard-library curses module. Its Claude ASCII
logo and ACCOUNT SWITCHER subtitle appear in terminals at least 60 columns wide
and enough rows for all accounts (28 rows for two, 33 for three). Smaller terminals
use a compact paginated account panel. A small terracotta `ng` signature sits right-aligned under the subtitle,
or beside the compact header. The layout adapts when the terminal is resized.

The palette uses terracotta in 256-color terminals, yellow in basic color terminals,
and green for the active account. The ACCOUNTS card lists every profile with its
alias, captured email, and quota progress bars. Everyday actions appear first;
management actions are grouped below when space permits. Group headings are not
selectable. Status and errors have separate panels.

- Up/Down or j/k: move the selection (wraps around).
- Tab/Shift-Tab: move forward/backward.
- Home/End: select the first/last item.
- Page Up/Page Down: scroll long account pickers.
- `[` / `]`: previous/next account page on the compact dashboard.
- `/`: search by alias, profile name or email. Esc clears the current filter.
- Enter: activate the selected item.
- Esc or q: return from a submenu or exit the main menu.
- r: refresh quota usage (main menu).
- Ctrl-C: cancel and restore terminal state.

Accounts are selectable directly. Enter opens contextual Switch, Details, Rename
alias, Renew, Login, Restore and Remove actions for that account. Successful
actions return to the dashboard with a six-second notification; errors stay visible
until the user chooses another action. Quota read status and token expiry are
displayed separately. Alias and search fields support selection, a visible cursor,
Left/Right, Home/End, Delete, Backspace and Ctrl+U, with inline validation.

Set `NO_COLOR=1` to disable colors; selection remains highlighted in reverse video.
Direct subcommands work without a terminal; the menu requires both terminal input
and output. During authentication, the menu restores normal terminal operation for
Claude's login flow. No additional Python packages are required.

## Quota usage

Each account shows its 5-hour and 7-day utilization as progress bars, with reset
times in local time. Bars use a positional gradient: each cell is colored by where
it sits along the bar, from green through yellow and orange to red, so a low reading
stays green and a high one shows the whole ramp. The percentage takes the color of
the point its bar reaches. 256-color terminals get an eight-step ramp; others fall
back to green, yellow and red. `NO_COLOR` disables it. The `usage` command prints
the same gradient when its output is a terminal. Usage loads in the background when the menu opens; the menu is usable
immediately and the bars appear when results arrive. Lookups for all accounts run
in parallel (at most four at a time) with an 8-second request timeout.

Usage comes from `GET https://api.anthropic.com/api/oauth/usage`, the same
undocumented endpoint used by Claude Code and the `opencode-quota` plugin. It may
change without notice; failures show a message instead of bars and never affect
switching or other actions.

The endpoint is rate limited per token. When it answers HTTP 429, the cooldown from
`Retry-After` is saved and honoured: that account is not queried again until it
expires, even when pressing `r`. Meanwhile the last known usage is shown, marked
`cached HH:MM, retry HH:MM`. Because OpenCode's quota plugin also polls the active
account, that account is more likely to hit the limit. Results under 60 seconds old
are reused when the menu reopens; `r` bypasses that freshness window.

Cached figures whose window has since reset show `window reset since`. The cache
(`<profile>.usage.json`) stores only percentages and timestamps, never tokens, and
logging in to a profile clears it.

Token handling: the active account is looked up with the live token in `~/.claude`,
and only once reconciliation has verified its owner. An expired active token is
renewed first, under the OpenCode plugin's lock (see below). An expiring inactive
token is refreshed under the tool's lock before its lookup. With no active profile
recorded, nothing is refreshed.

## Staggered usage-window schedule (24/7)

`ng-claude-account-window.timer` checks for due turns once a minute across midnight.
Actual account turns have interval **5 hours / N authenticated profiles**, so each
account gets a turn approximately every five hours. One account means five hours,
two means 2 h 30 min, three means 1 h 40 min, and four means 1 h 15 min. Adding or
removing profiles recalculates the interval without editing systemd units. A check
before the next due turn makes no quota request and sends no prompt. Timing is
subject to the one-minute polling resolution and host availability. This is
independent of daily OAuth renewal: refreshing a token does not start a usage window.

The first turn after boot is scheduled after five minutes. The profile cursor and
last attempt survive reboot in `window-schedule.json`. Missed turns are not replayed;
a due turn runs after boot, and the cadence then starts from that run. An early
service start is skipped if the calculated interval has not elapsed since its last
slot (with five seconds of timing tolerance).
Thus rebooting or restarting the service can shift the timetable; this is not a
fixed wall-clock schedule or a guarantee of an exact quota-reset offset. Pending
profiles are ignored; if the next due profile was removed, the cursor falls back
to the first authenticated profile. With no authenticated profiles, checks do nothing.

Each turn checks the account's live five-hour `resets_at` first. If it is in the
future, the window is already open and no prompt is sent. An expired reset, or an
explicit `utilization: 0, resets_at: null` response, permits one minimal Haiku
prompt, "Reply with OK only." The new reset time is checked afterward. A missing
or malformed reset, exhausted weekly quota, unknown credentials, network failure,
or usage-endpoint cooldown skips the prompt. There is no immediate retry, even
after a timeout: the request may already have reached the provider.

The Claude CLI runs in a temporary working and configuration directory, with safe
mode, no tools, no MCP servers, no session persistence, and the selected OAuth
access token in its environment (never in command arguments or logs). The live
credentials file and OpenCode's selected profile are never swapped for a prompt.
Normal token renewal may still update the active token when it has expired.

A prompt consumes some quota, including weekly quota. It can start a window but
cannot reset an open one, replenish usage, or guarantee exactly 0% use. Actual use,
quota endpoint delays, downtime, and skipped turns can alter the offset. This
depends on an undocumented usage endpoint; a successful prompt is only reported as
a confirmed new window when the subsequent reset timestamp verifies it.

Inspect or disable this schedule:

```sh
ng-claude-account status
ng-claude-account schedule status --timer all
ng-claude-account schedule enable
ng-claude-account schedule disable
systemctl --user list-timers ng-claude-account-window.timer
journalctl --user -u ng-claude-account-window.service
systemctl --user disable --now ng-claude-account-window.timer
```

The terminal menu's **Automation** entry opens a picker for usage windows or daily
token renewal, with live status, the next execution time, and Enable/Disable
actions. Status is refreshed after each action. These controls work without Claude
credentials. Disabling a timer prevents future turns; a turn already running may
still finish. The timer stays active between runs; its oneshot service normally
shows inactive. Controls require the installed units and a running user systemd
manager; failures are reported rather than silently accepted.

The independent installer installs both timers on Linux but only enables them
when explicitly requested with `--enable-renewal` or `--enable-windows`. The CLI must
support `--safe-mode`, `--tools`, and `--no-session-persistence` (checked against the
installed CLI). No browser login or interactive permission prompt is used.

## Storage and behavior

The executable is installed at `~/.local/bin/ng-claude-account`. The previous
`claude-account` executable was renamed; existing credential storage was retained.

Saved profiles live in `$XDG_DATA_HOME/claude-account` or, by default,
`~/.local/share/claude-account`, with the active profile's name in its `active` file.
`~/.claude/.credentials.json` is a regular file holding the active account's
credentials. Settings, skills, and history are not switched. Non-Claude credential
sections, including MCP authentication, are carried forward when switching.

### Why not a symlink

Versions before this one exposed the active profile through a symlink. Claude Code
(verified in 2.1.322) saves credentials by writing a temporary file and renaming it
over `~/.claude/.credentials.json`, which replaces any symlink with a regular file.
Every Claude CLI token renewal therefore broke the link: renewals stopped reaching
the saved profile, and refresh tokens rotated by Claude went stale there. Older
links are converted into a regular file automatically on the next run.

### Reconciliation

Before every action (and when the menu opens), the tool compares `~/.claude` with
the active profile. When they differ, typically because Claude Code or OpenCode
renewed the token, sometimes because of a direct `claude auth login`, it asks
Anthropic which account owns the live token (`GET /api/oauth/profile`, the
endpoint Claude Code itself uses) and compares its account UUID with the profiles':

- Active account: the renewed tokens are saved into its profile.
- Other registered account: they are saved into that profile, which becomes active.
- Unregistered account: nothing is attributed; the dashboard names the problem.
- Verification unavailable (offline or API error): nothing is attributed.

An expired live token is renewed first, so its owner can be verified. Credentials
that are not attributed are left in place. A later switch or restore saves what
`~/.claude` holds into its own profile when it still belongs to it, or otherwise
makes an exact private backup (`displaced-*.json`) before replacing it.

### Coordination with OpenCode

The `opencode-claude-auth@2.2.1` plugin serialises token renewals of file-based
credentials with `~/.local/share/opencode/claude-auth-refresh-<hash>.lock`. This
tool takes the same lock (honouring `OPENCODE_CLAUDE_AUTH_REFRESH_LOCK_DIR` and its
20-second staleness rule) while renewing or replacing `~/.claude`. When the plugin
renews the token meanwhile, its result is used instead of renewing twice with one
refresh token. The Claude CLI uses its own locking; concurrent writes from it are
reconciled on the next run rather than prevented.

The plugin rereads credentials after its 30-second cache expires, so a switch takes
effect within about 30 seconds. Requests already in progress are unaffected.

`renew` renews the active token when it is expired or about to expire. Re-authenticating
in the browser is only needed when the refresh token itself is rejected.

### Token lifetimes and daily renewal

Verified against Anthropic: an access token lasts 8 hours. Every renewal returns a
new refresh token, invalidating the previous one, valid for about 28 days from that
renewal (`refresh_token_expires_in`). An account whose token is not renewed for 28
days is signed out and needs a browser login. `refreshTokenExpiresAt` is updated on
every renewal, as Claude Code does, and `status`/`doctor` show it as the session
expiry; `doctor` warns 5 days ahead.

`renew --all` (menu: Renew expired tokens) renews every registered account whose
access token is expired or about to expire: the active one in `~/.claude` under the
plugin lock, the others in their profiles. Valid tokens are left alone, because each
renewal rotates the refresh token and gains nothing early. One failure does not stop
the others; the command exits with status 1 if any account needs attention.

A systemd user timer, `ng-claude-account-renew.timer`, runs `renew --all` daily
(randomized within an hour; missed runs happen at the next boot). Idle accounts'
access tokens are always expired by then, so each run renews them, restarting their
28-day session. With lingering enabled (`loginctl enable-linger`), it also runs
without an open session. Check it with:

```sh
systemctl --user list-timers ng-claude-account-renew.timer
journalctl --user -u ng-claude-account-renew.service
```

The installer deploys the units without changing activation by default. Pass
`--enable-renewal` or use `schedule enable --timer renewal` to activate this timer.
Hosts without a user systemd manager can still install and run the manual tool.

### HTTP requests

`claude.ai` is behind Cloudflare, which rejects Python's default User-Agent
(`Python-urllib/3.x`) with HTTP 403, error code 1010, before the request reaches the
OAuth server. Every request therefore sends `User-Agent: ng-claude-account/1.0`.
Earlier versions omitted it, so their token renewals always failed with HTTP 403.

## Profile identity

The login command captures `oauthAccount.emailAddress` and `accountUuid` from the
isolated login's `.claude.json`. It stores them alongside the profile's display
alias in `<profile>.identity.json`, with owner-only permissions. Aliases do not
change registered profile names or credential paths.
Use the menu's alias editor or the `alias` subcommand to customize them.

Profiles registered without a login (for example by `init`) learn their account
UUID and email from Anthropic the first time reconciliation needs them. Logging in
to the active profile updates `~/.claude` immediately. Authentication runs in a
temporary directory and updates the chosen saved profile only after success. It
preserves its alias and the current MCP credentials. Logging in the same Claude
account into both profiles prints a warning.

Global `~/.claude.json` is never used to infer identity. A missing email in a new
login clears the old identity instead of showing stale metadata.

## Diagnosis and restore

`doctor` checks the credentials file, the recorded active profile, whether the file
matches it, per-profile identity metadata, and local token expiry. It does not write
files or contact Anthropic. A file changed outside the tool is reported as a note,
not a problem: it is verified and saved on the next run. It exits with status 1 for
structural problems and 0 otherwise. Token validity here means local expiry only.

`repair <profile>` (menu: Restore an account) writes a saved account into
`~/.claude`, refreshing it first when it is about to expire. What `~/.claude` held is
saved into its own profile when it still belongs to it, or backed up byte-for-byte,
including malformed files. Links owned by another manager are refused. A failed
refresh changes nothing.

Backups and identity metadata are personal runtime data and must remain outside
version control.

## Installation and tests

Source repository: [NauelG/Claude-Account-Switcher](https://github.com/NauelG/Claude-Account-Switcher).
The source command is `bin/ng-claude-account`. Install on Linux:

```sh
python3 install.py
ng-claude-account
```

This is a terminal utility; it does not require managing OpenCode's configuration.

Only Linux credential files are supported. Python 3.11 or later with curses support
is required; no third-party Python packages are used. The installer deploys the
command with mode 700 and installs systemd units without activating new automation.
Use `--enable-renewal` and `--enable-windows` to opt in, or `--no-systemd` to skip
units entirely. Custom `--prefix` and `--unit-dir` paths support staged installs.

Track only source, documentation, and tests. Runtime credentials, identity metadata,
backups, and usage state must stay outside this repository. See [dotfiles
integration](DOTFILES.md) to consume a pinned version from configuration management.

Run the tests from the repository root on Linux:

```sh
make test
```

Tests use temporary homes and synthetic tokens; no real authentication or network
requests are required. The HTTP layer itself (headers, form body, response parsing)
runs against a fake server rather than being mocked away, so a missing User-Agent
or a dropped field fails the suite. The credentials suite emulates a fake Anthropic and both
writers of `~/.claude`: Claude Code's rename-over-the-file saves and the plugin's
in-place writes, including renewals racing a switch. The TUI suite uses a
pseudo-terminal to verify arrows, colors, cancellation, and terminal restoration.
Installer tests use temporary prefixes and mock the user systemd manager.
