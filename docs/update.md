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

### Updating from 2.0.1

The 2.0.1 updater switches the installation links, but the **old gateway
continues running**. An installed new version does not prove that its gateway
is running. Between turns, in a terminal outside Claude Code, update first,
then explicitly restart through the guarded command and inspect the result:

```bash
charmer update
charmer gateway restart
charmer gateway status
charmer update --verify
charmer doctor
```

Run the next step only after assessing the previous result. If restart is
held, keep the gateway running and follow [the persistence hold](guides/gateway.md#the-persistence-hold);
never bypass a held stop with a service-manager stop or a signal. Verification
cannot restart or repair it for you. On macOS, running-byte verification is
unavailable (exit 3); status and doctor remain separate health checks, not a
substitute for a running-byte match.

**macOS 13 or newer is required by this release.** Check your OS before
updating: the old updater does not guarantee refusal on an older macOS.

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

A newer charmer release may pin a newer Claude Code. `update` puts
that copy in place before it switches, so a launch never finds a missing
client. Your own Claude Code installation is untouched.

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
