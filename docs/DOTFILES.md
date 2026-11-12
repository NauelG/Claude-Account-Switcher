# Consuming Claude Account Switcher from dotfiles

Dotfiles should install a pinned revision of this repository, rather than carry a
second copy of its source, tests, and systemd units.

## Migration order

1. Publish and verify the initial repository revision.
2. Pin that immutable commit in the dotfiles installer.
3. Remove the previously managed executable and four systemd unit definitions from
   chezmoi's source. Remove their old unit-enabling script as well.
4. Exclude the executable and units from chezmoi management, so chezmoi does not
   delete files now owned by the standalone installer.
5. Fetch the pinned source revision and run `python3 install.py`.
6. Pass `--enable-renewal` and/or `--enable-windows` only when the host's personal
   configuration requests that automation. Existing active timers stay active.

The command, live credentials, profile registry, schedules, and account metadata
keep their existing paths. No credential conversion or login is required.

## Ownership

- This repository owns the executable, systemd definitions, tests, and documentation.
- The installer owns the deployed executable and units.
- Dotfiles own the source revision pin and personal automation preferences.
- Runtime credentials and state belong to neither source repository.

Avoid installing a moving `main` branch from a remote shell pipeline. Fetch a
specific source archive, verify its checksum, then run the installer. Update the
revision and checksum together in dotfiles after testing a newer release.

If migrating before this repository's first public commit is published, keep the
existing dotfiles deployment until the immutable pin is available. This prevents
an empty remote or an unavailable revision from interrupting the installed tool.
