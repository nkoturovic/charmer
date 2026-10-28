# Command-line reference

charmer installs two commands:

- `charmer`: the launcher and every user command;
- `charmer-proxy`: the local gateway's own tool (`charmer-proxy
  --help`), which the launcher and the gateway service run; you rarely need
  it directly.

A source checkout also has `charmer-dev`, the maintainers' tool for
reviewed catalog changes; it is not part of an installation.

Every command's `--help` lists its options, examples and exit behaviour.
Exit statuses of the user commands: 0 done, 1 failed, refused or
unavailable, 2 usage, 3 you declined, 130 interrupted.
Exceptions: `update --verify` uses 0 complete, 1 mismatch, 3 incomplete or
unsupported (2 usage, 130 interrupted). `doctor --repair <id>` exits 3 when
the session's scope is deferred rather than converged. A verified installation
with a stopped gateway is not ready; see [verification](../update.md#verify-an-installation).

## Output and partial failures

A plain `charmer sessions list` (also bare `charmer -r` without a
terminal) keeps readable rows when a record or the sessions directory
cannot be read, reports the errors on stderr and exits 1.
`charmer sessions list --json` instead returns an unavailable document,
not a partial inventory. In a complete JSON listing, `live` and
`needs_attention` are independent: a running session can need repair or a
lead choice, and an ended one can too.

`charmer sessions relink-runtime <id> <runtime-id>` saves the identity
change before repairing its scope. If that repair fails or is refused,
the identity change remains; the command exits 1 and prints the exact
`charmer doctor --repair <id>` command to finish it. When the repair is
deferred, it also exits 1 and names `charmer -r <id>` to resume and finish
convergence.

Declining token rotation, starter-profile creation or an import returns
3. A profile editor that cannot start or exits unsuccessfully returns 1;
the profile is not saved and the message names the temporary copy kept
for recovery. `$VISUAL` takes precedence over `$EDITOR`.

On a non-UTF-8 output stream, unsupported characters are replaced, even
when Python's UTF-8 mode is enabled. This also affects JSON string values:
use `PYTHONIOENCODING=utf-8` when their exact text matters.

<!-- generated: cli-reference (tools/docs_gen.py) -->

## Launch

```text
charmer [--profile <name> | --profile-file <path>] [-c | -r [<id>]] [--force] [--line] [--no-color] [--print-launch] [--version] [-- <claude-args>]
charmer <command>
```

| Argument | What it does |
| --- | --- |
| `--profile <name>` | launch a saved profile (the session follows it) |
| `--profile-file <path>` | launch one unsaved profile JSON (pinned, nameless); '-' reads stdin and forces noninteractive; resume/continue refuse it; nothing is saved |
| `-c`, `--continue` | continue the last managed session in this directory |
| `-r [<id>]`, `--resume [<id>]` | resume a managed session: its id, an id prefix of 8 or more characters, or its name; no value opens the sessions picker |
| `--force` | resume despite a background-liveness marker (only bypasses the heuristic ● check; identity/transcript guards still apply) |
| `--line` | force the line-based UI (no full-screen curses interface) |
| `--no-color` | disable all color output (the NO_COLOR environment variable is also honored) |
| `--print-launch` | print the exact Claude argv and env summary instead of launching (the gateway token is never shown); says whether the launch would succeed |
| `--version` | show the release identity (launcher, catalog, gateway, Claude Code) and exit |

Examples:

| Command | What it does |
| --- | --- |
| `charmer` | open the launcher |
| `charmer --profile <name>` | launch a saved profile |
| `charmer -c` | continue the last session in this directory |
| `charmer -r <id>` | resume a session (its id, an 8+ character prefix, or its name) |
| `charmer direct --model <model>` | launch one model as the lead |
| `charmer doctor` | check health, with one fix for each finding |
| `charmer --profile <name> -- <claude-args>` | pass `<claude-args>` to Claude Code unchanged |

### charmer direct

Launch one model as the lead, with no cm-\* agents (recorded and resumable like every session).

```text
charmer direct [--model <model>] [--no-subagents] [--force] [--print-launch] [-c | -r [<id>]] [-- <claude-args>]
```

| Argument | What it does |
| --- | --- |
| `--model <model>` | the model to run as the lead (charmer models list); without it a terminal opens the Direct picker |
| `--no-subagents` | hard-deny subagent delegation for this session (recorded; resume re-applies it, a mismatching explicit flag is rejected) |
| `--force` | resume despite a background-liveness marker (only bypasses the heuristic ● check) |
| `--print-launch` | print the exact Claude argv and env summary instead of launching |
| `-c`, `--continue` | continue the last managed session in this directory |
| `-r [<id>]`, `--resume [<id>]` | resume a managed session: its id, an id prefix of 8 or more characters, or its name |

## Inspect (read only)

### charmer doctor

Check this installation and its sessions. The verdict is Ready, Attention or BLOCKED; each finding names one fix. Plain doctor is read-only and does not fetch release metadata; use doctor --json for a read-only report. --repair, --prune and --rotate-token act.

```text
charmer doctor [--json | --repair <id> | --repair-all | --prune | --rotate-token | --prune-aliases [<alias>…]] [-v] [--first-run] [--preview] [--include-live]
```

| Argument | What it does |
| --- | --- |
| `--json` | read-only diagnostic report (one JSON document) |
| `-v`, `--verbose` | also show every session's line and the observation details |
| `--first-run` | the first-run checks in order, one fix each (with --json: one object) |
| `--repair <id>` | converge a session's scope to its record authority (its id, an id prefix of 8 or more characters, or its name); exits 3 when the scope is deferred, not converged |
| `--repair-all` | converge stopped durable sessions' scopes to record authority; lineups the installed catalog cannot recompile are deferred until resume or a model choice |
| `--preview` | read-only preview of a prune (--prune or --prune-aliases); nothing is written |
| `--prune` | remove stale staging dirs and scopes whose records are gone |
| `--rotate-token` | rotate the local gateway token hitlessly (dual-key window, ~5 min helper-TTL wait, reload verified; interactive; rerun resumes) |
| `--prune-aliases [<alias>…]` | remove gateway continuity aliases no live session references (all, or the named ones) |
| `--include-live` | with --repair-all: also converge sessions whose last event is not `end` (live or unknown) |

### charmer quota

Account quota and credential health from the local gateway (one local read; no provider call).

```text
charmer quota [--json]
```

| Argument | What it does |
| --- | --- |
| `--json` | one read-only report object |

### charmer explain

Explain a session's recorded bindings and observed routing.

```text
charmer explain [<id>] [--agent <agent>] [--json]
```

| Argument | What it does |
| --- | --- |
| `[<id>]` | its id, an id prefix of 8 or more characters, or its name; defaults to the current session |
| `--agent <agent>` | bound cm-\* role or exact observed agent id |
| `--json` | one read-only report object |

### charmer usage

Observed client requests per model and session (not token usage).

```text
charmer usage [--since <when>] [--session <id>] [--json]
```

| Argument | What it does |
| --- | --- |
| `--since <when>` | RFC 3339 instant or duration up to 30d; default 24h |
| `--session <id>` | its id, an id prefix of 8 or more characters, or its name; exact attribution only |
| `--json` | one read-only report object |

### charmer plan

Preview what the gateway would serve after pending changes; writes nothing.

```text
charmer plan [--assets <path>] [--json]
```

| Argument | What it does |
| --- | --- |
| `--assets <path>` | plan a candidate package's assets (the running launcher is unchanged) |
| `--json` | one read-only plan object |

## Manage

### charmer setup

Run the setup steps that are not done yet, in order: preflight, claude, gateway, providers, test (optional), profile, check. Nothing is saved until you confirm. --step runs one step (also when it is done); claude puts charmer's own copy of the pinned Claude Code in place; gateway records the gateway's outbound proxy (an unauthenticated http, https or socks5 URL; the gateway never inherits proxy variables) and starts the gateway, or re-renders a running one.

```text
charmer setup [--step preflight|claude|gateway|providers|test|profile|check] [--redo] [--status] [--answers <file>] [--keys-file <path>] [--claude-from <path>] [--proxy <url> | --no-proxy]
```

| Argument | What it does |
| --- | --- |
| `--step preflight\|claude\|gateway\|providers\|test\|profile\|check` | run this step only (also when it is done) |
| `--redo` | run the steps that are done too |
| `--status` | show the steps and exit 0 when every required one is done, else 1 |
| `--answers <file>` | a prepared setup (keys by file only; sign-ins and tests need you at the terminal) |
| `--keys-file <path>` | use an existing private key file (NAME=value lines) for API keys |
| `--claude-from <path>` | claude step: copy the pinned Claude Code from this file (checked by size and sha256) |
| `--proxy <url>` | gateway step: the gateway's outbound proxy (no credentials in it) |
| `--no-proxy` | gateway step: remove the gateway's outbound proxy |

### charmer profile

List, show, create, edit, copy, rename, remove and restore profiles; choose the default profile.

```text
charmer profile <command>
```

#### charmer profile list

List profiles with origin and last use.

```text
charmer profile list [--json]
```

| Argument | What it does |
| --- | --- |
| `--json` | one JSON document (stable fields) |

#### charmer profile show

Show a profile's evaluated lineup.

```text
charmer profile show [<name>]
```

| Argument | What it does |
| --- | --- |
| `[<name>]` | profile name (default: balanced) |

#### charmer profile new

Create a profile ($EDITOR, or --from SRC).

```text
charmer profile new <name> [--from <profile>] [--keep-fallback]
```

| Argument | What it does |
| --- | --- |
| `<name>` | profile name |
| `--from <profile>` | copy SRC (non-interactive) |
| `--keep-fallback` | with --from: the copy stays a fallback profile for SRC's provider |

#### charmer profile edit

Edit a profile in $VISUAL/$EDITOR.

```text
charmer profile edit <name>
```

| Argument | What it does |
| --- | --- |
| `<name>` | profile name |

#### charmer profile rm

Remove a profile of yours (a copy is kept).

```text
charmer profile rm <name> [--yes]
```

| Argument | What it does |
| --- | --- |
| `<name>` | profile name |
| `--yes` | skip the y/N confirmation |

#### charmer profile rename

Rename a profile.

```text
charmer profile rename <name> <new>
```

| Argument | What it does |
| --- | --- |
| `<name>` | source profile name |
| `<new>` | target profile name |

#### charmer profile duplicate

Duplicate a profile.

```text
charmer profile duplicate <name> <new> [--keep-fallback]
```

| Argument | What it does |
| --- | --- |
| `<name>` | source profile name |
| `<new>` | target profile name |
| `--keep-fallback` | the copy stays a fallback profile for the source's provider |

#### charmer profile reseed

Restore a shipped profile to its shipped version (your version is kept as a copy).

```text
charmer profile reseed <name> [--yes]
```

| Argument | What it does |
| --- | --- |
| `<name>` | profile name |
| `--yes` | skip the y/N confirmation |

#### charmer profile default

Show, set or clear the default profile (used where nothing else chooses one).

```text
charmer profile default [<name>] [--clear]
```

| Argument | What it does |
| --- | --- |
| `[<name>]` | the profile to make the default |
| `--clear` | go back to choosing automatically |

#### charmer profile starter

Preview a starter profile of locally ready lines (--apply saves it after confirmation).

```text
charmer profile starter [--name <name>] [--apply]
```

| Argument | What it does |
| --- | --- |
| `--name <name>` | profile name to save (default: starter; an existing name is refused) |
| `--apply` | save the previewed profile after a y/N confirmation |

#### charmer profile migrate

Convert legacy compositions to profiles (a bare run is a dry run).

It reads or converts an earlier format; its own `--help` lists its options.

### charmer sessions

List, show, stop, forget, link and repair managed sessions.

```text
charmer sessions <command>
```

#### charmer sessions list

List managed sessions (a report; pick one to resume with charmer -r).

```text
charmer sessions list [--here | --all] [--ended] [--needs-choice] [--older-than <n>] [--json]
```

| Argument | What it does |
| --- | --- |
| `--here` | sessions recorded in this directory |
| `--all` | sessions in every directory |
| `--ended` | sessions whose last event is end |
| `--needs-choice` | sessions that need a lead choice |
| `--older-than <n>` | last_seen_at older than N days (unknown or malformed times excluded) |
| `--json` | one JSON document (stable fields; no transcript content) |

#### charmer sessions show

Show a managed session's record (JSON).

```text
charmer sessions show <id>
```

| Argument | What it does |
| --- | --- |
| `<id>` | its id, an id prefix of 8 or more characters, or its name |

#### charmer sessions forget

Forget a managed session: its record and generated scope (the transcript is kept).

```text
charmer sessions forget [<id>…] [--here | --all] [--ended] [--needs-choice] [--older-than <n>] [--dry-run] [--force] [--yes]
```

| Argument | What it does |
| --- | --- |
| `[<id>…]` | its id, an id prefix of 8 or more characters, or its name; or omit SESSION and select with filters |
| `--here` | sessions recorded in this directory |
| `--all` | sessions in every directory |
| `--ended` | sessions whose last event is end |
| `--needs-choice` | sessions that need a lead choice |
| `--older-than <n>` | last_seen_at older than N days (unknown or malformed times excluded) |
| `--dry-run` | preview the frozen selection; write nothing |
| `--force` | forget even when background liveness cannot be determined |
| `--yes` | skip the y/N confirmation (required without a terminal) |

#### charmer sessions choose

Save lead choices for the next resume, without launching.

```text
charmer sessions choose [<id>…] [--here | --all] [--ended] [--needs-choice] [--older-than <n>] (--profile <name> | --direct <model>) [--effort <effort>] [--dry-run] [--yes]
```

| Argument | What it does |
| --- | --- |
| `[<id>…]` | its id, an id prefix of 8 or more characters, or its name; or omit SESSION and select with filters |
| `--here` | sessions recorded in this directory |
| `--all` | sessions in every directory |
| `--ended` | sessions whose last event is end |
| `--needs-choice` | sessions that need a lead choice |
| `--older-than <n>` | last_seen_at older than N days (unknown or malformed times excluded) |
| `--profile <name>` | follow this profile at the next resume |
| `--direct <model>` | use this direct lead at the next resume |
| `--effort <effort>` | direct lead effort (default: ultracode) |
| `--dry-run` | preview pending choices; write nothing and never launch |
| `--yes` | skip the y/N confirmation (required without a terminal) |

#### charmer sessions link

Adopt a native session (must exist in local Claude metadata).

```text
charmer sessions link [<runtime-id>] [--profile <name> | --direct <model>] [--cwd <dir>]
```

| Argument | What it does |
| --- | --- |
| `[<runtime-id>]` | native session UUID |
| `--profile <name>` | adopt following this profile |
| `--direct <model>` | adopt as an ad-hoc direct session |
| `--cwd <dir>` | authoritative original project directory (validated against metadata) |

#### charmer sessions relink-runtime

Repair a managed record with the authoritative native runtime UUID.

```text
charmer sessions relink-runtime <id> <runtime-id> [--cwd <dir>]
```

| Argument | What it does |
| --- | --- |
| `<id>` | the managed session: its id, an id prefix of 8 or more characters, or its name |
| `<runtime-id>` | UUID shown by native /status or /resume |
| `--cwd <dir>` | also replace the recorded original project CWD |

#### charmer sessions resolve-fork

Discard a pending native-fork marker (the fork transcript is kept).

```text
charmer sessions resolve-fork <id> <fork-id> [--yes]
```

| Argument | What it does |
| --- | --- |
| `<id>` | the parent session: its id, an id prefix of 8 or more characters, or its name |
| `<fork-id>` | runtime UUID of the native fork to stop tracking |
| `--yes` | skip the y/N confirmation (required without a terminal) |

#### charmer sessions stop

Stop a live background-owned session (upstream `claude stop`; the conversation is always kept).

```text
charmer sessions stop <id> [--force] [--yes]
```

| Argument | What it does |
| --- | --- |
| `<id>` | its id, an id prefix of 8 or more characters, or its name |
| `--force` | send claude stop even when background liveness cannot be determined |
| `--yes` | skip the interactive confirmation (required non-interactively) |

#### charmer sessions mark-ended

Record a synthetic SessionEnd for a session you know has exited (refused while it is live in the daemon or a running process).

```text
charmer sessions mark-ended (<id> | --all-dead)
```

| Argument | What it does |
| --- | --- |
| `[<id>]` | its id, an id prefix of 8 or more characters, or its name |
| `--all-dead` | every record whose last event is not `end` and that no daemon socket or running process names |

### charmer providers

List, add, connect (API key or sign-in), test, turn on or off, apply and remove providers.

```text
charmer providers <command>
```

#### charmer providers list

List every provider, shipped and yours: source, on or off, connection and line counts.

```text
charmer providers list [--json]
```

| Argument | What it does |
| --- | --- |
| `--json` | one JSON document (stable fields; never a key value) |

#### charmer providers show

Show one provider declaration.

```text
charmer providers show <provider> [--resolved]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id (providers.d/\<id>.json) |
| `--resolved` | print the resolved provider and lines as JSON |

