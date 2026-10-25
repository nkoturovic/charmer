# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions
follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Upgrade notes

The update installs the verified Claude Code 2.1.310 copy before switching
releases; your own Claude Code installation is untouched. Running sessions
use the new copy at their next launch or resume. State and record formats
remain 4.

Existing managed sessions pick up charmer's subagent status line at their
next resume, or through `charmer doctor --repair-all` for stopped sessions.
Your own `subagentStatusLine` setting wins; charmer also leaves it unset when
it cannot read a settings layer. Doctor explains which line new sessions
will use. After rolling back to 2.1.0, its doctor reports BLOCKED
"scope differs" for sessions with the new compiled key until
`charmer doctor --repair-all` or a resume rebuilds their scopes.
See [updating from 2.1.0](docs/update.md#updating-from-210).

### Added

- Managed sessions show subagent type, requested model, effort, context and
  task in Claude Code's agent panel. Your main status line is unchanged;
  `charmer statusline main` is available as an opt-in renderer.
- Doctor reports which subagent status line new sessions get, including a
  user-owned setting or a settings file it cannot read.

### Changed

- Managed sessions use Claude Code 2.1.310 and catalog 41. Every client
  platform build is checked against Anthropic's signed manifest. Behind
  the gateway, some built-in and MCP tools are listed by name and fetched
  with ToolSearch when needed.
- Release bundles run CPython 3.15.1 from python-build-standalone 20261021.
  The Nix flake uses a refreshed nixos-unstable lock.
- Running setup when everything is already configured avoids repeated
  client checks; every launch still verifies the client's full hash.
- **V** opens Providers details, **B** opens named bindings in the profile
  editor, and **W** opens gateway actions from Findings, using the same
  dialog as Providers and Direct. The old **Q**, **N** and **G** keys remain
  accepted for compatibility on their respective screens.
- The Sessions keybar uses both available rows, puts row actions first and
  keeps help and exit visible. The marked count appears only when nonzero.
- Launch cards put the review-independence verdict before its routing;
  direct cards omit the review and provider-spread rows.
- Models, Settings and text views show `↓ N more` for entries below the
  visible area; text views count wrapped lines.
- Development and tests use refreshed build and CI pins and improve
  developer promotion (file modes and rollback), lock and resource cleanup,
  draft/build failures, probe synchronization and timeout cleanup, licence
  reporting, pin checks, required tests, hygiene and fixture isolation;
  Nix uses the shared guarded runner and final failure summaries remain
  visible when a test replaces stderr, with
  [test-isolation limits](AGENTS.md#4-development-workflow) unchanged.
- Help distinguishes `charmer migrate` and `charmer restore-2x` support
  for pre-public record formats from `charmer update --rollback`. These
  commands remain available; no retirement is scheduled.

### Security

- Provider listings hide credential-bearing file names and model keys
  from rejected secret-bearing declarations. Model edit/removal commands
  cannot resolve those declarations as write targets, and qualification
  refusals use the same redacted diagnostics.
- Terminal control characters in session relink success lines are escaped.
- Isolated checks on Claude Code 2.1.310 again observe the synthetic gateway
  key only at the gateway in the tested request paths. Requests that bypass
  the test proxy were not observed.
- The client pin includes upstream sandbox-boundary, worktree and
  Bash-permission fixes from 2.1.304–2.1.309. charmer has not tested those
  fixes and does not replace the client's sandbox or worktree protections.
- The Nix lock selects OpenSSL 3.5.9, including the fix for CVE-2026-84782.

### Fixed

- Cancel is first and focused when discarding profile edits or deleting
  a named binding. Gateway dialogs offer stop advice only for a gateway
  known to be running or starting.
- Fresh launch cards point quota warnings to **P → F** fallback profiles,
  keeping observation age and known reset times when they fit; resume cards
  keep their Sessions → Lineup remedy.
- Saved-choice confirmations say "Save the choice for N session(s)?",
  OpenRouter help wraps to the dialog width, and first-run **H** is labelled
  "checks" while nothing is connected.
- Sessions stays open with a warning when resume-time relinking fails.
- Doctor detects unexpected scope files and directories and incorrect
  ownership or permissions even when compiled contents match. A scope that
  becomes unreadable during its check reports a repair command instead of
  crashing.
- charmer's settings and managed-policy reads no longer hang on FIFOs or
  other special files. Regular-file symlinks and the 1 MiB limits are kept.
- Change previews treat incomplete published gateway configurations and
  malformed route sections as unknown, not empty, retaining their
  destructive-change checks. Doctor skips an uncheckable pending-change
  comparison instead of crashing.
- Managed subagent status lines stay silent when their launcher is killed
  or their stderr setup fails.
- After an HTTP MCP connection closes mid-call, Claude Code retries only
  tools the server marks read-only or idempotent. A state-changing tool
  marked idempotent can still run twice; check the external system after
  an interrupted call before asking again.
- An inherited setting can no longer narrow managed sessions' retry
  watchdog to capacity errors only.
- Session listings report an incomplete inventory instead of silently
  omitting unreadable records. Plain output keeps readable rows and exits
  1; JSON reports unavailable. Whether a session is running is independent
  of whether it needs attention.
- Relinking a session reports an unfinished scope repair, keeps the saved
  identity change and names the doctor command to finish it.
- Explicit non-UTF-8 output encodings no longer crash help or listings,
  even with Python's UTF-8 mode enabled. Unsupported characters are
  replaced; use UTF-8 for lossless JSON text.
- Declining token rotation, starter-profile creation or an import returns
  exit 3. A profile editor that fails to start reports the error without
  a traceback, returns 1 and names the unsaved copy kept for recovery.
- Interrupted or truncated release downloads fail before switching the
  installation, without exposing response details; local write errors keep
  their own diagnostics. Launcher-ownership checks no longer hang on a
  FIFO or socket at a recorded path.
- Concurrent installers and updaters serialize stale-lock reclamation and
  remove only their own lock. Incoming bundles are flushed before replacing
  installed trees and before switching release links, including reinstalling
  the same version. An older bundle without the helper uses system `sync`
  with a notice.
- Service removal and failed-install cleanup recheck the gateway after its
  hold check. An incomplete restoration keeps gateway changes paused until
  files, endpoint, service state and required starts are restored. Missing
  or unusable state directories now name a recovery command.
- Gateway log pruning counts toward the start deadline. Invalid UTC dates
  in instance-log names hold changes and skip pruning instead of crashing.
- File replacement reports when new bytes were written but their durability
  is unconfirmed, rather than implying that nothing changed.
- JSON readers accept valid surrounding whitespace and reject oversized
  numbers, deep nesting, invalid Unicode and malformed registry shapes
  without tracebacks. A trailing newline no longer bypasses an anchored
  schema pattern. Invalid ledger, evidence and export-receipt data are
  reported without rewriting them. Session records that exceed parser
  limits remain forgettable.
- Profile reseed previews include lead-provider restriction changes.
  Blocked cleanup of old client copies now reports why nothing was removed.
- Gateway start preparation no longer hangs on special files or misses
  changes beyond a file's initial bytes.
- Provider listings, qualification, smoke checks and local management
  readings reject truncated declared-length responses.

### Known limits

- The [late-stream completion limit](docs/troubleshooting.md#late-stream-completion-limit)
  remains on Claude Code 2.1.310 with the tested CLIProxyAPI 8.0.33: a
  successful client result is not proof of complete stream framing.
- A user-installed `agent.spawn` mod can change an agent's model within
  the lineup; a `settings.read` mod can also widen the allowed model list.
  The compiled lineup does not constrain user-installed code.
- The client can resend an unrecognised provider refusal up to three times.
  On resume, a passed-through `--tools` option does not narrow the tools
  offered to the model, although calls outside it are still refused.
  See [client limits](docs/reference/compatibility.md#verified-behavior-and-limits).
- CPython 3.15.1 still carries a tar-filter advisory outside charmer's
  extraction paths. The Nix channel holds Python 3.14.8 and GnuPG 2.4.9;
  GnuPG is for development and tests, not bundle signature verification.
- Older releases retain their unguarded stale-lock reclamation: do not run
  it alongside another installer or updater. The older-bundle `sync`
  fallback away from Linux lacks the newer helper's per-file guarantee.

## [2.1.0] — 2026-10-24

### Upgrade notes

Updating switches the installation, not the running gateway. Between turns,
in a plain terminal outside Claude Code, run each step after checking the
previous result:

```bash
charmer update
charmer --version
charmer gateway restart
charmer gateway status
charmer update --verify
charmer doctor
```

A long-running gateway may hold the update or restart with "coverage
truncated". Keep it running, check the credentials with
`charmer providers list`, then run `charmer gateway clear-hold` in that
plain terminal and retry the held step. For a failed save or another hold
reason, follow [the persistence hold](docs/guides/gateway.md#the-persistence-hold).
Never bypass a hold with a service-manager stop or a signal. The guarded
restart also enables quota readings in an updated release bundle.

**macOS 13 is the minimum the installer enforces.** No runtime support is
claimed for macOS 13 or 14; the native Apple silicon release-bundle journey
runs on macOS 15. Intel has no support claim. On macOS 12, stay on 2.0.1
until upgrading macOS: the 2.0.1 updater does not enforce the new floor.
If you updated on macOS 12 anyway, try `sh install.sh --rollback` with this
release's installer to return to the retained previous release. Its checks
run with the installed 2.1.0 runtime, which has no macOS 12 evidence; if that
runtime cannot start, the rollback stops and changes nothing. Its safety
checks and persistence hold still apply.
Running-gateway byte verification is Linux-only; on macOS it reports
unavailable, not a match. See [updating](docs/update.md#updating-from-201).

### Added

- Doctor groups findings by area, puts a persistence hold first among
  Attention findings, and separates optional-evidence advisories from the
  verdict. Repeated findings are collapsed with counts; `doctor -v` keeps
  the full detail and JSON findings include their area.
- **H** on the launch card opens a findings list with guarded actions and
  confirmations. Housekeeping is offered only when session liveness and
  records can be checked safely; other rows show the exact remedy. Opening
  Sessions from a needs-choice finding keeps the launch's extra arguments
  and its way back.
- Session lists and bulk housekeeping accept combined directory, ended,
  needs-choice and age filters. Preview several sessions before forgetting
  them with one confirmation; live, unknown or changed records are refused,
  and transcripts stay untouched. Bulk selections refuse an unreadable
  sessions directory; a single-session `forget --dry-run` previews that
  same operation without writes, including its force option. In **S**, mark
  rows with **Space** or **\***, combine filters with **B**, and forget with **X**.
- `charmer sessions choose` and **Q** in Sessions save a profile or direct
  lead for sessions that need a choice, without launching. A valid saved
  choice clears the actionable warning and applies at the next resume.
- `charmer update --verify` checks the installed release against signed
  exact-version metadata, its files, the managed client and, on Linux, the
  running gateway's bytes. `--from-dir` works fully offline. The launch
  card's **U** and the findings list **H** offer the same explicit check;
  neither plain doctor nor opening H fetches release metadata. A missing or
  unreadable optional previous release does not prevent verification;
  unusable supplied metadata is incomplete, distinct from a signature
  failure. When only the running gateway differs, the remedy is a guarded
  restart rather than reinstalling.
- If the gateway fails to start after an update, its failure and doctor
  name `charmer update --rollback`, then `charmer gateway start`, when the
  retained release is a compatible rollback with a different gateway.
- Adding an OpenRouter model suggests its vendor family from the model's
  namespace, so review routing can tell families apart. The suggestion is
  editable, `--family` wins, unknown, router and stealth namespaces stay
  unknown, and existing lines are never rewritten.
- A task-first quickstart and an encrypted cold-backup and restore guide
  cover first launch and recovery without deleting transcripts.

### Changed

- The full-screen interface now requires verifiable eight-bit terminal
  settings. Unsupported terminals get a notice; the launch/resume line
  flow remains available.
- Managed sessions use Claude Code 2.1.302, catalog 40, and CLIProxyAPI
  8.0.33 built with Go 1.27.3. Every client platform build is checked against
  Anthropic's signed manifest. charmer's compiled permission default is
  unchanged.
- Release bundles run CPython 3.15.0, with the GIL enabled and the
  experimental JIT off. The Nix flake follows nixos-unstable of 2026-10-15.
- Release bundles now provide the same passive account-quota readings as
  the Nix package in `charmer quota`, `/cm quota` and the Providers screen.
  After updating, use the guarded `charmer gateway restart`; an update
  alone does not enable readings in the running gateway. Disabled readings
  stay disabled. Source checkouts report quota as unavailable; a missing
  reading never means zero use or a remaining balance.
- On the OpenAI API key, Claude Code's per-request output cap reaches
  OpenAI as the request's output limit, streaming or not. A cap below
  OpenAI's minimum of 16 tokens is raised to 16, so `/model` validation
  keeps working. On the ChatGPT account the model's own output limit still
  applies.
- Managed terminal sessions may run a subagent in the foreground;
  background remains the default.
- Claude Code's own `--autocompact` flag after `--` is refused: set the
  session's window with `charmer window-ceiling` instead.
- The launch card shows gateway holds, describes **U** as checking for an
  update and opens Get started on Enter when nothing is connected.
  **L** launches anyway. Gateway actions offer no restart while held;
  otherwise a restart first lists known running sessions and asks.
- Scopes compiled under the earlier product name appear as one doctor
  finding. Repair recompiles stopped sessions; running sessions pick up
  the current scope and client policies at their next resume.

### Security

- The gateway is built with Go 1.27.3, which fixes a certificate-verification
  vulnerability in `crypto/x509` (GO-2026-6809).
- Isolated checks on Claude Code 2.1.302 observe the synthetic gateway key
  only at the gateway in the tested request paths, including a gateway URL
  from managed settings and a key-helper refresh after a 401. The previous
  pin also sent it with start-up and feature-flag requests.
- Managed sessions disable and deny fork subagents. Inherited environment,
  user, project and local settings cannot re-enable the fork gate or
  replace workflow agents' models or the auto-mode classifier model.
  A managed policy that locks a model is reported, never overwritten.
- A model switch is refused when its fence hook cannot answer, including
  a missing, non-executable, crashing or slow shim. The fence gets
  15 seconds; other hooks keep their five-second limit and failure policy.
- Routine gateway routing logs keep the provider and model but no longer
  name credentials. WebSocket diagnostics move to debug level without
  credential IDs; selection and failure lines use account indexes or
  `[redacted]` for API keys. Substitution warnings remain per attempt and
  withhold key-derived indexes. Other gateway lines can still name account
  files; never share raw logs.
- The release build refuses runtime archives with escaping links or
  duplicate members, and prepared runtimes that retain Python startup
  customization files. Build and CI tools use refreshed, verified pins;
  isolated test lanes refuse bubblewrap older than 0.12 or setuid builds.
- A weekly vulnerability watch checks the latest signed release's gateway,
  Go modules and CPython. Reviewed CPython exceptions are tied to the
  advisory revision and runtime version and expire; one issue tracks
  current findings.

### Fixed

- Long-running gateways no longer hold every stop, restart or update just
  because ordinary logs filled the read limit. The persistence hold reads
  credential-save evidence over the whole run and reports where it started
  and how many saves it read. Missing or unreadable evidence still holds.
- The hold recognizes missing journal history, reported drops from the
  shared user-service log limit, and a journald restart. Failed saves are
  retained even in an incomplete read. If journald's process start cannot
  be observed, checking the credentials and explicitly clearing that
  coverage gap acknowledges it for the same gateway instance; later
  failures still hold. Unreadable system-journal drop notices are reported
  as unchecked, not as a failed save.
- Request paths cannot impersonate credential-save evidence. Large ordinary
  log lines no longer make that evidence incomplete. Old on-demand logs
  are removed only after a complete read; a read that times out keeps the
  log. A supervised stop refuses if the gateway restarted during its hold
  check.
- On OpenAI's API-key and ChatGPT-account routes, identified context-window
  refusals reach Claude Code as "prompt is too long", before or after the
  answer starts, so it compacts and retries. Other errors are unchanged.
- OpenAI-compatible tool calls stay separate when their indexes are
  omitted or fragments interleave, keep their announced IDs, and finish as
  tool use unless the provider reports truncation or filtering.
- A newly loaded credential is no longer left without models when request
  or refresh bookkeeping changes it before its first model registration.
- The full-screen interface remains interruptible while waiting for input
  and preserves incomplete multibyte characters across waits and screen
  suspension.
- Output on non-UTF-8 terminals no longer fails on charmer's markers:
  characters the encoding cannot show are replaced.
- `charmer lineup --session` accepts managed and runtime ids, historical
  runtime aliases and unique prefixes of at least eight characters.
  Ambiguous selections name the candidates; unreadable possible owners and
  pending forks still refuse.
- Uninstall fences gateway starts even without a working folder and
  refuses a symlinked working folder. Waiting writers reject lock files
  replaced while they acquire them.
- Migration from the earlier product name recognizes Claude and ChatGPT
  credential-save receipts, so persisted refreshes no longer cause a false
  unsaved-credential hold.

### Known limits

- A credential-file or auth-directory read error can remove one or more
  accounts' models from the running gateway instead of retaining the last
  working credentials. Resolve the read failure and restore owner-only
  access, then check `charmer providers list` and `charmer doctor`. If models
  remain missing, follow the persistence hold before a guarded restart.
  Do not delete the credential files.
- A model or alias reload can forget the gateway's in-memory rejection of
  an account whose access and refresh tokens were already refused, so the
  gateway may try that account again and return another 401. Sign in to the
  account again through charmer; do not edit or delete its credential file.
- On the ChatGPT account and OpenAI API key routes, once an earlier message
  in a reply has produced text, the gateway can omit a later message that
  arrives only in its final event. If a reply stops at a preamble or looks
  incomplete, ask for the rest.
- The pinned Claude Code 2.1.302 can resend an HTTP MCP tool call when its
  connection closes mid-call, so a tool that changes something may run
  twice. After an interrupted MCP call, check the external system for a
  duplicate before continuing. The pin also predates upstream fixes in
  Claude Code 2.1.304–2.1.309, including sandbox-boundary, worktree and
  Bash-permission fixes: do not rely on its Bash sandbox as the only boundary
  for untrusted code. charmer does not replace Claude Code's sandbox or
  worktree protections.
- An already-open loopback WebSocket connection can keep using a retired
  gateway key until it disconnects. Reconnect with the new key after
  rotation; managed Claude Code sessions use HTTP, not that route.
- Context-window errors from OpenRouter and other OpenAI-compatible
  providers do not trigger automatic compaction. Claude Code shows the
  provider's error; run `/compact`, then continue.
- A stream missing its final completion marker after completed content can
  make the pinned client report success without recovery, despite the
  gateway's error. A successful client result is not proof of complete
  stream framing; see [the late-stream limit](docs/troubleshooting.md#late-stream-completion-limit).
- Installed `agent.spawn` mods can redirect a fresh workflow agent to
  another model within the lineup fence. Exact per-agent binding is not
  guaranteed against that code.

## [2.0.1] — 2026-10-18

The first release under the name charmer. 2.0.0 was tagged but never
published: the release check found the vulnerabilities below in the
gateway's Go toolchain and in golang.org/x/net. 2.0.1 contains everything
listed under 2.0.0, with the gateway rebuilt on the fixed versions.

### Security

- The gateway is built with Go 1.26.9, which fixes GO-2026-6599 and
  GO-2026-6600 (html/template), GO-2026-6603, GO-2026-6609, GO-2026-6610,
  GO-2026-6611, GO-2026-6612, GO-2026-6613 and GO-2026-6617 (net/http),
  GO-2026-6605 (net/http and net/http/httputil), GO-2026-6607 (crypto/tls)
  and GO-2026-6608 (mime/multipart and net/textproto).
- The gateway links golang.org/x/net 0.60.0, which fixes GO-2026-6603,
  GO-2026-6610, GO-2026-6611, GO-2026-6612 and GO-2026-6617 in its HTTP/2
  implementation. The update moves golang.org/x/crypto to 0.57.0,
  golang.org/x/sync to 0.23.0, golang.org/x/sys to 0.48.0 and
  golang.org/x/text to 0.42.0, the smallest set of versions it requires.
- These golang.org/x/crypto and golang.org/x/text versions also fix
  GO-2026-6303, GO-2026-6354 and GO-2026-6355 (golang.org/x/crypto/ssh)
  and GO-2026-6629 (golang.org/x/text/secure/precis). The 2.0.0 review had
  found that none of them affects the gateway as charmer runs it.

## [2.0.0] — 2026-10-18

Renamed from claude-multi to charmer. The program, its commands, files,
service, environment, model selectors, release signatures and repository
carry the new name; what it does is unchanged. charmer 2.0.0 installs
beside an installed claude-multi and never updates it in place;
`charmer migrate from-legacy` moves such an installation onto charmer once.

### Added

- `charmer migrate from-legacy` moves an installation of claude-multi 1.1.0
  onto charmer in one journaled transaction: it stops the claude-multi
  gateway, renames its state, configuration and data directories, rewrites
  the files that name them or its service, writes charmer's hook shims,
  starts `charmer-gateway.service` when the gateway ran as a service (an
  on-demand gateway stays on demand) and runs doctor. `--dry-run` lists
  what it would move, `--status` shows the journal, `--back` returns to
  claude-multi from any step, and `--purge` removes the retained
  claude-multi installation tree and caches once the move is complete. It
  refuses while a session runs, across file systems, and when charmer
  already has state of its own; see the legacy migration guide. `--back`
  also hands back the sessions resumed or started with charmer: their
  records and launch settings name the earlier model names and variables
  again, and the kept release's own `doctor --repair-all` compiles the rest
  of their files again. `--back` checks charmer's gateway for an unsaved
  credential and stops it before it moves anything, enables the earlier
  gateway service again only if it was enabled and starts it if it was
  enabled or running, and refuses, moving nothing, when a folder exists
  under both names. `--purge` removes the emptied folders only while they
  are empty, reaches what it removes only through real folders, and
  refuses when the gateway's key file is selected through a link or `..`
  or lies inside what it removes.
- The installer and `charmer setup` notice a claude-multi installation and
  point to `charmer migrate from-legacy` instead of setting up beside it
  (one that cannot move, such as a Nix installation, is set aside as the
  guide's Start fresh instead section shows); doctor reports what such an
  installation left behind, and `charmer
  import` accepts an operator export claude-multi wrote.

### Changed

- The commands are `charmer`, `charmer-proxy` and `charmer-dev` (formerly
  `claude-multi`, `claude-multi-proxy` and `claude-multi-dev`); the installer
  writes `~/.local/bin/charmer` and `~/.local/bin/charmer-proxy`.
- The Python package, its distribution and console scripts are `charmer`
  (import package `charmer`, formerly `claude_multi`). charmer is not
  published on PyPI or npm; the packages named `charmer` there are unrelated.
- State, configuration and data live in `~/.local/state/charmer`,
  `~/.config/charmer` and `~/.local/share/charmer` (installations, sign-in
  records, charmer's own Claude Code copies, traces), formerly under
  `claude-multi`. The hook shims are `charmer-hook`, `charmer-hook-3` and
  `charmer-gateway-token`; the Nix service link is
  `~/.local/share/charmer/nix/current`; the build cache is
  `~/.cache/charmer-build`.
- The supervised gateway is `charmer-gateway.service` with
  `charmer-gateway-failure.service` (formerly `claude-multi-gateway…`), still
  on 127.0.0.1:8317; its journal start line and render sentinel say
  `charmer-proxy` and `charmer-render-`.
- Every `CLAUDE_MULTI_*` environment variable is now `CHARMER_*` with the
  same suffix, for example `CHARMER_MANAGED_ID`, `CHARMER_SESSION_ID`,
  `CHARMER_LAUNCH_EPOCH`, `CHARMER_GATEWAY` and `CHARMER_SECRET_ENV`;
  nothing exports the old names. `CHARMER_` is a reserved prefix for
  credential names. Status lines and scripts that read the old names must
  read the new ones.
- Catalog 39: the 31 gateway selectors `claude-multi-<model>-<effort>` are
  `charmer-<model>-<effort>` (same effort and `[1m]`); `gpt-multi-*`,
  `custom-*` and the canonical Claude model ids are unchanged. The gateway
  serves those 31 under the new names only: a session or setting recorded
  under an old name is translated to its new name when it launches or
  resumes, on the same provider, model, effort and window. A continuity
  alias the gateway keeps for a retired model keeps the name it was saved
  under, so it still shows in both model lists with the old prefix.
- The gateway's model list is unchanged from 1.1.0: the Anthropic-format
  list (`GET /v1/models` with an `anthropic-version` header) shows each
  `charmer-*` selector as it is, as it showed the old names, and
  `gpt-multi-*` and `custom-*` in the gateway's cloaked form as before. The
  gateway carries one more patch to keep it so.
- Messages, help, the TUI, the `/cm` skill (`charmer lineup`) and the
  lineup notice name charmer. The account sign-in acknowledgements carry new
  ids, so the next sign-in asks you to type `personal` again.
- Releases are signed under the namespace `charmer-release` with the
  principal `release@charmer` (the same key); the assets are
  `charmer-<version>-<target>.tar.gz`, `install.sh` and `install.ps1`.
- The repository is `github.com/nkoturovic/charmer`; the Nix flake's outputs
  are `charmer` and `charmer-proxy`; the doctor report's schema title and
  the operator export's format identifier name charmer.

### Removed

- The instructions for reaching a release through `update --from-dir` from
  an installed 1.0.0: charmer does not install claude-multi releases.

## [1.1.0] — 2026-10-07

More permissive model selection, OpenRouter stealth discovery, and fixes for
proxy-based updates, token-refresh shutdown and Mistral-style thinking responses.

### Added

- Model declaration commands accept an explicit per-model family label. The TUI
  model form supports editable display names and derives a stable local key when
  a new declaration leaves it blank; existing keys remain unchanged.
- OpenRouter discovery preserves every public model and offers a separately
  confirmed account-filtered listing. The CLI and TUI share pricing, context,
  tool-support and source labels. Stealth models are distinguished from routers
  and carry maker-hidden, pre-release, prompt-retention and availability cautions.
- Known `stealth/<name>` models can be added through discovery or the Providers
  screen using anonymous metadata lookup. Missing facts remain unknown;
  suffixed ids such as `:free` stay visible but are not addable in this release.
  Discovery never enables, admits, binds or chooses a default automatically.

### Changed

- Admission and qualification are optional attestations, separate from permission
  to use a route. Valid model lines on enabled, approved routes remain selectable
  without those badges; missing, failed or stale evidence is shown as a warning.
  Admission and revocation change metadata only and make no inference request.
- Any bounded, printable model-family label is accepted. An unrecognized label
  produces an "independence unknown" review label rather than a binding refusal.
  Explicit lead, agent and workflow-default bindings can override capability
  and role recommendations, with warnings.
- Provider context-window mismatches are capacity warnings that report the
  actual agent class, shared window and compaction trigger. Route approval,
  secret and consent checks, exact launch fences, native selector and effort
  constraints, disabled providers and role isolation remain enforced.
- The managed Claude Code pin is 2.1.292, with refreshed offline client evidence,
  explicit Agent-effort precedence and hook-context checks, and a synthetic
  compaction/resume test back to the previous 2.1.286 client.

### Fixed

- Step-by-step TUI text forms show the actual insertion point and scroll long
  values into view, including a visible end-of-input position in a full field.
  Cursor cleanup preserves hidden-field handling and choice screens.
- Release checks and downloads honour the process HTTPS proxy and bypass
  environment, with sanitized connection errors. TLS, release-host, redirect
  and signature checks remain in place. This uses the updater process's
  environment, not the gateway's outbound proxy setting.
- Gateway shutdown waits, within its existing bound, for in-flight sign-in
  token refreshes and their saves. The 30-second shutdown grace period starts
  at exit; exceeding it leaves persistence unverified and reports that fact.
- Mistral-style text and thinking content chunks are decoded in streaming and
  non-streaming responses instead of appearing as raw JSON or losing thinking.
  Existing string-content responses retain their previous bytes and behavior.
- Sessions compiled by 1.0.0 do not report integrity drift merely because newer
  review labels and model warnings render differently. Verified old diagnostic
  presentation is retained until resume, with doctor Attention rather than a
  false integrity block.
- Narrow-screen model pickers keep warning remedies and provider approval
  commands visible. TUI Direct mode now offers a default-No "Launch anyway?"
  confirmation for a missing credential, matching the CLI's advisory behavior.
- Installation documentation states minimal Linux prerequisites, version-matched
  checksum and signature downloads, and the terminal requirement for setup and
  uninstall.

### Compatibility and limits

- The supervised Linux gateway still needs unprivileged user namespaces,
  restricted by AppArmor on Ubuntu 23.10 and later; the on-demand gateway
  does not. See [service requirements](docs/guides/gateway.md#the-supervised-service-linux).
- Catalog version is 38; record and state format remain 4. Existing qualification
  records retain their original evidence and may become stale under the new pin;
  no automatic requalification or provider call is made.
- Rolling back the installation does not translate new model declarations or
  bindings into older policy. Family labels or unattested bindings newly allowed
  by this release may be rejected by 1.0.0; retain a compatible profile if you
  need to resume work there.
- Mistral thinking is unsigned. Faithful replay of Mistral reasoning history is
  not included; the existing [translation limits](docs/providers/openai-compatible.md#translation-limits)
  still apply. Provider behavior is tested with fixtures, not live accounts.
- Whether OpenRouter's account-filtered listing includes a particular stealth
  model depends on what the service returns. Add-by-id remains available for
  known supported ids; missing listing facts are not invented.
- A 1.0.0 installation behind a mandatory proxy must reach this release once
  through the installer or an offline update, because its old updater does not
  yet contain the proxy fix. See [Updating](docs/update.md).
- Platform-support claims are unchanged; see [Compatibility](docs/reference/compatibility.md#platforms).

## [1.0.0] — 2026-10-05

The first release.

### Compatibility and limits

- The supervised Linux gateway needs unprivileged user namespaces,
  restricted by AppArmor on Ubuntu 23.10 and later; the on-demand gateway
  does not. See [service requirements](docs/guides/gateway.md#the-supervised-service-linux).
- Platform support requires a native install journey; the channel and
  platform labels are in [Compatibility](docs/reference/compatibility.md#platforms).
- Keyed OpenAI-compatible support is open. Its proof uses synthetic
  endpoints, not vendor accounts; the [translation limits](docs/providers/openai-compatible.md#translation-limits)
  still apply, and presets are not vendor-tested.
- Quota observations are available only in the Nix channel, not in release
  bundles or source checkouts. They are not provider balances or billed
  token totals.
- Managed sessions run the hash-verified pinned Claude Code. Adoption by a
  newer client's background supervisor and resuming a newer client's
  transcript are not verified compatibility guarantees; see
  [client skew](docs/reference/compatibility.md#client-skew).
- State format is 4. Updates never downgrade state, and rollback refuses
  when the previous release cannot read the state on disk. Nix packages
  update through Nix; release bundles use `claude-multi update`. See
  [Updating](docs/update.md).

Release-bundle platform status:

- Linux x86_64: supported; native release-bundle journeys and the Linux
  release battery.
- Linux aarch64: supported; native release-bundle journey on Ubuntu 24.04
  arm64.
- Apple silicon: supported; native release-bundle journey on macOS 15;
  on-demand gateway only, with no launchd service.
- Intel macOS: built and reproduced, no native journey; no support claim.
- Windows through WSL2: supported inside WSL2; Windows Server 2025 with
  Ubuntu 24.04. Native Windows is not supported.

The native journeys exercise release bundles, not Nix installations.

### Command-line names

Use these public spellings; the alternative spellings remain accepted
aliases, not separate workflows:

| Alias | Public spelling |
| --- | --- |
| `--composition`, `--composition-file` (aliases) | `--profile`, `--profile-file`, respectively |
| `compose` | `profile` |
| `compose delete` | `profile rm` |
| `compose restore-default` | `profile reseed balanced` |
| root `show` | `profile show` |
| `sessions link --composition` / `--model` (aliases) | `sessions link --profile` / `--direct` |
| `sessions transition` | `lineup --session <runtime-id> --relaunch profile <name>` |

The [command reference](docs/reference/cli.md) gives the arguments for
each public command.

### Added

- Keyed OpenAI-compatible endpoints and the Cerebras, Gemini, Groq, Mistral
  and xAI presets, after the complete source-bound compatibility proof.
  Messages-to-chat translation is HTTPS and bearer-only; it still requires
  route approval, a key, model admission and agent qualification. Presets are
  documented configurations, not vendor-tested guarantees. Endpoint forms
  offer only supported authentication, and generic OpenAI Platform host
  refusals point to its separate supported API-key route.
- Launch Claude Code with a lineup — a lead model plus up to nine `cm-*`
  agents on other models — from a saved profile, an unsaved profile file or
  a single direct lead (`claude-multi`, `claude-multi direct`).
- A per-session compiled scope: agent files, the lead set `/model` offers,
  the `/cm` skill for live lineup changes and lifecycle hooks; sessions are
  recorded and resumable, and native forks resolve in one step.
- claude-multi's own hash-verified copy of the pinned Claude Code version,
  copied from an existing installation or downloaded with consent
  (`claude-multi setup --step claude`).
- A local gateway (CLIProxyAPI with local patches, loopback only), started
  on demand or installed as a supervised user service
  (`claude-multi gateway …`, `claude-multi gateway service install`).
- Profiles, seeds and named bindings; providers, models and qualification
  for models you declare; a terminal UI for every task, with a line
  mode without curses.
- `claude-multi doctor` with a remedy for every finding.
- Agents run at full context: one compaction window, the smaller of the
  window ceiling (800K) and the lead set's smallest provider bound, applies
  to the lead and to every agent on a 1M-class line whose provider bound
  is at least that window. An agent line whose bound is below it runs in
  the 200K class, so it never outgrows its provider. A session recorded
  with agents in another class keeps them until its next resume, which
  shows each move ("agent class 200K → 1M"); doctor reports pending moves.
- One window ceiling for the lead and every agent, 200K to 800K (default
  800K): `claude-multi window-ceiling 400K` sets it,
  `claude-multi window-ceiling --reset` restores the default, and the
  Settings row edits it; it applies at the next launch or resume. The card, the lineup
  summary (`/cm`, `profile show`) and `--print-launch` show each role's
  effective window.
- Setting up from the terminal: `claude-multi setup` runs the steps that
  are not done yet, `claude-multi setup --answers FILE` applies a prepared
  setup after showing its whole plan for one confirmation (a starter
  profile with its slots and files, the default profile; a file whose
  entries depend on each other is refused with the fix), and
  `claude-multi doctor --first-run [--json]` checks a new installation
  without changing anything.
- Connecting providers from the terminal or the UI: API keys
  (`claude-multi providers set-key|remove-key`), Claude and ChatGPT account
  sign-ins in the terminal after a personal-use confirmation
  (`claude-multi providers sign-in|sign-out`), a consented connection test
  (`claude-multi providers test`) and a key file you choose
  (`claude-multi setup --keys-file`).
- OpenAI's models over an OpenAI API key
  (`claude-multi providers transport openai api-key`), through the
  gateway's Responses route: only the models reviewed for the OpenAI API
  are served on it, each with its documented context window and efforts;
  never through a saved ChatGPT account and never as an empty model list.
  The gateway sends the key as a plain API client (no ChatGPT client
  version or session header) and replaces any provider error with fixed
  local text, so an error that quotes the key never reaches a client.
  GPT-6.1 Sol, GPT-6 Astra and GPT-6 Luna are reviewed for it. In
  Providers, Enter on OpenAI chooses between the ChatGPT account and the
  API key, as it does for Anthropic, and K replaces the key in use. While
  the key is selected the gateway adds no hosted image-generation tool the
  client did not ask for, and a model's details in Models show the key
  route's own evidence (its documented bounds and the conservative
  validated floor), never the account route's measurement. Each request
  is one Responses request with no retry when it succeeds; the client's
  output cap is not passed on, so the model's own output limit applies.
- Presets in the provider picker for vendors with a documented
  Anthropic-compatible endpoint (Z.ai, Moonshot AI's Kimi platform,
  Alibaba Cloud Model Studio, Novita AI, Vercel AI Gateway), with an
  OpenAI-compatible endpoint (Groq, Mistral AI, xAI, Cerebras, Google
  Gemini) and for servers on
  your computer (Ollama, LM Studio, vLLM, llama.cpp). A preset fills in the
  address, the key's name and the model list from the vendor's
  documentation and is labelled a preset: nothing is tested until you add
  and admit a model (`claude-multi providers add --preset NAME`, or Enter
  on it in Get started: you name it, approve where its key goes and type
  the key). One more provider from a preset whose key another provider
  already uses gets a key of its own under its own name; sharing the saved
  key (`--reuse-key`) or replacing it for every provider that uses it
  (`--replace-key`, after a confirmation that names them) is your explicit
  choice. Providers N lists the same presets as Get started, and the preset
  form takes another address the vendor documents, such as a workspace
  endpoint (the preset's own by default; checked like
  `providers add --preset NAME --base-url URL`).
- Replacing a saved API key that other providers share
  (`claude-multi providers set-key`, or K on Providers) names every
  provider using it and asks first (default No, Cancel on the screen);
  `--yes` replaces it without the question. A provider of a `custom.json`
  that is not migrated yet counts like any other (none once it is
  migrated, nor one claude-multi ignores), here and when a preset's key
  name is already in use. The key is saved only while the provider it was
  planned for still exists and still uses that key; a provider removed or
  changed meanwhile refuses the change with nothing written.
- For an Anthropic or OpenAI API-key transport that is chosen and approved
  but whose key is not saved, doctor names
  `claude-multi providers set-key <provider>` and the screens name G → K,
  instead of a switch to the transport already in use; the launch card's H
  report names G (providers) → K on the provider (a resume card its way
  back first).
- Models for the shipped Kimi and Qwen providers, read from their vendors'
  documentation: Kimi for Coding on the Kimi Code plan, and Qwen3.8 Max and
  Qwen3.8 Flash on the Model Studio Token Plan, so either key alone starts
  a profile.
- Account sign-ins are described as data (`account-pools.json`): each
  account's provider, sign-in method (a browser page or a device code),
  personal-use text, sign-in record names and registry sections. The
  Claude and ChatGPT sign-ins behave as before and earlier confirmations
  stay valid. Sign-in records are told apart by the gateway's full file
  name prefixes, so one service's records never count as another's
  (`kimi-ai-…` is never a `kimi` record), and a confirmation a later
  release stores for an account it adds is kept and ignored.
- A provider whose key you save before it has any model continues into
  adding one (a listing you consent to, or by hand) and admitting it, so a
  profile can use it; the command line and the profile step name the same
  steps. A server on your network is lead-only.
- Providers E edits a provider you added in a form filled with what it
  has now, through the same checks, preview and confirmation as
  `claude-multi providers edit`. Saved Claude and ChatGPT accounts stay
  listed while their provider uses its API key.
- `claude-multi setup --answers` plans a starter profile against the
  transport the same file selects.
- The launch card keeps its notices readable at 80×24: an ignored
  `CLAUDE_CONFIG_DIR`, new model lines (M → Enter admits) and implementers
  outside a Git repository (git init) each keep a row in a shorter form, and
  a long notice loses its middle, never its remedy. Slots missing the same
  sign-in share one row; a sign-in remedy names G → L on the card (a resume
  card names its way back first: Esc once for each screen above the launch
  card, or `run claude-multi` again when Esc ends it), while the command
  line keeps the sign-in commands. V shows the terms and the `/cm` guidance the help
  shows.
- The lineup dialog offers a fallback to every provider that has a
  fallback profile, not only the providers the session already uses, with
  `/cm fallback`'s own preview.
- The binding picker and the selected profile-editor slot describe the
  slot's role and grade; V shows the whole description.
- After a forget, the Sessions screen shows the result in a view that keeps
  the full runtime id and both ways to manage the conversation again.
- A resume gate whose transcript sits under a directory that cannot be
  told apart, or no longer exists, offers the directory chooser; moving the
  transcript stays a command you run.
- A colour-query reply that arrives in pieces, before and after the card is
  drawn and with pauses between them, no longer quits the card and none of
  its bytes reaches it as a key. Esc still cancels the card while such a
  reply is unfinished, also when Enter follows it at once.
- The new-provider form never draws a key typed or pasted into the
  secret-name field, not even in part, and refuses it at the field, before
  any preview. A candidate declared from the
  pinned registry keeps the registry as its context source.
- `claude-multi sessions resolve-fork` asks before it discards a fork's
  marker (y/N, default No); without a terminal it needs `--yes`.
- `/cm show` lists the last day's failed requests per provider the session
  uses, with how far the gateway's log was read and the remedy
  (`/cm fallback <provider>` with `/cm profiles`, `/cm set <agent>=<model>`; one size however
  many profiles there are); when the log cannot be read it says so instead of counting zero.
- A storage or permission failure of `claude-multi lineup` (no space, no
  permission, a read-only file system) names the fix like every other
  command; `/cm` keeps answering with status 0.
- `claude-multi doctor --rotate-token` asks its questions on the terminal
  and writes its result to stdout and a pause to stderr, so both can be
  redirected.
- Ctrl-C in `claude-multi-proxy` prints one cancellation line and exits
  130.
- doctor shows every `SSL_CERT_FILE`/`SSL_CERT_DIR` value home-relative, a
  relative one included.
- `claude-multi gateway logs`, the gateway log view and the log tail a
  failed start shows print every line redacted: keys and tokens,
  `Authorization`, API-key and management-key headers, cookies,
  secret-named fields, credentials in addresses and account identifiers
  (`auth=` sign-in record names, JSON account fields, e-mail addresses)
  read `<redacted>`. Terminal escape sequences are removed before anything
  is matched; a quoted value, a list or an object is redacted whole, and so
  is a value that runs over several lines, such as a private key, also when
  the lines shown start inside it, it follows a field name
  (`private_key: …`) or the log ends before its last line. A carriage
  return, a vertical tab or a Unicode line separator inside a journal
  message never splits a header from its value.
- The supervised gateway service trusts the certificates the launcher
  trusts: `claude-multi gateway service install` (and its refresh) writes
  `SSL_CERT_FILE`/`SSL_CERT_DIR` into the unit, bound read-only where the
  service would not see them, and refuses, changing nothing, when one is
  not a readable file or folder, its path cannot be written into a unit
  (a `:` included, in your home folder too), or a certificate folder is
  your home folder, a folder above it or a private system folder (or links
  to one). A unit missing a certificate bind it needs reads as stale.
- The gateway dialog and the transition screen's note on a session still
  running wrap, so their remedies are never cut off.
- The key file you select (`claude-multi setup --keys-file`) may sit in any
  private folder of your own (no group or other access), besides
  `~/.config/claude-multi`; managed sessions are denied that file by name.
  No other folder is assumed to hold keys.
- `SHA256SUMS` lists `install.sh` and `install.ps1` besides the manifest and
  the bundles, so the release signature covers the installers;
  `install.sh` carries the bundles' checksums only. `tools/release.py`
  `build`, `verify-draft` and `publish` refuse a release whose
  `SHA256SUMS` leaves either installer out.
- `claude-multi window-ceiling --reset` names the ceiling it replaced and
  the command that sets it again.
- The preset key chooser shows short choices, with the full name of a new
  key in the text above them, readable at 80×24.
- The help names `USAGE.md` as the documentation map; doctor's
  managed-policy remedy reads "ask the policy owner, or use plain claude".
- A retained OpenRouter model alias reports its model's owner, as the
  current ones do.
- The Nix package installs `claude-multi` and `claude-multi-proxy` only;
  `claude-multi-dev` stays a source checkout's command.
- Kimi or Qwen alone reaches a usable profile: adding a model the listing
  states no effort for, or one entered by hand, no longer fails on a
  provider without a high-effort contract. The command line's next steps
  name the whole path (list, add, admit, then `claude-multi setup --step
  profile`).
- `claude-multi quota`, `/cm quota` and the Providers details name the
  quota's condition (available, stale, exhausted, management-disabled,
  unavailable, not-ours); the command exits 1 exactly when no reading was
  made.
- `claude-multi uninstall` (also `install.sh --uninstall`): the plan first,
  credentials only after a typed phrase; it refuses while a session may
  still run, another command writes session state or a store it removes,
  a sign-in save is unconfirmed, a transaction holds the gateway
  inhibition or the gateway service cannot be removed safely, never
  follows a link out of its folders, removes the installer's launchers and
  PATH line only as its receipt proves them, and keeps every key file you
  did not agree to delete, the previous release and a launcher changed
  meanwhile.
- One list of credential locations: managed sessions cannot read any of
  them, `claude-multi export --out` never writes into one, and uninstall
  deletes what lies there only after the typed phrase.
- One inhibition per state root for transactions that change the gateway
  or its installation (the service hand-off, installers, updates, machine
  moves): while one is recorded, every other start, stop, restart, service
  change, configuration change, install and update refuses with one message
  naming the owner and the remedy (exit 1); a gateway already proven yours
  keeps serving and doctor reports an interrupted one. Shell owners use
  `python3 -m claude_multi.gateway_inhibition`. A change of a home's
  gateway configuration or keys also waits for the transaction of the
  state root that manages that home, refuses when that root changed while
  it waited, and refuses while `continuity.json`, which names it, cannot be
  read.
- A new home records its own gateway port before anything renders, reloads
  or starts a gateway there; until then those commands say the gateway is
  not set up and name `claude-multi setup --step gateway`.
- A Python package with three console entry points (`claude-multi`,
  `claude-multi-proxy`, `claude-multi-dev`) and a Nix
  flake.
- Release bundles for Linux and macOS on x86-64 and arm64, each with its
  own Python runtime and gateway, built reproducibly by
  `tools/build.py release`, with licences (the bundled runtime's library
  licences and notices included), SBOMs and checksums; releases
  are checked, signed and published with `tools/release.py`.
- An installer for Linux and macOS (`install.sh`; `install.ps1` sets up
  WSL 2 on Windows and runs it there). It verifies the release signature
  (a failed signature always stops it), installs into
  `~/.local/share/claude-multi/install` keeping the previous version,
  never touches another installation's state without `--migrate-from-nix`,
  and records the launchers and PATH line it wrote in `installer.json`.
  It pauses gateway changes while it works; an install interrupted after
  it started changing the installation keeps them paused until
  `sh install.sh --repair` finishes it (given the release again when a
  first install stopped before any version was in place).
- `claude-multi update`: checks for a newer signed release, shows what it
  changes (size, Claude Code, state format, gateway restart), asks, and
  installs it together with the Claude Code version it needs; `--check`
  only checks, `--rollback` returns to the previous version, `--from-dir`
  uses downloaded release files. The card's U runs the same steps; doctor
  and the card show how old the installed release is. A running gateway
  keeps the version it was started from until it restarts. An interrupted
  update or rollback is finished by the next `claude-multi update`; a
  switch stopped between the `current` and `previous` links is put back as
  it was. A rollback goes only to the version it showed.
- The installer and `update` remove old versions and the Claude Code
  copies no installed release needs; a copy a running session uses is
  kept and reported.
- `claude-multi --version` names the catalog, gateway and Claude Code
  versions it ships.
- State is only ever migrated forward: a rollback refuses once a newer
  version has upgraded the state, and an older version refuses to change
  newer state and names the fix.
