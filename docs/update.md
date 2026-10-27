# Update charmer

## A release bundle

```bash
charmer update            # check, show the plan, ask, install
charmer update --check    # only check whether a newer release exists
charmer update --from-dir <path>   # use release files you downloaded
charmer update --rollback # switch back to the previous version
```

On the launch card, **U** offers **Newer release** (the same update steps)
and the separate **Verify installed** action. **H** also has an explicit
**Verify this installation** row. Neither opening H nor plain doctor
fetches release metadata. The card's release row is local information,
not a background check for a newer version: it shows
the installed version and release date and says that U checks for a newer
release. At 30 days it shows the release's age and suggests
`charmer update --check`.

`update` downloads the latest release's signed checksums and verifies
their signature with the release key this installation carries (it never
falls back to unsigned checksums). The same version is "up to date". A
newer one shows `Update plan: <installed-version> -> <new-version>` first:

- the bundle name, download size and signed-checksum verification;
- installation next to the current release, keeping it for rollback;
- the Claude Code pin and state format, only when they change: a matching
  client is copied from this computer or downloaded and verified before
  the switch; a state-format upgrade on first run prevents rollback to
  the old format;
- whether the gateway changes and needs a restart, or only the launcher
  changes and new sessions use it while the running gateway keeps running;
  for a gateway that changes, the way back: “if the new gateway does not
  start: charmer update --rollback”.

It asks before it installs; without a terminal it needs `--yes`, and
inside a Claude Code session it refuses. Exit statuses: 0 done or nothing
to do, 1 refused or failed, 2 usage, 3 declined, 130 cancelled. Doctor and
the card show how old the installed release is.

A running gateway keeps the version it started from until you restart it
between turns (`charmer gateway restart`); while its persistence hold
is active, a switch that changes the gateway waits.

### Updating from 2.2.1

charmer 2.3.0 moves the gateway to CLIProxyAPI 8.0.45 with 27 local patches
and Claude Code from 2.1.310 to 2.1.311. Catalog 42 ships with this release;
Go 1.27.3 and the bundled CPython 3.15.2 stay the same. Updating switches
the installation, not the running gateway. Between turns, in a plain
terminal outside Claude Code, run each step after assessing the previous
result:

```bash
charmer update
charmer --version
charmer gateway restart
charmer gateway status
charmer update --verify
charmer doctor
```