#### charmer providers validate

Validate providers.d (or one candidate FILE); writes nothing.

```text
charmer providers validate [<file>]
```

| Argument | What it does |
| --- | --- |
| `[<file>]` | a candidate \<id>.json checked with the current files |

#### charmer providers template

Print a providers.d template for a reviewed kind.

```text
charmer providers template [--kind anthropic-compatible|openai-compatible-lan|openai-compatible]
```

| Argument | What it does |
| --- | --- |
| `--kind anthropic-compatible\|openai-compatible-lan\|openai-compatible` | reviewed provider kind (default anthropic-compatible) |

#### charmer providers add

Declare a new provider, by hand or from a reviewed --preset (route approval follows unless --declare-only).

```text
charmer providers add [<provider>] [--preset <preset>] [--as <provider>] [--secret-file <file>] [--reuse-key | --replace-key] [--kind anthropic-compatible|openai-compatible-lan|openai-compatible] [--base-url <url>] [--auth bearer|header|none] [--header <header>] [--secret-ref <secret-ref>] [--family <family>] [--display <text>] [--contracts <contracts>] [--listing-url <url>] [--listing-auth provider|none] [--listing-shape anthropic|openai] [--declare-only]
```

| Argument | What it does |
| --- | --- |
| `[<provider>]` | new provider id (never a catalog id) |
| `--preset <preset>` | declare from a reviewed preset or sample (a template, never a grant) |
| `--as <provider>` | provider id for --preset |
| `--secret-file <file>` | import the key from a private 0600 file into the secret store (terminal only) |
| `--reuse-key` | for --preset when another provider already uses its API key: share that saved key (none is imported; by default the new provider gets its own key name) |
| `--replace-key` | for --preset when another provider already uses its API key: replace that shared key with --secret-file (asks first, naming every provider using it) |
| `--kind anthropic-compatible\|openai-compatible-lan\|openai-compatible` | generation protocol (default: anthropic-compatible, recommended for documented Messages endpoints; explicit preset kind wins) |
| `--base-url <url>` | provider base URL (https when keyed) |
| `--auth bearer\|header\|none` | credential transport |
| `--header <header>` | header name for --auth header (x-api-key only) |
| `--secret-ref <secret-ref>` | logical credential reference in the secret store (never the value) |
| `--family <family>` | family label (1–64 printable single-line characters; unrecognized labels do not establish independence) |
| `--display <text>` | display name |
| `--contracts <contracts>` | reviewed payload contracts it uses |
| `--listing-url <url>` | model listing URL (with --listing-shape) |
| `--listing-auth provider\|none` | listing authentication only (default: provider for keyed routes, none for LAN) |
| `--listing-shape anthropic\|openai` | model listing shape (with --listing-url) |
| `--declare-only` | write an inert, unapproved declaration (no approval, no provider call) |

