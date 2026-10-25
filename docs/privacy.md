# Privacy: what leaves your computer

charmer runs Claude Code and a local gateway on your computer and
sends your requests to the providers you connect. This page lists what
goes where, what stays local, and what charmer does not control.

## Data flows

| From | To | What | When |
| --- | --- | --- | --- |
| Claude Code (managed session) | the local gateway (loopback) | your prompts, file contents the session reads, tool results, the gateway key | every model request |
| the gateway | the provider serving each role | the requests above, with that provider's key or sign-in | every model request |
| Claude Code (managed session) | Anthropic | Claude Code's own start-up, feature-flag and other nonessential requests, as upstream Claude Code makes them | as Claude Code decides |
| the launcher | `downloads.claude.ai` | a download of the pinned Claude Code | only after you agree |
| the launcher | the release location | release metadata and signed checksums; the bundle when an update is accepted | metadata when you run update or check; the bundle after confirmation or `--yes` |
| the launcher | a provider's model-listing endpoint | the listing request, with the authentication named in its request plan, or no credential for a public/keyless listing | only after you agree to the listed requests |
| the launcher | the public third-party model feed | a credential-free GET to compare the feed with the pinned registry | only after you agree to the feed request |
| the launcher | your configured name resolver and a declared keyless LAN server's host and port | hostname resolution and a short TCP check; no HTTP request, prompts or credentials | when a diagnostic/readiness check probes that declared server |
| the launcher, through the gateway | the selected provider | a connection-test, admission-smoke or qualification request | only after you agree to the listed requests |
| the gateway's sign-in process and your browser | the account provider | sign-in authorization and token exchange | when you choose account sign-in |

Model listings and the public feed go directly from the launcher, not
through the gateway. The launcher also contacts loopback for gateway
status, quota and lifecycle checks; those do not send a provider request.

Isolated Linux x86_64 testing of the pinned Claude Code sent every request
the client made through its proxy to a local interceptor that accepted any
host. The test client key reached the local gateway and nothing else: none
of the requests to `api.anthropic.com`, `downloads.claude.ai` or
`raw.githubusercontent.com` carried it, with the managed settings or with
the client's defaults. Earlier pins, through 2.1.292, sent it with
requests to `api.anthropic.com` (paths including `/api/claude_cli/bootstrap`
and `/api/eval/<id>`). These requests were intercepted locally, not sent to
anyone; a request that does not use the proxy was not observed.

This is not an exhaustive list of Claude Code's destinations. Equivalent
traffic observations are not available for Linux aarch64, macOS or WSL2.
Do not treat a platform's installation result as a guarantee that
background requests are absent or credential-free.

### Claude Code's own traffic

Managed sessions keep Claude Code's upstream behaviour for its
nonessential traffic: **charmer does not guarantee suppressing Claude
Code background traffic.** Claude Code controls its own start-up and
feature-flag requests. Do not assume they are confined to loopback or
credential-free: a managed client's credential is the gateway's local key,
not a provider key. That key only opens your local gateway, which listens
on loopback; rotate it any time with `charmer doctor --rotate-token`.
The launcher sets `CLAUDE_CODE_DISABLE_FAST_MODE=1`; this setting is not
a guarantee that all non-model requests are suppressed. Claude Code's
feedback drafts are off in managed sessions by default (Settings →
feedback drafts).

### What providers receive about you

Besides the requests themselves:

- third-party Anthropic-compatible providers receive Claude Code's
  session id, and for agent requests the agent id (and the parent agent's
  where Claude Code sends one), as random identifiers per session;
- the two OpenAI routes (the ChatGPT account and the OpenAI API key)
  receive neither id, but a prompt-cache key the gateway derives from the
  model, the session and the agent: a stable value for one agent of one
  session, from which the ids cannot be read back;
- OpenAI-compatible routes receive none of these;
- charmer adds no identifying hint headers of its own.

OpenAI-compatible providers receive the caller's `User-Agent` header, which
can identify the client software and version; the gateway supplies a default
when none is sent. On Claude account and Claude-compatible direct routes,
requests recognised as Claude Code retain the client's own SDK-version
header, `x-stainless-package-version`, instead of the gateway's pinned value.

On the OpenAI API key, the gateway sends the key as a plain API client
(no ChatGPT client version or session header) and adds no hosted tool
the client did not ask for.

Each provider's own terms and retention policies govern what it keeps,
and some differ by product: Meta's contributor tier (the
`muse-contributor` line) is priced lower in exchange for permission to
use your prompts and completions to train future Meta models
([providers/api-keys.md](providers/api-keys.md#meta)).

## What stays on your computer

| What | Where | Notes |
| --- | --- | --- |
| API keys | the key file ([reference/settings.md](reference/settings.md#the-api-key-file)) | mode 0600 |
| account sign-ins | `~/.local/share/charmer/` | plaintext token files, mode 0600, in a private directory |
| sign-out and profile backups | next to the originals | kept until you remove them |
| the gateway's configuration and local key | `~/.config/charmer/` (`config.yaml`, `api-key`) | contain secrets |
| session records and generated files | `~/.local/state/charmer/` | no transcript content |
| the lineup log | `~/.local/state/charmer/lineup-log/` | the binding of each agent start; no content |
| hook failures | `~/.local/state/charmer/hook-errors.log` | time, event, session and error class; never content |
| gateway logs | `~/.local/state/charmer/gateway/logs/` (on demand) or the service journal | can name account files and credential indexes; `charmer gateway logs` shows them redacted |

Backup tools, sync clients and desktop search indexers may copy files from
your home folder, these included. Exclude `~/.config/charmer/` and
`~/.local/share/charmer/` from them if you do not want credentials
copied. Account for XDG-selected configuration/state roots and any external
key file too; the table shows default locations, not a complete inventory
of custom paths.

A [cold backup](guides/backup.md) deliberately copies credentials and
refresh tokens, unlike configuration export. Encrypt it, keep it outside
every source tree and source-code checkout, and restrict its archive to
mode 0600 in a private 0700 directory. Do not share the archive, its
plaintext contents or raw logs. Restore workspaces are sensitive too.
Never run a restored OAuth sign-in on two hosts at once.

charmer never reads Claude Code's transcripts. User-owned Claude Code
files and transcripts are excluded from the cold-backup procedure; it
cannot recover conversation history from charmer records.

## Inside managed sessions

A managed session does not inherit your credentials: every provider key
variable charmer knows of, and every other `*_API_KEY` variable in
your shell, is removed from the session's environment, except the names
you list in Settings (**O**) → kept environment (`session_env_keep` in
`choices.json`; names only, never values; provider key names are refused
there). The session's agents are also denied reading charmer's
configuration, its credential folders and your key file. See
[security.md](security.md#credential-containment).

## Exports and diagnostics

- `charmer export` writes your configuration without keys,
  sign-ins, sessions, logs or evidence
  ([guides/move-machines.md](guides/move-machines.md)).

### Diagnostics you share

- `charmer doctor --json` is the report to attach to an issue. Its
  environment block is an allowlist: folders are reported as present or
  not (no paths from your home), the terminal type is generalized, and
  custom hosts and proxy values are left out. Doctor never prints a
  secret.
- `charmer --version` prints the release identity (launcher,
  catalog, gateway, Claude Code); a missing component says unavailable.
- Gateway logs are for reading locally: the files can name account files
  and credential indexes. `charmer gateway logs` redacts keys, tokens,
  cookies and account identifiers; still check its output before you
  attach it anywhere public.
- Never attach a key file, a sign-in file, `config.yaml`, a transcript or
  an environment dump.