The restart checks ownership and the persistence hold before stopping. If
an update or restart is held, keep the gateway running and follow
[the persistence hold](guides/gateway.md#the-persistence-hold). Never bypass
a held stop with a service-manager stop or a signal. Verification cannot
restart or repair the gateway for you.

On Linux, check for `running-gateway: current-match` after the restart:
the opened running executable matches the authenticated installed gateway.
If installed bytes match but the running build differs or mismatches,
restart between turns as the summary says. For `running-gateway: unavailable`,
check `charmer gateway status` and retry once the gateway runs. On macOS,
running-byte verification reports unavailable (exit 3), not a match.
Status and doctor remain separate readiness checks, not a substitute for
running-byte verification.

Gateway status, doctor and the gateway view compare the recorded and
installed gateway files by SHA-256 when their file identities differ.
Matching bytes at another path make switching paths optional: the finding
says `gateway same binary on another path; restart optional`. Changed,
missing or unreadable files still leave a restart pending. This on-disk
comparison does not verify running bytes or remove running-path retention
protection. Other restart conditions and persistence holds still apply.

The update installs charmer's verified **Claude Code 2.1.311** copy before
switching the release: it copies a matching local file or downloads and
verifies the pin when none is available. Your own Claude Code installation
is untouched. A session already running keeps its client until its next
launch or resume (`/exit`, then `charmer -r <id>`). State and record formats
remain 4; no record migration is needed.

#### Models you added that now ship

If you added Haiku 5.5 on Anthropic, Muse Wonder 1.0 on Meta, or
DeepSeek V4.1 Flash 1017 or Kimi K3.1 Pro on OpenRouter yourself, your key
now resolves to the shipped line (`haiku`, `muse`, `or-deepseek` or
`or-kimi`). Profiles keep working;
running sessions keep their captured routes until resume. Other models you
added are unchanged. Remove the duplicate declaration with the successor
that the message names; this rewrites the profiles and named bindings
that use your key:

```bash
charmer models rm <line> --successor <line>
```

The first `<line>` is your key; the successor is the shipped key. The
command asks before changing anything. If a running session still uses your
line, removal refuses: exit that session first and follow the named remedy.
These are the messages to expect. `KEY` is your key, `P` the provider,
`W` the wire id and `S` the shipped line. The listing example uses the wire
id as its display name; a display name you chose is kept. Its columns are separated by tabs (shown as `\t` below).

| Surface | Text |
| --- | --- |
| `charmer providers validate` (exit 0) | `providers.d/P.json: lines.KEY: P/W is now provided by the catalog line "S" — yours yields to it; remove it: charmer models rm KEY --successor S` |
| `charmer doctor` (Attention) | `operator line KEY (P/W) is now provided by the catalog as S: yours yields to it — remove it: charmer models rm KEY --successor S` |
| `charmer models` | `KEY\tW\tnow provided by the catalog as S\tremove yours: charmer models rm KEY --successor S` |
| `charmer models show <line>` (exit 0) | `operator line KEY (P/W) is now provided by the catalog as S: yours yields to it — remove it: charmer models rm KEY --successor S` |

For a bound line, doctor adds, for example,
`; used by profile mine cm-explorer: they move to S at their next launch or resume`.
An admitted line adds ` (it was admitted)` before that semicolon.

Trying to add a model the release already ships writes nothing (exit 1).
For a new Haiku key, the refusal is:

```text
charmer: models add: anthropic/claude-haiku-5-5 ships with charmer as the line haiku — nothing written; bind it in a profile (charmer profile edit NAME) or see charmer models
```

If the earlier Kimi declaration is still present, it instead names its
removal:

```text
charmer: models add: openrouter/moonshotai/kimi-k3.1-pro ships with charmer as the line or-kimi — nothing written; yours (custom-moonshotai-kimi-k3-1-pro) yields to it — remove it: charmer models rm custom-moonshotai-kimi-k3-1-pro --successor or-kimi
```

Aliases left by models you removed may still be reported as
`alias <alias> (captured for KEY) has no line — prune when unused: charmer doctor --prune-aliases`.
Once no live session uses them, name the aliases to remove:

```bash
charmer doctor --prune-aliases <alias>…
```

Without names, the option prunes all eligible unused aliases. It keeps
captures of current model declarations; naming an alias still used by a
live session refuses the whole operation.

#### Settings at the next resume

Managed sessions launched before the update, running or stopped, pick up
two new compiled settings at their next resume: `ANTHROPIC_DEFAULT_HAIKU_MODEL` names the bound
Haiku model, otherwise the lead, and `DISABLE_AUTO_COMPACT="0"` re-enables
managed compaction. They do not change your own settings or plain `claude`.
Until then, doctor says
`fence/picker differ from the installed catalog (catalog changed since launch); applies at the next resume`.
Resume with `charmer -r <id>` after exiting the session.

A later live Haiku bind or unbind has its own reason:
`session <id8>: background requests move to claude-haiku-5-5[1m] at the next resume`
(or the lead's selector after unbinding). Reloading plugins does not change
this launch-time setting. A user `DISABLE_COMPACT` still needs removal;
see [compaction troubleshooting](troubleshooting.md#inside-a-session).

#### Before going back to 2.2.1

If 2.2.1 is the retained previous release, `charmer update --rollback`
returns to it. Prepare first, between turns:

- Rebind profile roles that use `haiku`, or `or-kimi` at low or max; 2.2.1
  has neither. Its doctor reports such a profile as BLOCKED, for example
  `profile mine: agents.cm-explorer.model: unknown model 'haiku'` or
  `profile mine: agents.cm-implementer.effort: 'max' is not supported by 'or-kimi' (available: high, xhigh)`.
  Profiles that `charmer models rm <line> --successor haiku` rewrote are
  among them.
- Remove each model you added on OpenRouter with the low, medium or max
  contract (`charmer models rm <line>`), or keep only high and xhigh
  (`charmer models edit <line>`). 2.2.1 rejects those contracts
  (`proxy effort contract 'output-config-low' is not declared by provider 'openrouter'`),
  and while such a line exists `charmer providers apply` and adding a model
  refuse with `operator layer invalid — config not written`. After the
  rollback, `charmer models rm <line>` refuses while another such line remains.
- Exit sessions that use `haiku`, `muse`, `or-deepseek` or `or-kimi`: 2.2.1
  does not serve the selectors 2.3.0 compiled for them. At their next resume
  `muse`, `or-deepseek` and `or-kimi` resolve to 2.2.1's Muse Spark 1.3,
  DeepSeek V4.1 Flash and Kimi K3, and a `haiku` role is unbound.

After the rollback the running gateway is still 2.3.0's. Run the guarded
`charmer gateway restart` between turns, then `charmer update --verify`, and
do not postpone the restart: 2.2.1's configuration does not turn off the
2.3.0 gateway's response compression.

Sessions 2.3.0 compiled show 2.2.1's Attention
`session <id8>: fence/picker differ from the installed catalog (catalog changed since launch); applies at the next resume`
until their next resume (`charmer -r <id>`); `charmer doctor --repair-all`
does not clear it. A session whose lineup binds `haiku` also shows
`session <id8>: the installed catalog no longer evaluates the recorded lineup (agents.cm-explorer.model: unknown model 'haiku'); it applies at the next resume (charmer -r <id>)`.

### Updating from 2.2.0

Follow the [same update sequence](#updating-from-221). In addition to the
client and gateway changes, the bundled Python moves to CPython 3.15.2.
The running gateway still needs the guarded restart.

### Updating from 2.1.0

Follow the [same update sequence](#updating-from-221), then check the
session settings below.

Existing managed sessions get charmer's subagent status line at their next
resume. `charmer doctor --repair-all` recompiles stopped sessions now;
running sessions can wait for their next resume. Doctor reports this as
Attention, not damage. A user, project or local `subagentStatusLine` setting
wins, and an unreadable settings layer also makes charmer compile none.
Doctor says which applies. To use charmer's rows instead of your own,
remove your `subagentStatusLine` setting and resume; charmer never changes
your main `statusLine`. See
[the agent panel](guides/sessions.md#the-agent-panels-rows).

After `charmer update --rollback` returns to **2.1.0**, its plain doctor
reports BLOCKED “scope differs” for sessions carrying the subagent status
line 2.2.0 and later compile. Run `charmer doctor --repair-all` for stopped sessions,
or resume one to recompile it. This removes the new compiled key; it does
not change your own status-line settings or the conversation.

### Updating from 2.0.1

The 2.0.1 updater switches the installation links, but the **old gateway
continues running**. An installed new version does not prove that its gateway
is running. Between turns, in a terminal outside Claude Code, update first,
then explicitly restart through the guarded command and inspect the result:

```bash
charmer update
charmer --version
charmer gateway restart
charmer gateway status
charmer update --verify
charmer doctor
```

Run the next step only after assessing the previous result. A long-running
gateway may hold the update or restart with "coverage truncated". Keep it
running, check the credentials with `charmer providers list`, then run
`charmer gateway clear-hold` in a plain terminal outside Claude Code and
retry the held step. For a failed save or any other hold reason, follow
[the persistence hold](guides/gateway.md#the-persistence-hold); never bypass
a held stop with a service-manager stop or a signal. Verification
cannot restart or repair it for you. On macOS, running-byte verification is
unavailable (exit 3); status and doctor remain separate health checks, not a
substitute for a running-byte match.

**macOS 13 is the minimum the new installer enforces.** This is not a
runtime support claim for macOS 13 or 14; the native Apple silicon journey
runs on macOS 15. On macOS 12, stay on 2.0.1 until upgrading macOS. Check
your OS before updating: the old updater does not guarantee refusal on an
older macOS.
If you updated on macOS 12 anyway, use this release's installer and run
`sh install.sh --rollback` in a plain terminal to return to the retained
previous release. Installer rollback does not apply the new-install macOS
check, but it runs its checks with the newly installed release's runtime,
which has no macOS 12 evidence; if that runtime cannot start, the rollback
stops and changes nothing. Its usual safety checks and persistence hold
still apply.
See [macOS requirements](install/macos.md#architectures-and-evidence).

## Verify an installation

```bash
charmer update --verify                 # installed exact version, not latest
charmer update --verify --json          # one JSON result; disclosure on stderr
charmer update --verify --from-dir <dir> # fully offline, downloaded metadata
```

This is a read-only integrity snapshot, not an update or health test. Online
verification discloses the release host and installed exact version before
fetching only `SHA256SUMS`, `SHA256SUMS.sshsig` and the outer release
`MANIFEST.json`. The canonical GitHub origin and exact-version download path
come from installed code, **not a URL in the manifest being checked**. GitHub's
release-asset hosts may serve a redirected download; redirects to latest,
another release page or another host are refused. Custom or test release
locations use `--from-dir` for verification; the updater's `--base-url` option
is unchanged. Verification never asks for latest, contacts a provider or
probes the gateway over TCP. The TUI asks before that fetch, with **Cancel** focused.
Choose **Downloaded files** there to enter a directory instead: just like
`--from-dir`, this makes no network attempt or transport. That directory
must contain those three files from the installed exact release; it cannot
supply replacement trust keys. Archives are not downloaded for verification.
Wrong-version, malformed, oversized or unreadable metadata makes verification
incomplete (exit 3), not evidence that the installation is damaged. Obtain the
three files for the installed release and retry with `--from-dir`. A failed
signature or signed checksum is an authentication failure (exit 1): do not use
those metadata files.

The authentication chain is:

1. The **installed trust** verifies the signature on `SHA256SUMS`.
2. Those signed sums authenticate the **outer release manifest**.
3. The selected target's `manifest_sha256` authenticates the **installed
   inner bundle manifest**. These two manifests are different files.
4. The canonical `bundle-content-v1` hash and regular-file count are
   recomputed for the completed `versions/<version>` tree and compared
   with the authenticated content identity.
5. The gateway and the separately stored managed client are checked against
   their authenticated identities, including executable mode and size.

The **only excluded path inside the version tree is `MANIFEST.json`**, to
avoid a digest cycle. The installer adds no files there after extraction.
There are no wildcard cache exclusions: missing or extra files, including
an unexpected `__pycache__`, count as changes. Receipts, locks, staging,
`current`/`previous` links, stable outer launchers/shims and managed-client
storage are outside that tree, not exclusions from it. The managed client
gets its own check; outer launcher receipts and shims are not authenticated
by the bundle-content hash. An aggregate content mismatch cannot locate the
individual changed files.

On **Linux only**, verification hashes an opened `/proc/PID/exe` of the
observed owned gateway, then rechecks process identity. It never hashes the
stamp's pathname as a substitute for running bytes. The component statuses
stay distinct:

| Running gateway | Meaning |
| --- | --- |
| `current-match` | opened running bytes match the authenticated installed gateway |
| `different-running-build` | bytes match a retained bundle's local identity, not the authenticated installed build; that retained comparison is not signed evidence |
| `stopped` | no running executable to verify; **not ready**, even when installed files verify |
| `unavailable` | ownership, process identity or bytes cannot be proved, or the platform is not Linux |
| `mismatch` | running bytes differ from the authenticated gateway |

The optional `previous` bundle supplies only a local comparison, including
when it is a development build. A missing or unreadable `previous` link does
not prevent verification of the selected installation. If installed bytes
verify and **only the running gateway differs**, restart between turns with
`charmer gateway restart` (it follows the persistence hold); reinstalling or
rolling back is not needed for that difference alone.

CLI text, JSON and the TUI show the same component statuses. Verification's
exit contract is mode-specific: **0** complete (`verified`), **1** mismatch
(including a different running build), **3** incomplete or unsupported;
**2** invalid arguments, **130** interrupted. A proven stopped gateway can
have verified installed files (0); that is never a readiness claim. Use
`charmer gateway status` and `charmer doctor` separately. JSON contains
`status`, `exit_code`, `version`, `target` and `checks`, never credentials or
absolute local paths.

`--verify`, `--check` and `--rollback` are mutually exclusive. `--json`
requires `--verify`; verification refuses `--yes` and `--base-url`.
`--check` also refuses `--yes`; rollback takes neither download-source flag.
Choose `--from-dir` or `--base-url`, not both, for an update or check.

Verification does not refresh shims, create stores, acquire a client,
read gateway tokens, repair, recover an interrupted update or restart.
Source and Nix installations report unsupported (3), never signed-bundle
success; verify the checkout or use `nix store verify` instead. This check
uses installed code and trust, not independent attestation of a hostile
host or an atomic filesystem snapshot. Observed concurrent changes leave
verification incomplete; retry after the installation is stable.

## An installation from before the rename

charmer was renamed in 2.0. An installation made under its earlier name
is not updated in place: `charmer migrate from-legacy` moves it onto charmer
once, and `--back` returns it ([the migration guide](guides/legacy-migration.md)).

## What happens to old files

Updates keep `current` and `previous` under
`~/.local/share/charmer/install/` and remove other versions (except
one a running gateway still executes), and they remove Claude Code copies
no installed release needs; a copy a running session uses is kept and
reported.

The 2.2.0 updater and installer flush incoming contents before replacing an
installed version and persist the final version entries before switching
`current` and `previous`. The installer does this for same-version
reinstalls too. Rollback
reuses the retained version without this incoming-bundle step. The updater
of an older release still uses that older release's update code.

When this installer is used with an older bundle that lacks the durability
helper, it prints a notice and uses system `sync`; away from Linux, that
fallback does not give the helper's per-file guarantee. If such a bundle
cannot safely reclaim a stale install lock, confirm no installer or updater
is running, then follow the printed removal command for only the named
lock. Never remove `.lock-guard`, and never run an older release's
stale-lock reclamation alongside another installer or updater.

While an update or the installer works, gateway changes are paused (see
[the gateway guide](guides/gateway.md#when-gateway-changes-are-paused)).
If one is interrupted after it started changing the installation, the
next `charmer update` finishes it before doing anything else; an
interrupted install or installer rollback is finished by
`sh install.sh --repair`. A switch stopped between `current` and
`previous` is put back as it was.

## Rolling back

`charmer update --rollback` (and `sh install.sh --rollback`) switch
back to the previous version. They are command-line only, because they
replace the program the launcher runs. A rollback goes only to the
version it showed you: if the installation changed while it waited for
your answer, it refuses. Afterwards `previous` is the version you left,
so a second rollback undoes the first.
`charmer restore-2x` restores pre-public record-format backups and is
unrelated to release rollback.

When the selected older release supports `update --verify`, it verifies
that exact older version, not the newest release. **After rollback to
2.0.1 the `--verify` flag does not exist**: use its supported status and
doctor commands, and do not describe their result as signed verification.
A still-running gateway may also need an explicit guarded restart after
rollback; keep the persistence hold in force.

When the gateway does not start after an update that changed it (`gateway
start`, `gateway restart`, a launch, or the TUI's start and restart), the
failure adds a second remedy: “if this began after charmer update: charmer
update --rollback, then charmer gateway start (returns to <version>'s
gateway; sessions keep their records)”; doctor's Gateway finding and the
TUI's gateway dialog show it too while the gateway is stopped. It appears
only for an installed release whose previous version has another gateway
and still reads the state on disk, never on the Nix or source channels.

## State only moves forward

A newer release may upgrade the format of charmer's state. After
that, an older version refuses to change the state and names the fix
(run the newer version), and a rollback that would go behind the state
format is refused. Rollbacks restore the program, never the state.

## Claude Code with an update

When the incoming release changes the Claude Code pin, `update` puts the
verified matching managed copy in place before switching. An unchanged pin
is not reinstalled by update; use `charmer setup --step claude` if that copy
is missing or damaged. Rollback does not acquire a client either. Your own
Claude Code installation is untouched.

## The Nix package

A Nix installation updates through Nix (your flake or profile);
`charmer update` and **U** say so instead of installing anything.
The card says “updated through your Nix flake”. The state rule above
applies to the Nix package too.

## Offline and behind a proxy

`charmer update` and the card's **U** use the invoking process's
`https_proxy` / `HTTPS_PROXY` and `no_proxy` / `NO_PROXY` environment
variables, not the gateway's proxy setting. Lowercase wins, including an
empty lowercase override. See [guides/networking.md](guides/networking.md)
for proxy and certificate configuration and its limits.

`--from-dir <path>` installs from release files you downloaded yourself
(the signature is still checked), and remains available offline. How old
the installed release is (on the card and in doctor) is the release's date,
also when you are offline.