#### charmer providers edit

Edit a declaration in $VISUAL/$EDITOR (validated before it is written).

```text
charmer providers edit <provider>
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |

#### charmer providers approve

Approve a declared provider's credential route (terminal, outside sessions).

```text
charmer providers approve <provider>
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |

#### charmer providers set-key

Set or replace a provider's API key (shipped providers and yours; terminal, outside sessions).

```text
charmer providers set-key <provider> [--secret-file <file>] [--yes]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |
| `--secret-file <file>` | read the key from a private 0600 file instead of a hidden prompt |
| `--yes` | replace a saved key without the y/N question (for every provider that uses it) |

#### charmer providers remove-key

Remove a provider's saved API key (terminal, outside sessions).

```text
charmer providers remove-key <provider> [--yes] [--name <key-name>]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |
| `--yes` | skip the y/N confirmation |
| `--name <key-name>` | the key's name, for a key kept after its provider was removed |

#### charmer providers sign-in

Sign in to your Claude or ChatGPT account in this terminal (personal use; you confirm that first).

```text
charmer providers sign-in anthropic|openai [--no-browser]
```

| Argument | What it does |
| --- | --- |
| `anthropic\|openai` | anthropic (Claude account) or openai (ChatGPT account) |
| `--no-browser` | print the address instead of opening a browser (Claude account) |

