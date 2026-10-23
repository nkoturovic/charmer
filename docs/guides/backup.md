# Back up and restore an installation

This guide covers **charmer-owned files only**. It excludes user-owned
Claude Code files: `~/.claude/`, including settings, plugins, memory and
transcripts under `~/.claude/projects/`, and any alternative Claude Code
configuration or transcript directory. Do not add those directories to the
archive. This is **not conversation recovery**: restoring charmer's session
records does not restore the transcripts needed to resume conversations.
Back up your projects and other personal files separately, with their own tools.

## 1. Choose the right operation

| You need | Use |
| --- | --- |
| portable configuration on another computer | [Export and import](move-machines.md): profiles, declarations and reconnect requests, not credentials or sessions |
| recovery after disk loss or a broken home | a cold backup made while all charmer writers are stopped, as below |
| a replacement or second computer | [Move to another computer](move-machines.md), then fresh sign-in; not a clone of live OAuth records |
| undo a recent release update | [Update rollback](../update.md), subject to its state-format and retained-version limits |

There is no automated backup command or TUI backup action. Whether you
start from CLI documentation or the launcher's help, use this same file-level
procedure in a plain terminal outside managed sessions. Export is not a cold
backup, and update rollback is not disk-loss recovery.

## 2. Inventory the actual paths

Do not assume that three default directories contain everything. Record the
release, platform, installation channel, root locations and key-file selection
privately; record names and paths, never credential values. The path rules are
also listed in [Settings and files](../reference/settings.md).

| What to include | Path rule |
| --- | --- |
| operator configuration: profiles, bindings, Settings, preferences and choices | `$XDG_CONFIG_HOME/charmer` when that variable is absolute; otherwise `~/.config/charmer` |
| gateway configuration, provider declarations and ledger, endpoint, continuity, default key file and key-file pointer | always `~/.config/charmer`, even with `XDG_CONFIG_HOME` set |
| session records, scopes, notices, pointers, locks, hook shims, logs and hold/inhibition/ownership documents | `$XDG_STATE_HOME/charmer` when absolute; otherwise `~/.local/state/charmer` |
| supervised gateway's state and working directory | `~/.local/state/charmer`, even if the launcher's state root differs; also include any separately selected or recorded managed state root |
| sign-in records, retained copies, traces, owned client and bundle installation | always `~/.local/share/charmer`; `auth/` holds secret sign-in records; `claude/` holds the owned executable; `install/` holds versions, links and `installer.json` |
| bundle launchers | `~/.local/bin/charmer` and `~/.local/bin/charmer-proxy` |
| Linux supervised service, if used | `$XDG_CONFIG_HOME/systemd/user` when absolute, otherwise `~/.config/systemd/user`: the selected `<unit>.service`, `<unit>-failure.service` and its enable link, normally `default.target.wants/<unit>.service` |

Relative `XDG_CONFIG_HOME` and `XDG_STATE_HOME` are ignored.
**`XDG_DATA_HOME` does not move charmer's data or bundle installation.** It
can select an upstream Claude Code download location used during acquisition;
that upstream installation is not charmer's owned copy and is excluded here.
The service's executable links and gateway paths are HOME-relative.

**Exceptions to resolve before copying:**

- The API-key file can be outside these roots: `CHARMER_SECRET_ENV` wins
  for the launcher/on-demand gateway, then `secret-file.json` selects a
  file, then `secrets/provider-keys.env` is the default. The supervised
  service ignores `CHARMER_SECRET_ENV`; include its pointer-selected or
  default file **as well as** an override used by your shell. Include the
  real external file, not just the pointer. Never source or print it.
- A custom state root, symlinked root, manually placed installation,
  wheel/virtual environment, custom launcher or unit name needs its own
  inventory. The example below refuses symlinked directory roots and does
  not follow links inside archives. Inventory an external target separately;
  never add tar's dereference option to capture it indiscriminately.
- Keep custom certificate files and the external dependencies your service
  needs separately. A link into the Nix store is not a backup of the store;
  reinstall the same package through Nix instead. Do not mix installation
  channels over a restored channel marker.

The gateway's `config.yaml`, `api-key`, `previous-key`, `management-key*`,
provider key files, `auth/` and sign-out backups are **secrets**, including
refresh tokens. Logs and session metadata are private too. Include all retained
hold and recovery evidence; a cold copy must not selectively discard it.
`install/` and `claude/` are reinstallable from trusted release inputs, but
keeping them preserves an offline recovery option for the same platform.

## 3. Quiesce before taking the copy

1. Close **all managed sessions**, including background sessions and their
   agents, across every project and state root. Check the Sessions screen
   (**S**), or `charmer sessions list`; stop background sessions through the
   [supported session controls](sessions.md). An ended foreground window
   alone is not proof that its background work ended. Unknown liveness means
   **stop here**, not permission to copy. Finish any setup, sign-in, update,
   service hand-off or other configuration writer too.
