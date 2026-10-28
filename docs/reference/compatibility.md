# Compatibility

## Claude Code

Each charmer release runs exactly one Claude Code version: the one
its release pins, verified for every platform the release builds. This
release pins **Claude Code 2.1.312**. `charmer --version` names the
pinned version, and doctor shows the pin, when it was verified, and the
evidence class for your platform.

### How the copy is acquired

Only charmer's own copy runs: `~/.local/share/charmer/claude/<version>/claude`.
Setup's `claude` step (`charmer setup --step claude`) puts it in
place, trying in this order:

1. `--claude-from <path>`, a file you name;
2. a copy an earlier release retained;
3. your Claude Code versions directory (`~/.local/share/claude/versions`,
   or under `XDG_DATA_HOME`) and the `claude` on your PATH;

taking a file only when its size and sha256 match the pin for your
platform (the values come from Anthropic's signed release manifest). When
none matches, it shows the one download it would make from
`downloads.claude.ai` (the address and size; no credentials are sent) and
asks; no sends nothing. An interrupted download resumes, three tries at
most. Anthropic's installer is never run, and your own installation is
never moved, linked or changed.

### Refusal and checks

- Every launch checks the copy's size and full sha256 before anything
  else happens, and refuses a missing or changed copy with the fix:
  `charmer setup --step claude`.
- The launch card checks only the copy's size and metadata, so a damaged
  copy of the right size can look ready there; doctor and every launch
  check the full hash. This difference is deliberate.
- There is no way to run a client that does not match the pin.
- A copy a running session uses is locked; setup and updates keep it.

### Newer Claude Code versions

A newer Claude Code reaches managed sessions with a charmer release
that verifies it: releases repin as patch releases, on a regular cadence
and when a client change requires it. Until then doctor shows Attention
when the pin was verified more than 30 days ago or your own Claude Code
is newer; there is nothing to run. Managed sessions disable Claude Code's
own updater, so the managed copy never changes by itself, while your own
`claude` keeps updating as usual.

### Refusal fallback

Claude Code 2.1.312 can switch a refused turn of a model it does not
recognise to Opus 4.8 (cyber) or Opus 5 (bio, frontier-LLM) when the session
allows them, which every shipped lineup with an Anthropic line does. Managed
sessions turn that switch off for each such model in their compiled settings
(`CLAUDE_CODE_MODEL_CAPABILITIES`, one `-refusal_fallback` entry per fenced
non-Anthropic selector, compiled for every launch and resume). Those settings
outrank your user settings, and project and local settings cannot set the
variable. A refused GPT, Kimi or OpenRouter turn — lead, agent, workflow agent
or resumed — ends on its own model with the refusal. Canonical Opus 5.5,
Fable 5.1 and Sonnet 5.5 keep the client's switch. `switchModelsOnFlag:
false` and `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1` stop every switch,
canonical ones too. No refusal switch runs the PreModelSwitch hook. The
setting never enters your own settings or plain `claude`. 2.3.0 held this
client back for that switch; the counter is why 2.4.0 admits it.
`CLAUDE_CODE_MODEL_CAPABILITIES` is not documented upstream; charmer relies on
its measured behaviour on the pinned client and re-measures it at every
re-pin, as required re-pin evidence.

### Deferred client fixes

The pin retains the fixes for interrupted HTTP MCP calls. When the
connection to an HTTP MCP server closes before a tool call is answered,
Claude Code 2.1.312 does not send the call again automatically unless the
server marks the tool read-only or idempotent. A tool marked neither is
called once; a tool that changes something but is marked idempotent can
still be called twice. An interrupted call still reports an error, and the
tool may have run: check the external system before asking for it again.

The pin also carries the upstream fixes from Claude Code 2.1.304–2.1.309 for
native sandbox boundaries (for example shell start-up files running outside
the sandbox, or a writable `--mcp-config` file), worktrees (`--worktree`
removing a worktree and its branch without asking) and Bash permission checks.
It also carries the Linux sandbox fix 2.1.311 brought for overlapping added or plugin
directories with `blockReadsOutsideWorkingDirectories`.
charmer has not tested those fixes. Do not rely on the client's Bash sandbox
as the only boundary for untrusted code; charmer does not replace Claude
Code's sandbox or worktree protections.

### Client skew

Claude Code's background supervisor can keep a backgrounded managed
session running on your own, newer `claude`. Its hooks then tell you on
every prompt to `/exit` and resume it with `charmer -r <id>`. One
limit: when a session already carries its lineup marker, the per-prompt
check can stay quiet after an upgrade (or while detection is unsure)
until the session's next start; doctor and the next resume report it.

A Claude Code settings file with keys the pinned version does not know (a
newer Claude Code wrote them) makes a launch use the default permission
mode, naming the keys.

### Verified behavior and limits

The Linux client probes use isolated fixture providers, not real accounts.
They verify agent discovery and reload, lifecycle notices, model-switch
fences, effort transmission, helper-token rotation, and compaction. They do
not establish supervisor takeover or a real provider's acceptance of large
contexts or signed reasoning history.

- Agent frontmatter effort is a default: an explicit `effort` on an Agent
  call overrides it and the session effort. Low through max frontmatter
  values reach the wire; `ultracode` in frontmatter is silently ignored by
  the client, so charmer rejects it there. A lead's `ultracode` sends
  `xhigh` on the wire.
- Canonical Anthropic models (Opus 5.5, Fable 5.1, Sonnet 5.5, Haiku 5.5)
  book a 1M client window with or without `[1m]`; from Claude Code 2.1.311
  neither form sends the long-context beta. A gateway model name needs
  `[1m]` for the 1M window and its beta. Canonical Opus 5.5 sends a 128K
  output cap on the first turn, both at startup and after a model switch.
  The fixture's advertised 300K/16K limits are not fetched through the
  custom endpoint; this is not proof of server-limit negotiation or
  provider acceptance.
- Managed sessions re-enable compaction against `DISABLE_AUTO_COMPACT` in
  your user settings. Doctor reports that override as Info: managed
  sessions compact, while plain `claude` does not. `DISABLE_COMPACT` in
  your user settings env still stops managed compaction on 2.1.312;
  doctor reports Attention with the remedy to remove it, then resume.
- Hook lineup notices survive startup, resume, compaction and prompts,
  including markup-looking text. The client escapes embedded
  `system-reminder` tag openings; it does not discard the notice. The
  Bash-queue delay reported in
  [upstream #105012](https://github.com/anthropics/claude-code/issues/105012)
  was not reproduced with charmer's real compiled lifecycle hooks in
  isolated testing on 2.1.312; this is not a general latency guarantee.
- Claude Code 2.1.312 limits prompt and agent hooks (not command hooks) to
  24 concurrent evaluations per session; in isolated testing at most 12 ran
  at once on 2.1.311 and 2.1.312. charmer's own hooks are command hooks.
- From 2.1.312 an agent definition's own `autoCompactWindow` can lower that
  agent's window, never raise it and not below 100K; `cm-*` agents carry
  none.
- After a lineup change, spawn fresh agents. Continuing an old agent with
  SendMessage can use its new model with its old prompt and history.
- A worktree-isolated agent that finishes without changes loses its
  worktree; continuing it with SendMessage then runs it in the main
  checkout without isolation
  ([upstream #104928](https://github.com/anthropics/claude-code/issues/104928),
  reported on clients since 2.1.292, including 2.1.312). The lead continues
  an isolated agent only while its worktree exists; otherwise it dispatches
  a fresh agent with the partial results.
- Worktree isolation refuses commands that name shell words such as `hash`,
  `source`, `alias` or `complete` anywhere in their arguments
  ([upstream #95611](https://github.com/anthropics/claude-code/issues/95611)).
- `/effort` also saves the effort as `modelSettings.<model>.effortLevel` in
  your user settings, where every Claude Code session on that model that
  states no effort starts with it; charmer's launches and resumes state
  theirs ([upstream #105036](https://github.com/anthropics/claude-code/issues/105036)).
- Session scratchpad directories remain after a session ends
  ([upstream #104612](https://github.com/anthropics/claude-code/issues/104612)).
- When several MCP calls whose answers are too large to show inline finish
  together, Claude Code can hand more than one of them the same saved result
  file, so a call can see another call's answer
  ([upstream #105138](https://github.com/anthropics/claude-code/issues/105138);
  reproduced on 2.1.312).
- Claude Code can run every workflow agent on one model
  (`CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL`), replacing the models of `cm-*`
  agents too. Managed sessions set it to `inherit` in their own settings,
  which outrank your user, project and local settings, so that environment
  override cannot replace their lineup models. A managed policy that sets
  it is reported like any policy model lock. Installed mods are a separate
  boundary, described below.
- User-installed `agent.spawn` mods can move a fresh workflow agent to
  another model of the lineup. Against these mods, charmer guarantees fence
  membership, **not the exact per-agent model binding**: no outside-fence
  model reaches the wire. A resumed agent ignores a mod's model rewrite
  and keeps the model it started with, even if a mod redirected that fresh
  spawn. A managed `allowManagedHooksOnly` policy keeps mods out, but also
  disables charmer's hooks; charmer reports it as a managed lock, not a
  protection its session scope can supply. On 2.1.312, the client checks
  retain both the passing fence check (`spawn-mod: PASS class=SM-fenced`)
  and the known binding failure (`spawn-mod-binding: FAIL class=SB-rebound`).
- From Claude Code 2.1.304 a mod's `settings.read` hook can rewrite the
  session's merged settings, charmer's own included. In isolated testing on
  2.1.310 (not repeated on 2.1.311 or 2.1.312), a user-installed mod that added a model
  to `availableModels` let an agent whose definition names that model run
  on it, outside the lineup; on 2.1.302 the same mod changed nothing. A
  permission deny rule still applied. Against a mod that rewrites settings, fence membership is not
  guaranteed either; only a managed `allowManagedHooksOnly` keeps such mods
  out, and charmer's hooks with them.
- Fork subagents (a copy of the calling agent on its own model and context)
  are not lineup agents: managed sessions deny them (`Agent(fork)`) and
  set `CLAUDE_CODE_FORK_SUBAGENT=0` in their settings, while clearing any
  inherited value at launch. This turns the fork gate off even against
  user, project or local settings that enable it. In a terminal, ordinary
  subagents may run in the foreground; the background remains the default.
- An MCP permission rule `mcp__<server>__*` grants only that named server,
  not neighboring names that overlap after normalization or contain `__`.
  For a plugin server, a managed `deniedMcpServers` rule uses
  `plugin:<plugin>:<server>`, not the bare server name. The isolated checks
  verify denial before server startup at launch, subagent spawn, resume
  and plugin reload.
- `claude-code-guide` and the client's background requests run on Haiku 5.5
  when the lineup binds the `haiku` model, otherwise on the lead. Managed
  sessions pin `ANTHROPIC_DEFAULT_HAIKU_MODEL` to the lineup's Haiku line,
  else the lead, so a value in your own or your project's settings cannot
  send these requests outside the session's models.
- In isolated testing the gateway key reached only the gateway: none of the
  client's requests to Anthropic or its other hosts carried it, with the
  managed settings or without them, including a managed-settings gateway
  URL and a token-helper refresh after a 401. Claude Code 2.1.292 and
  earlier sent it with their start-up and feature-flag requests to
  Anthropic. See
  [privacy.md](../privacy.md#data-flows).
- Behind charmer's gateway the client sends the session's effort level
  with every request, and the gateway accepts it. Claude Code 2.1.312 would
  send a request again (first without its thinking-token-count beta), then
  try without the effort level, `context_management`, tool search or its
  in-conversation system turns if a gateway refused them; in isolated
  testing charmer's gateway accepted each on every route checked.
- Behind the gateway the client offers tool search: some built-in tools
  (for example SendMessage, WebFetch, WebSearch and the worktree and cron
  tools) and MCP tools are listed by name, and the model fetches one with
  ToolSearch when it needs it. The gateway passes fetched tools on.
- On Claude Code 2.1.311 (not re-measured on 2.1.312), `--tools` on a resumed turn no longer narrows the
  tools offered to the model: the request keeps the session's full tool
  list, though a call to a tool outside `--tools` is still refused and does
  not run. `--disallowedTools` still removes a tool from the list on resume.
  charmer never passes `--tools` itself; only a `--tools` you pass through to
  Claude Code when resuming is affected.
- When a provider refuses a request with an error the client does not
  recognise, Claude Code 2.1.312 sends it up to five more times, the first
  without its thinking-token-count beta, before it shows the error.
- A synthetic session made by 2.1.312, including a tool turn and manual
  compaction, resumes on the previous pin, 2.1.311, from its summary without
  replaying the old tool turn. This does not verify every operator session
  or background-supervisor rollback.
- Qualification evidence recorded against an older client remains stored
  with that identity and becomes stale; a re-pin does not rerun paid
  qualification automatically.

## Agent context

The lead and every agent on a 1M-class line run at one window: the
smaller of the window ceiling (800K by default, 200K–800K) and the
smallest provider bound among the lead set. An agent line whose provider
bound is below that window runs in the 200K class. See
[guides/models.md](../guides/models.md#context-windows).

## Platforms

| Target | Channel | Status |
| --- | --- | --- |
| Linux x86_64 | release bundle, Nix | supported; native release-bundle journeys and the Linux release battery |
| Linux aarch64 | release bundle, Nix | supported; native release-bundle journey on Ubuntu 24.04 arm64 |
| macOS, Apple silicon | release bundle, Nix | supported; native release-bundle journey on macOS 15; on-demand gateway only |
| macOS, Intel | release bundle only | built and reproduced, no native journey; no support claim |
| Windows | the Linux bundle inside WSL2 | supported inside WSL2; Windows Server 2025 with Ubuntu 24.04 and 26.04; native Windows is not supported |

A platform is supported only with a completed native install journey in
the release's evidence. A cross-build or a test on another platform does
not establish support. These native journeys exercise release bundles;
listing Nix as a channel does not claim a Nix installation journey.

The installer enforces macOS 13 as the minimum for a new installation of
charmer 2.1.0 or newer. No runtime support is claimed for macOS 13 or 14;
the native Apple silicon journey runs on macOS 15. On macOS 12, stay on
2.0.1 until upgrading the OS; its updater does not enforce the new floor.
See [installing on macOS](../install/macos.md#architectures-and-evidence).

## Full-screen terminal input

The full-screen interface requires both input and output to be terminals with
verifiable eight-bit settings: `CS8` set and `ISTRIP` clear before its first
curses initialization. They may be different terminals; both must qualify.
The input terminal controls incoming bytes, while curses can derive its byte-input
meta mode from the output terminal. charmer checks both again on entry and after
returning from an external editor. It does not change those flags or enable
terminal meta mode, which can change Alt-key behavior. This is a compatibility
restriction: a terminal initially using
`CS7` with `ISTRIP` clear could work with the previous wide-character reader.
An already initialized curses screen must have been initialized by charmer;
a failed or partial initialization is not reused as a verified screen.

When this requirement cannot be verified, charmer prints a notice before
starting the full-screen interface and does not automatically query the
terminal's background color. Launch and resume paths retain their line flows;
profile new/edit retains its external-editor fallback. Direct callers of the
curses wrapper can receive an error instead. An input failure after a screen
starts keeps that operation's existing cancellation and saved-change behavior.

Text is decoded strictly using the active locale encoding. Incomplete characters
survive timed waits, resize and an external-editor suspension; malformed input
fails rather than inserting replacement characters. Idle waits do not redraw.
Five consecutive materially early native input errors stop the screen after
paced retries. This is a finite recovery policy, not an EOF diagnosis: repeated
indistinguishable benign interruptions can also exhaust it.

Ordinary input waits use 100 ms slices and lookahead uses an 80 ms budget. These
are not universal native-call deadlines: curses can wait for each missing byte
of a function-key sequence using `ESCDELAY`. The default remains 25 ms, and an
explicit `ESCDELAY` is honored rather than clamped. Alt chords remain Esc followed
by their character. Both terminals' attributes are snapshotted after stream
redirection and restored before either redirected file descriptor. Cleanup
attempts the remaining restorations even if an earlier step raises, preserving
the original failure. No native or kernel input queue is flushed during cleanup.

## Transports

| Route | Status |
| --- | --- |
| Anthropic Messages to the built-in keyed providers (Anthropic API key, DeepSeek, Kimi, Meta, OpenRouter, Qwen) | supported |
| the OpenAI Platform API key, through the gateway's route to OpenAI's Responses API | supported for the models reviewed for it; the client's per-request output cap is passed on as the request's output limit ([providers/api-keys.md](../providers/api-keys.md#openai)) |
| Claude and ChatGPT account sign-ins | supported, for personal use |
| Anthropic-compatible endpoints of your own, and the Anthropic-compatible presets | supported; a preset is the vendor's documented endpoint, not tested by charmer |
| keyless OpenAI-compatible servers on your network, and their presets | supported, for leads and agents |
| keyed OpenAI-compatible endpoints, and the presets that use them | supported in this release after the complete compatibility proof; HTTPS and bearer-only, with [translation limits](../providers/openai-compatible.md#translation-limits); presets are not vendor-tested |

Anthropic and OpenAI use either their account sign-in or their API key,
never both at once, and never fall back from one to the other.

## Quota readings

Release bundles and the Nix package provide the same passive account-quota
readings in `charmer quota`, `/cm quota` and the Providers screen. They use
the matching gateway shipped with the installation; source checkouts do not
provide readings. Updating a bundle leaves the running gateway unchanged:
use the guarded `charmer gateway restart` before expecting readings. Disabled
readings stay disabled. Quota is an observation, not a remaining balance;
missing data never means zero use. See [quota readings](../guides/gateway.md#quota-readings).

## Managed policy

A Claude Code managed policy (`/etc/claude-code`, `/Library/Application
Support/ClaudeCode` or `C:\Program Files\ClaudeCode`) can make managed
sessions impossible: a launch refuses when the policy sets version bounds
that exclude the pin, restricts providers without the gateway as a
pinned custom endpoint, sets its own `apiKeyHelper` or a forced login
organization, turns hooks off, or denies a bound model. Ask the policy
owner; charmer never edits policy.