#### charmer providers sign-out

Sign out of your Claude or ChatGPT account (the records are kept as a backup).

```text
charmer providers sign-out anthropic|openai [--yes]
```

| Argument | What it does |
| --- | --- |
| `anthropic\|openai` | anthropic (Claude account) or openai (ChatGPT account) |
| `--yes` | skip the y/N confirmation |

#### charmer providers test

Send one small request to each named provider after one consent (may be billed).

```text
charmer providers test <provider>…
```

| Argument | What it does |
| --- | --- |
| `<provider>…` | provider id |

#### charmer providers enable

Turn a provider on for new sessions.

```text
charmer providers enable <provider>
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |

#### charmer providers disable

Turn a provider off for new sessions (running sessions keep theirs).

```text
charmer providers disable <provider>
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |

#### charmer providers rm

Remove a provider you added (refused while its lines are admitted, bound or live).

```text
charmer providers rm <provider> [--yes]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | provider id |
| `--yes` | skip the y/N confirmation (the API key is kept) |

#### charmer providers apply

Re-render the gateway config from the catalog and providers.d, verify the reload.

```text
charmer providers apply
```

#### charmer providers transport

Show or switch a catalog OAuth-pool provider's reviewed transport (same selectors; switching is a guarded route approval).

```text
charmer providers transport <provider> [oauth-pool|api-key] [--secret-file <file>]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | catalog provider id (e.g. anthropic) |
| `[oauth-pool\|api-key]` | the transport to use (omit to show the current one) |
| `--secret-file <file>` | import the key from a private 0600 file (api-key; terminal only) |