2. Record the release with `charmer --version`. Then request the guarded stop.

```bash
charmer gateway stop
charmer gateway status
```

Continue only after the stop succeeded and status proves the gateway stopped.
Do not open the launch card, resume a session or run setup while copying: the
on-demand gateway can start again when needed. A supervised service must also
stay stopped throughout copying and recovery; do not log out/reboot into an
enabled service while its files are being restored.

If stop is held, **keep the gateway running** and follow its reason-specific
[persistence-hold remedy](gateway.md#the-persistence-hold). A **failed save**
requires a later confirmed durable save of that credential (free space first
if needed; fresh sign-in only if no save comes), not merely clearing a flag.
For a coverage-only hold, check credentials with `charmer providers list`
before considering the explicit, confirmed `charmer gateway clear-hold`.
A **known journald restart** after the gateway start or latest clear, or an
**unavailable journald observation**, is a coverage risk that an explicit
clear can acknowledge after credential inspection. An unavailable observation
remains visible after the clear; another gateway instance needs its own check.
Later failed saves still hold. The separate advisory that system-journal drop
notices could not be read does not hold by itself. Even a successful
observation is not proof of complete journal history.

Never force-kill a held gateway, stop its unit behind the guard, or delete
hold, inhibition, lock or ownership data to get past a refusal. An active or
unreadable inhibition means finish/recover its owning operation first, not
copy a transaction halfway through. If any writer restarts, discard this
attempt as incomplete and make a new cold copy after quiescing again.

## 4. Archive and encrypt

These examples require Bash, GNU tar, `realpath` and GnuPG 2 (`gpg` and
`gpgconf`). On a system where GNU tar is named `gtar`, set `TAR=gtar`.
If a tool is unavailable, stop rather than silently weakening the procedure.
Use a private filesystem you control; use encrypted storage for temporary
and restore workspaces too. Set `backup_parent` to an **existing private
0700 directory**, outside every source tree below and every source-code
checkout, not a synced or publicly shared folder. Keep a second encrypted
copy on separate storage and keep its passphrase separately.

Select the default and XDG roots in the same shell used for the archive.

<!-- example: backup-paths -->
```bash
set -euo pipefail
umask 077
case ${XDG_CONFIG_HOME:-} in
  /*) operator_config=$XDG_CONFIG_HOME/charmer ;;
  *) operator_config=$HOME/.config/charmer ;;
esac
case ${XDG_STATE_HOME:-} in
  /*) session_state=$XDG_STATE_HOME/charmer ;;
  *) session_state=$HOME/.local/state/charmer ;;
esac
gateway_config=$HOME/.config/charmer
gateway_state=$HOME/.local/state/charmer
data_root=$HOME/.local/share/charmer
unit_dir=${operator_config%/charmer}/systemd/user
sources=("$gateway_config" "$data_root" "$session_state"
         "$HOME/.local/bin/charmer" "$HOME/.local/bin/charmer-proxy")
[[ "$operator_config" == "$gateway_config" ]] || sources+=("$operator_config")
if [[ "$session_state" != "$gateway_state" && -d "$gateway_state" ]]; then
  sources+=("$gateway_state")
fi
```

**Review and complete `sources` before the next block.** Append each applicable
unit file and enable link, external key file, custom installation and additional
state root from section 2, as individual absolute paths. Do not archive the whole
home, `.local/bin` or systemd user directory. Omit a missing entry only after
confirming it is unused; a missing key or unaccounted-for root is not a complete
backup. Do not add user-owned Claude Code files. For a selected key file behind
a link, stop and resolve the unsupported layout rather than copying only a link.

The archive stores explicit paths relative to `/`, numeric owner metadata,
modes and symlinks (without following them). No verbose tar output or plaintext
archive is produced. GnuPG asks for a strong passphrase in the terminal; never
put a real passphrase in a command, environment variable or shell history. Its
fresh home below uses no existing keys, configuration or agent.

<!-- example: backup-archive -->
```bash
set -euo pipefail
umask 077
: "${backup_parent:?Choose an existing private backup directory}"
backup_parent=$(realpath -e -- "$backup_parent")
relative_sources=()
for source in "${sources[@]}"; do
  [[ "$source" == /* && -e "$source" ]]
  [[ ! -L "$source" || ! -d "$source" ]]
  resolved=$(realpath -e -- "$source")
  case "$resolved" in
    /|"$HOME"|"$HOME/.claude"|"$HOME/.claude/"*)
      printf '%s\n' 'Refused: source is too broad or belongs to Claude Code.' >&2; exit 1 ;;
  esac
  case "$backup_parent/" in
    "$resolved/"*)
      printf '%s\n' 'Refused: backup destination is inside a source tree.' >&2; exit 1 ;;
  esac
  relative_sources+=("${source#/}")
done
work=$(mktemp -d "$backup_parent/charmer-cold.XXXXXXXX")
crypto_home=$work/gnupg
mkdir -m 700 -- "$crypto_home"
archive=$work/charmer.tar.gpg
"${TAR:-tar}" --create --numeric-owner --file - --directory / -- "${relative_sources[@]}" |
  gpg --no-options --homedir "$crypto_home" --no-symkey-cache \
      --pinentry-mode loopback --symmetric --cipher-algo AES256 --output "$archive"
chmod 600 -- "$archive"
gpg --no-options --homedir "$crypto_home" --no-symkey-cache \
    --pinentry-mode loopback --decrypt "$archive" |
  "${TAR:-tar}" --list --file - > /dev/null
```

Both pipelines must succeed; a partial file is not a backup. The second checks
decryption and tar readability, not application recovery. Check the archive's
0600 mode and keep the private path inventory, release identity and service
enablement choice with the encrypted backup. Do not publish archive listings,
credential contents or raw logs. A backup made before a later token rotation
may require fresh sign-in when restored.

## 5. Restore into an empty target, then verify

Prefer an **empty/disposable target** first. Same-machine recovery needs the
same HOME path, platform, installation channel and root selections. Install the
**same version**, or one explicitly documented to read the saved state and
configuration; never lower `state-version` or edit a record to make an older
release accept it. A newer state format or uncertain compatibility means stop
and obtain a compatible release. Do not restore over any running installation.

Use the authenticated same-version release ([release verification](../security.md))
and its installer with `--no-setup` if reinstalling programs; keep service
activation off. Each download belongs in a new empty directory. Alternatively,
restore the archived `install/` for the same platform and verify it against the
trusted signed release before use. Reinstalling `install/` or `claude/` is an
alternative to restoring those program trees, not an overlay with mixed versions.

Decrypt **your own backup** to a private temporary archive, not `/` or the
live home. GnuPG can emit plaintext before it discovers an integrity failure:
**do not pipe decryption into extraction**. Set `restore_parent` to an existing
private 0700 directory on encrypted storage, outside every destination. In a
new Bash terminal, set `archive` to the encrypted file and `crypto_home` to a
new empty 0700 GnuPG home, as above. Each restore block sets its own shell
safety options; run it as a block and stop if it fails.

<!-- example: backup-unpack -->
```bash
set -euo pipefail
umask 077
: "${restore_parent:?Choose an existing private restore directory}"
: "${archive:?Choose your encrypted backup}"
: "${crypto_home:?Choose the new isolated GnuPG home}"
unset restore_root
[[ "$restore_parent" == /* && -d "$restore_parent" && ! -L "$restore_parent" ]] || {
  printf '%s\n' 'Refused: restore parent must be a real absolute directory.' >&2; exit 1;
}
[[ -f "$archive" && -d "$crypto_home" ]] || {
  printf '%s\n' 'Refused: encrypted archive or isolated GnuPG home is missing.' >&2; exit 1;
}
restore_work=$(mktemp -d "$restore_parent/charmer-restore.XXXXXXXX") || exit 1
plaintext=$restore_work/charmer.tar
if ! gpg --no-options --homedir "$crypto_home" --no-symkey-cache \
    --pinentry-mode loopback --output "$plaintext" --decrypt "$archive"; then
  printf '%s\n' 'Decryption/integrity check failed: do not extract or copy this workspace.' >&2
  exit 1
fi
chmod 600 -- "$plaintext" || exit 1
extract_root=$restore_work/files
mkdir -m 700 -- "$extract_root" || exit 1
if ! "${TAR:-tar}" --extract --preserve-permissions --no-same-owner \
    --file "$plaintext" --directory "$extract_root"; then
  printf '%s\n' 'Extraction failed: do not copy from this workspace.' >&2
  exit 1
fi
restore_root=$extract_root
```

Extraction starts only after GnuPG returns success. `restore_work` is private
0700 staging; its temporary plaintext archive is 0600 and contains secrets.
On decryption failure, a partial plaintext file may remain there, but no
extraction is attempted. Protect failed workspaces too and retire them only
under your private-data retention policy. Never continue or copy from a failed
workspace: a directory, file or marker inside it is not proof of authentication.
`restore_root` is assigned only after authentication and extraction succeed.

Keep scripts and tools outside the extracted tree. Do not execute its programs,
source files or run an interpreter from there. Review its **path and mode
metadata only** against your saved inventory, including symlink targets. Never
extract an unknown archive into a production home. Numeric owners were recorded
for reference; restore as your normal user, not root (`--no-same-owner`).

Restore configuration and external keys, then data, then state, at their original
absolute locations. If a destination already exists (even an installer's newly
created scaffold), move that exact root aside to a private sibling on the same
filesystem first; retain it. Do not merge roots, overwrite evidence or use
recursive removal commands. Only after the entire unpack block succeeded and
you reviewed the inventory, use this checked copy for each inventoried root.
Keep `restore_root` from that successful step and set `destination` to the
original absolute path; its parent must be a real, private directory you own.
Every failed precondition explicitly aborts before `cp`; do not skip a refusal.

<!-- example: backup-copy -->
```bash
set -euo pipefail
umask 077
: "${restore_root:?Complete the authenticated unpack step first}"
: "${destination:?Choose one inventoried absolute destination}"
[[ "$restore_root" == /* && -d "$restore_root" && ! -L "$restore_root" ]] || {
  printf '%s\n' 'Refused: the successful restore staging directory is missing.' >&2; exit 1;
}
[[ "$destination" == /* && "$destination" != / && "$destination" != "$HOME" ]] || {
  printf '%s\n' 'Refused: choose one inventoried absolute destination, not a whole home.' >&2; exit 1;
}
[[ ! -e "$destination" && ! -L "$destination" ]] || {
  printf '%s\n' 'Refused: destination already exists; keep it and move that root aside first.' >&2; exit 1;
}
saved=$restore_root/${destination#/}
[[ -e "$saved" || -L "$saved" ]] || {
  printf '%s\n' 'Refused: inventoried source is missing from restore staging.' >&2; exit 1;
}
cp -a -- "$saved" "$destination" || exit 1
```

Preserve **original modes, including executable bits** on launchers, installed
programs and state helpers. Do not recursively chmod files to 0600: that makes
programs unusable. Private configuration/state/data roots and credential
folders must remain 0700; credential and record files remain 0600. Do not
chmod symlinks or apply blanket mode changes to installed program trees.
Restore launchers from the inventory or regenerate them with the same signed
installer. Preserve its receipt, but do not replace whole shell profiles with
backup copies; re-establish PATH through the installer if necessary.

Keep the supervised unit and enable link in staging until roots and programs
are complete. Use the supported service installation flow to regenerate the
unit and enablement, not a blind copy of host-specific service state. When a
restored process stamp, PID, listener, ownership or channel cannot be proved,
stop at the guard. Use `charmer gateway status`, `charmer doctor --json` and the
reported [guarded remedies](gateway.md#when-gateway-changes-are-paused).
An interrupted installer uses `sh install.sh --repair`; an interrupted service
hand-off uses `charmer gateway service install`. An unreadable owner record is
not permission to take over. Never hand-edit or delete stale exec stamps,
locks, inhibition or failed-save evidence, and never clear a failed save just
to start the restored gateway. Missing old journal evidence can still hold;
recovery does not manufacture it.

Before resuming sessions, if the restored release supports it, use
`charmer update --verify --from-dir <dir>` with that exact release's downloaded
signed metadata ([verification and limits](../update.md#verify-an-installation));
without `--from-dir` verification discloses and fetches that exact version.
Older releases without `--verify` cannot perform this check. Check
`charmer doctor` after the supported gateway/service start. A **Ready**
health report is not a signature or full restore test. Require both installation
identity verification and healthy recovery; account credentials may need fresh
sign-in. A session without its separately retained Claude Code transcript
cannot be resumed from charmer metadata alone. Keep the encrypted backup and
moved-aside roots until recovery has actually been accepted.

## 6. Do not clone rotating sign-ins

Never run a restored OAuth sign-in on two hosts at once, including during a
rehearsal. Both copies can rotate the same refresh token and invalidate one
another (`invalid_grant`). A replacement-machine move uses export/import and
fresh sign-in, not this same-home restore example. If the old host is lost or
has used its credentials since the backup, fresh sign-in may still be required.
Do not test a real account by starting a restored clone.

## 7. Keep rollback and recovery separate

`charmer update --rollback` selects a retained compatible release; it does not
restore deleted roots, a lost home or conversation history. A cold recovery
restores the saved point in time, with its compatibility and credential limits.
Test your procedure with synthetic credentials in an isolated disposable home
before relying on it. Archive/encryption and mode checks alone are not a
production backup or full restore acceptance.

When finished with either example, stop only its isolated encryption agent.
Keep the encrypted archive; retire temporary plaintext restore work only under
your own retention policy, never by deleting the original roots.

<!-- example: backup-crypto-close -->
```bash
set -euo pipefail
: "${crypto_home:?Choose only the isolated GnuPG home created above}"
gpgconf --homedir "$crypto_home" --kill gpg-agent
```
