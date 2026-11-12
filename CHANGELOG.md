# Changelog

## Unreleased

- Make accounts directly selectable on the dashboard, with contextual switching,
  details, alias editing, renewal, login, restoration, and profile removal.
- Add case-insensitive account search, clear authentication/quota states, and
  short success notifications without acknowledgement screens.
- Add cursor-aware Unicode text editing with selection, inline validation, and
  keyboard shortcuts for replacing, deleting, and clearing input.
- Summarize automation activation, next account turn, remaining time, and service
  results in the terminal interface instead of showing raw systemd output.
- Extract Claude Account Switcher from its original chezmoi dotfiles distribution.
- Provide an independent user installer; scheduled automation is opt-in.
- Preserve the existing `ng-claude-account` command and private data layout.
- Include dynamic profiles, credential reconciliation, quota bars, terminal menus,
  daily token renewal, and optional staggered usage-window prompts.
- Run isolated credential, profile, quota, scheduler, installer, and terminal tests
  on Linux with Python 3.11, 3.12, and 3.13.