#### charmer providers migrate-custom

Migrate custom.json to providers.d (a bare run is a dry run).

It reads or converts an earlier format; its own `--help` lists its options.

### charmer models

Without a command (or with list): the models (generation, provider, class, efforts, status), their typed /model selectors and the retired keys. The other commands manage the models you add.

```text
charmer models [--candidates] [--all] [<command>]
```

| Argument | What it does |
| --- | --- |
| `--candidates` | advisory registry and served candidates (nothing declared or admitted) |
| `--all` | with --candidates: every registry channel and older candidates |

#### charmer models list

The listing (same as charmer models).

```text
charmer models list [--json]
```

| Argument | What it does |
| --- | --- |
| `--json` | one JSON document (stable fields) |

#### charmer models add

Declare a model of yours (New · not admitted; allowed anywhere).

```text
charmer models add <provider> <wire> [--as <new-id>] [--family <family>] --context <n> --source docs|operator|registry [--source-ref <text>] [--effort <effort>] [--default-effort <effort>] [--display <text>]
```

| Argument | What it does |
| --- | --- |
| `<provider>` | catalog or providers.d provider id |
| `<wire>` | upstream model id |
| `--as <new-id>` | line key (derived from WIRE; custom- reserves local ids against future catalog keys) |
| `--family <family>` | model family label (overrides the provider default; no inference) |
| `--context <n>` | declared context tokens |
| `--source docs\|operator\|registry` | where the context figure comes from |
| `--source-ref <text>` | URL/date for --source docs\|registry |
| `--effort <effort>` | declared effort (repeatable; with contracts a map, else a list) |
| `--default-effort <effort>` | default effort (a declared one) |
| `--display <text>` | display name (chosen freely; defaults to WIRE) |

#### charmer models admit

Record an optional local admission badge (zero inference; terminal, outside sessions).

```text
charmer models admit <line>
```

| Argument | What it does |
| --- | --- |
| `<line>` | line key |

#### charmer models revoke

Remove only the admission badge (use and qualification unchanged).

```text
charmer models revoke <line> [--yes]
```

| Argument | What it does |
| --- | --- |
| `<line>` | line key |
| `--yes` | skip the y/N confirmation (required without a terminal) |

#### charmer models edit

Edit a model of yours in $VISUAL/$EDITOR (shows the admission consequences).

```text
charmer models edit <line>
```

| Argument | What it does |
| --- | --- |
| `<line>` | line key |

#### charmer models rm

Remove a line you added (captured aliases stay served until pruned).

```text
charmer models rm <line> [--successor <line>] [--yes]
```

| Argument | What it does |
| --- | --- |
| `<line>` | line key |
| `--successor <line>` | rewrite the profiles and named bindings that name KEY to this offered line |
| `--yes` | skip the y/N confirmation |

#### charmer models show

Show a line (status, selectors, class, evidence).

```text
charmer models show <line> [--resolved] [--evidence]
```

| Argument | What it does |
| --- | --- |
| `<line>` | line key |
| `--resolved` | print the resolved entry as JSON |
| `--evidence` | print the tool-owned evidence as JSON |

#### charmer models qualify

Run the optional, consented qualification battery on a model of yours (its own aliases) through the loopback gateway. Every request is listed before one y/N; no retries; 120 s and 256 KiB per request (context 300 s). Evidence only: it never admits a line or edits its declaration. No flag means --smoke. Your models only (shipped models carry reviewed evidence).

```text
charmer models qualify <line> [--smoke] [--efforts] [--tools] [--tool-choice forced|auto] [--stream] [--context <n>] [--agents]
```

| Argument | What it does |
| --- | --- |
| `<line>` | the key of a model you added |
| `--smoke` | one minimal generation at the default effort |
| `--efforts` | one minimal generation per declared effort |
| `--tools` | a strict-schema tool round trip (up to 2 requests; the second only after the expected tool call) |
| `--tool-choice forced\|auto` | --tools with a forced named tool (default) or a strict automatic tool; disclosed before consent, never retried the other way |
| `--stream` | one streamed generation ending in message_stop |
| `--context <n>` | a synthetic retrieval at about N input tokens (8192..declared; 300 s) |
| `--agents` | --smoke --efforts --tools --stream (not --context); first-party pool lines also run the offline exact-client check |

### charmer discover

List the models a provider advertises (PROVIDER), every enabled direct provider (--all), or compare the public feed with the pinned registry (--feed). Provider calls run only in a terminal outside Claude Code sessions, after one y/N naming every request.

```text
charmer discover [<provider>] [--all | --feed] [--add <wire>…] [--as <new-id>] [--family <family>] [--context <n>] [--over-listed <text>]
```

| Argument | What it does |
| --- | --- |
| `[<provider>]` | provider id (see `charmer models`) |
| `--all` | list every enabled direct provider with a supported listing (observation only; `discover openai` stays separate) |
| `--feed` | compare the public model feed with the pinned registry (advisory) |
| `--add <wire>…` | declare listed WIRE(s); admission and diagnostics are optional |
| `--as <new-id>` | line key for exactly one --add WIRE (custom-... reserves local ids against future catalog keys) |
| `--family <family>` | model family label for exactly one --add WIRE (overrides the default; no inference) |
| `--context <n>` | declared context when the listing states none, or an explicit override |
| `--over-listed <text>` | required to declare --context above the listing-stated value |

### charmer lineup

Show or change a session's lineup from a terminal (what /cm runs in a session).

```text
charmer lineup [--session <runtime-id>] [--relaunch] [--its-exited] <request>
```

| Argument | What it does |
| --- | --- |
| `--session <runtime-id>` | runtime session id (/cm passes it) |
| `--relaunch` | relaunch instead of a live apply |
| `--its-exited` | the session has exited (no confirmation) |
| `<request>` | lineup request: show, profiles, profile, set, unset, direct, pin, follow, fallback, review, quota |

The requests are `/cm`'s ([inside a session](#inside-a-session)); inside a session the runtime id comes from the environment.

### charmer window-ceiling

One ceiling governs the lead and every agent: a session's window is the smaller of it and the lead set's smallest provider bound, and agent lines whose bound is below that window keep the 200K class. Without arguments it shows the ceiling and whether it is set or the default.

```text
charmer window-ceiling [<ceiling> | --reset]
```

| Argument | What it does |
| --- | --- |
| `[<ceiling>]` | the new ceiling in tokens (400000) or thousands (400K), from 200K to 800K |
| `--reset` | go back to the default ceiling (800K) |

### charmer gateway

Manage the local gateway. It starts on demand when a session needs it; stop and restart act only on a gateway proven to be yours and never while a credential save may be unresolved.

```text
charmer gateway <command>
```

#### charmer gateway start

Start the gateway if it is not running and wait until it is ready.

```text
charmer gateway start
```

#### charmer gateway stop

Stop your gateway (refused while the persistence hold is active).

```text
charmer gateway stop
```

#### charmer gateway restart

Stop, then start your gateway (same checks as stop).

```text
charmer gateway restart
```

#### charmer gateway status

Backend, endpoint, instance, log and persistence hold (sends no token).

```text
charmer gateway status
```

#### charmer gateway logs

The newest gateway log lines (or one instance's).

```text
charmer gateway logs [-n <n>] [--instance <nonce>]
```

| Argument | What it does |
| --- | --- |
| `-n <n>`, `--lines <n>` | how many lines (default 50) |
| `--instance <nonce>` | the instance id shown by `charmer gateway status` |

#### charmer gateway clear-hold

Clear the persistence hold after you verified the credentials (a terminal outside Claude Code; typed confirmation; the gateway's own recovery for a hold its log reads could not settle).

```text
charmer gateway clear-hold
```

#### charmer gateway ensure

For scripts: exit 0 only when your gateway runs and is ready (starts it when stopped).

```text
charmer gateway ensure [--quiet] [--max-wait <n>] [--base-url <url>]
```

| Argument | What it does |
| --- | --- |
| `--quiet` | print nothing on success |
| `--max-wait <n>` | wait at most this long (default 45) |
| `--base-url <url>` | refuse unless the gateway's configured endpoint is URL (a session's compiled ANTHROPIC_BASE_URL) |

#### charmer gateway service

Hand the gateway to a hardened systemd user unit that restarts it on failure, or back to the on-demand start. Both directions stop the running gateway only after the persistence hold, and restore the previous backend when a step fails.

```text
charmer gateway service <command>
```

##### charmer gateway service install

Install (or refresh) the unit and hand the gateway to it.

```text
charmer gateway service install [--name <unit>]
```

| Argument | What it does |
| --- | --- |
| `--name <unit>` | the unit's name (default: charmer-gateway) |

##### charmer gateway service uninstall

Remove the unit and hand the gateway back to the on-demand start.

```text
charmer gateway service uninstall
```

##### charmer gateway service status

Whether the service is installed, its name, backend and unit state.

```text
charmer gateway service status
```

### charmer update

Update charmer itself: check, show the plan, ask, install (installed releases), or switch back to the previously installed version. --verify instead checks the exact installed release against signed metadata without changing anything (online unless --from-dir is given). Verification exits: 0 complete, 1 mismatch, 3 incomplete or unsupported; 2 usage, 130 interrupted.

```text
charmer update [--check | --rollback | --verify] [--json] [--yes] [--from-dir <dir>] [--base-url <url>]
```

| Argument | What it does |
| --- | --- |
| `--check` | only check whether a newer release exists |
| `--rollback` | switch back to the previously installed version |
| `--verify` | verify this exact installed release; no repair, restart or state writes |
| `--json` | --verify only: one verification object on stdout; disclosure on stderr |
| `--yes`, `-y` | apply without asking (needed without a terminal; not with --check or --verify) |
| `--from-dir <dir>` | use the release files in a local directory instead of downloading |
| `--base-url <url>` | update/check only: download from this https location instead of the release's own |

### charmer export

Write your portable configuration (JSON on stdout by default): no secrets, keys, sessions, logs or evidence; trust travels as inert re-approval requests.

```text
charmer export [--out <file>]
```

| Argument | What it does |
| --- | --- |
| `--out <file>` | write FILE atomically (0600) and record a confirmed-export receipt |

### charmer import

Preview a portable export against this computer (writes nothing); --apply writes the ready items; imported trust is never active.

```text
charmer import <file> [--apply]
```

| Argument | What it does |
| --- | --- |
| `<file>` | a `charmer export` JSON file |
| `--apply` | confirm and apply the ready items shown (a terminal outside Claude Code) |

## Recovery and earlier formats

### charmer migrate

Convert pre-public record formats v1–3 (written before charmer 1.0.0) to the current format, keeping byte-exact backups for restore-2x; --dry-run writes nothing; migrate from-legacy moves an installation of the earlier name onto charmer.

It reads or converts an earlier format; its own `--help` lists its options.

### charmer restore-2x

Restore the pre-public record backups made by migrate; not a release rollback (use charmer update --rollback).

It reads or converts an earlier format; its own `--help` lists its options.

### charmer custom

The earlier custom-model registry (move it to providers: charmer providers migrate-custom).

It reads or converts an earlier format; its own `--help` lists its options.

### charmer uninstall

Remove charmer from this computer. The plan is shown first; your credentials go only after a typed confirmation; kept backups and your Claude Code are never removed.

```text
charmer uninstall [--dry-run] [--keep-setup] [--keep-credentials] [--yes] [--force]
```

| Argument | What it does |
| --- | --- |
| `--dry-run` | show the plan and remove nothing |
| `--keep-setup` | keep your setup and session state (profiles, settings, records) |
| `--keep-credentials` | keep your API keys, sign-ins and the local gateway key without asking |
| `--yes` | skip the y/N confirmation (never the typed one for credentials) |
| `--force` | remove even when sessions may still be running (they are named) |

## The gateway tool

`charmer-proxy` is the local gateway's own tool. The launcher and the gateway service run it; a doctor finding names it when it is the fix.

| Command | Arguments | What it does |
| --- | --- | --- |
| `charmer-proxy init` | `[--reload-check \| --start-check \| --prepare-start] [--state-root /abs]` | Prepare the gateway's configuration, key and directories and verify the hot reload (up to 2 s). |
| `charmer-proxy run` | `[--prepared \| --prepare-and-exec [--detach]] [--instance NONCE] [--state-root /abs]` | Exec the gateway, holding the single-instance lock across exec. |

`charmer-proxy --help` lists its 6 maintenance commands too, and the exit statuses of every command.

## Inside a session

`/cm` shows or changes the session's lineup. A change applies after you type `/reload-plugins`, or at the next resume when it needs a relaunch.

| Request | What it does | Changes the lineup |
| --- | --- | --- |
| `/cm show` | this session's lineup (also the empty request) | no |
| `/cm profiles` | the profiles this session can switch to | no |
| `/cm profile <name>` | follow another profile | yes |
| `/cm set <agent>=<model>[:<effort>]` | bind one agent | yes |
| `/cm unset <agent>` | unbind one agent | yes |
| `/cm direct [<model>[:<effort>]]` | a lead with no agents | yes |
| `/cm pin` | keep the current lineup and stop following the profile (drops a pending change) | yes |
| `/cm follow` | follow the session's profile again | yes |
| `/cm fallback <provider> [--preview]` | move the roles bound to a provider to a fallback lineup | yes |
| `/cm review [high-stakes] [<range>]` | ask for a review from another model family | no |
| `/cm quota` | the quota of this session's accounts | no |

`/cm` passes everything after it as one request; `charmer lineup --session <runtime-id> <request>` runs the same request from a terminal.

<!-- end of generated: cli-reference -->

## The doctor report

`charmer doctor --json` writes one read-only document (`schema_version`
1): its `status` (`ready`, `attention` or `blocked`), `coverage`, `facts`,
`diagnostics` and the `environment` block of allowlisted facts. Each
diagnostic has:

| Field | What it is |
| --- | --- |
| `severity` | `block`, `attention`, `info` or `advisory`: optional evidence (an admission badge, qualification, a recommendation an explicit binding overrides), which the status ignores, so a report whose only findings are advisories is `ready` |
| `code` | a stable kebab-case code: a typed finding's own; otherwise the action code of a finding the TUI's findings list acts on (`hold`, `hold-coverage`, `missing-key`, `earlier-scopes`, `scope-repair`, `unused-alias`, `prune`, `dead-sessions`, `seed-update`, `advisory-model`, `needs-choice`) or a model finding's code (`admission`, `qualification`, …), else `legacy-line` |
| `text` | what it is about, without a path, a value or an error body (plain `charmer doctor` prints the full line and its fix) |
| `subject_id` | the session, model or alias it names, when that is a safe identifier, or null |
| `remedy` | its one-line command, or null |
| `area` | where plain doctor lists it: `Gateway`, `Installation`, `Providers`, `Sessions`, `Profiles`, `Models` or `Other`, or null |

Plain `charmer doctor` prints the same findings: BLOCKED, then Attention by
area (a persistence hold first), then **Advisories (optional evidence; does
not change the verdict)**, each repeated finding collapsed into one line;
`-v` prints every original line ([troubleshooting](../troubleshooting.md)).
