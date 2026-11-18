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
between turns (`charmer gateway restart`). While its persistence hold is
active, an update or rollback that changes the gateway is refused.

### Updating from 2.6.0

charmer 2.6.1 keeps the gateway on CLIProxyAPI 8.0.76, now with 31 local
patches, built with Go 1.27.4. The gateway's bytes change, so this update
needs a gateway restart. It fixes the [empty one-shot result on
OpenRouter](providers/openrouter.md#one-shot-results). Catalog 46 adds
Gemini 4 Neon as `or-gemini-neon`; no shipped profile changes. Claude Code
stays at 2.1.326; state and record formats stay 4.

In a plain terminal outside Claude Code, between turns, run each step only
after checking the previous result:

```bash
charmer update
charmer --version
charmer gateway status
charmer gateway restart
charmer update --verify
charmer doctor
```

**If the update is held**, 2.6.0's updater refuses before it switches:

> charmer update: `the gateway cannot be replaced now: the gateway's persistence hold is active: <reason> — <remedy>; nothing was changed`
>
> `fix: keep the gateway running until charmer gateway status shows the hold cleared, then run the update again`

A stopped turn or an expired request can leave a failed credential save,
most often on Codex. Keep the gateway running. Wait for a persisted save of the same
credential; sign in again only if none comes. If the hold remains, verify
the credentials with `charmer providers list`, then run
`charmer gateway clear-hold` in a plain terminal between turns and type
`clear-hold` to confirm. When `charmer gateway status` shows no hold, run
`charmer update` again. The update does not remove this limit.

**After a successful update**, a running gateway still uses 2.6.0's bytes.
Status shows `gateway restart pending` for the binary. There is no new
restart row for start arguments or the environment filter. The guarded
restart checks ownership and the hold again. If it is held, leave the
gateway running, follow [the persistence hold](guides/gateway.md#the-persistence-hold)
and retry the restart, not the update. On Linux, `charmer update --verify`
reports `running-gateway: different-running-build` before the restart and
`running-gateway: current-match` after it; on macOS this check reports
`unavailable`.

Until you admit Neon, doctor shows the informational advisory
`New model: Gemini 4 Neon · OpenRouter (or-gemini-neon) — not admitted (optional badge)`
on every installation, even without an OpenRouter key; admission is optional.

If you declared `google/gemini-4-neon` on OpenRouter yourself, your line
yields to `or-gemini-neon`. Remove it with the command doctor gives, for
example `charmer models rm custom-google-gemini-4-neon --successor or-gemini-neon`
([models you added that now ship](#models-you-added-that-now-ship)).

#### Before going back to 2.6.0

If 2.6.0 is the retained release, `charmer update --rollback` checks your
state against its catalog before it asks. 2.6.0 has no `or-gemini-neon`
line. The check lists profiles, named bindings, the Settings workflow
default and running sessions that use Neon. For example:

```text
Profiles and named bindings 2.6.0 cannot evaluate (docs/update.md#rolling-back):
  - Settings workflow_default_binding.model: unknown model key 'or-gemini-neon' — change or clear the workflow default in Settings (charmer, then O)
  - bindings.neon-review.model: unknown model 'or-gemini-neon' — rebind it in the profile editor (charmer, then P)
  - profile neon: lead.model: unknown model 'or-gemini-neon' — charmer profile edit neon
Running sessions on selectors 2.6.0 does not serve (docs/update.md#rolling-back):
  - session 88888888 uses charmer-or-gemini-neon-xhigh, which 2.6.0 does not serve — exit it first (end the session; after the rollback, charmer -r 88888888-8888-4888-8888-888888888888 resumes it on 2.6.0's models)
```

With a finding, the default is No and `--yes` refuses. Rebind the listed
profiles and bindings, change or clear the workflow default (**O** on the
card), and exit sessions running on Neon. The printed resume hint does
not cover a saved lineup that still names Neon: exiting does not change
that lineup. Before going back, choose a profile that 2.6.0 can use. On
the card press **S**, select the session, then press **T** to change its
lineup; or run `charmer lineup --session <runtime-id> profile <name>`.
Then run `charmer update --rollback` again.
A persistence hold is separate from these findings: confirming the
rollback does not waive it.

After the rollback, run the guarded `charmer gateway restart` under 2.6.0
between turns. This restores 2.6.0's handling of OpenRouter replies.
Check `charmer update --verify`, then run `charmer doctor --repair-all`.
State and record formats stay 4.

### Updating from 2.5.0

The changes below describe the update from 2.5.0 to 2.6.0. If updating
straight to 2.6.1, also see [updating from 2.6.0](#updating-from-260).

charmer 2.6.0 moves the gateway to CLIProxyAPI 8.0.76 with 30 local patches,
built with Go 1.27.4, and Claude Code from 2.1.322 to 2.1.326. Catalog 45
ships with 2.6.0; the bundled CPython stays 3.15.2
(python-build-standalone 20261110). The gateway's bytes change, so this
update needs a gateway restart. Updating switches the installation, not the
running gateway. Between turns, in a plain terminal outside Claude Code, run
each step after assessing the previous result:

```bash
charmer update
charmer --version
charmer gateway status
charmer gateway restart
charmer update --verify
charmer doctor
```

Before the restart, `charmer gateway status` and doctor each show one
restart row: `gateway restart pending: the running gateway runs <path>, this install provides <path> (different on-disk SHA-256 hashes)`.
2.6.0 starts the gateway with 2.5.0's arguments and environment filter, so
there is no second restart row, and one restart clears it. Until then
doctor also reports the lines this catalog moves
([the GPT-6.2 lines](#the-gpt-62-lines)). On Linux,
`charmer update --verify` reports `running-gateway: different-running-build`
until the restart and `running-gateway: current-match` after it; on macOS
running-byte verification reports `unavailable`. The restart checks
ownership and the persistence hold before stopping, and it closes HTTP
requests still in flight: restart between turns. If it is held, keep the
gateway running and follow
[the persistence hold](guides/gateway.md#the-persistence-hold); never bypass
a held stop with a service-manager stop or a signal.

The update installs charmer's verified **Claude Code 2.1.326** copy before
switching the release: it copies a matching local file or downloads and
verifies the pin when none is available. Your own Claude Code installation
is untouched. A session already running keeps its client and its compiled
settings until its next launch or resume (`/exit`, then `charmer -r <id>`);
until then doctor says
`session <id8> runs Claude 2.1.322, not the pinned 2.1.326` and
`fence/picker differ from the installed catalog (catalog changed since launch); applies at the next resume`
for it (a session on a line this catalog moved gets another row instead
of the second: [the GPT-6.2 lines](#the-gpt-62-lines)). State and record
formats remain 4; no record migration is needed.

Managed sessions launched before the update pick up these changes at their
next resume ([compatibility](reference/compatibility.md#verified-behavior-and-limits)):

- The subagent status line charmer compiled is gone: Claude Code 2.1.326
  shows a deprecation notice at every start while any settings layer sets
  `subagentStatusLine`. Until a session's next resume, doctor reports it as
  Attention, `its subagent status line differs from this release's compile`;
  `charmer doctor --repair-all` drops the line from stopped sessions now,
  except those on a line this catalog moved.
  To keep charmer's rows, see
  [the agent panel](guides/sessions.md#the-agent-panels-rows).
- A model id Claude Code does not know and that has no `[1m]` runs in the
  window class charmer exports (`CLAUDE_CODE_MAX_CONTEXT_TOKENS`, normally
  200K, or the lead set's declared window); `[1m]` selectors and Claude's
  own 1M ids keep 1M, even against a `CLAUDE_CODE_DISABLE_1M_CONTEXT` in your
  settings; and Claude Code's exit hint names `charmer --resume <id>`.
- In a terminal, Claude Code 2.1.326 asks whether to add the session's scope
  folder ("Adding a new directory"). Answer Yes, or trust charmer's scopes
  folder once so that no session asks ([quickstart](quickstart.md#3-launch));
  choosing "No, exit" ends the launch with status 1.

These do not change your own settings or plain `claude`.

Doctor shows an advisory beginning `New model: Gemini 4 Carbon` on every
installation, even without an OpenRouter key, until you optionally admit
the line. It is informational, not a blocker; neither admission nor use
is required.

If you declared a model yourself that 2.6.0 added as a line, the
shipped line serves it and yours yields, with one Attention row and its
remedy: `charmer models rm <line> --successor <line>`
([models you added that now ship](#models-you-added-that-now-ship)). For
OpenRouter declarations of `google/gemini-4-hydrogen` or
`openai/gpt-6.2-sol`, the successor is `or-gemini` or `or-gpt`, respectively.
Both shipped lines carry every effort the listing advertised, so bindings
at low or medium (and max for `or-gpt`) still resolve after the update.
These added efforts are contract-level only, not measurements.

#### The GPT-6.2 lines

This section also covers `or-gemini`'s move to Gemini 4 Hydrogen.

Catalog 45 moves `sol` to GPT-6.2 Sol and `astra` to GPT-6.2 Astra, on the
ChatGPT account and on an OpenAI API key, and `or-gpt` to GPT-6.2 Sol on
OpenRouter ([models guide](guides/models.md#lines-moved-in-260)).
`or-gemini` moves the same way, from Gemini 3.8 Flash to Gemini 4 Hydrogen
on OpenRouter, but was not checked by a live request before 2.6.0 shipped.
Profiles and bindings that name these keys follow without reseeding or effort
changes. On the Free, Free workspace and K-12 plans neither `sol` nor
`astra` is served ([ChatGPT account](providers/chatgpt-account.md#5-use-it)).

Until the gateway restart, the running gateway serves 2.5.0's lines, and
doctor reports BLOCKED
“the on-disk gateway config differs from a fresh render of the installed catalog”
and the Attention “Pending served change: …”, which counts the selectors
the restart adds. The guarded restart publishes the new configuration as
the 2.6.0 gateway starts, and both rows clear. Restart rather than running
the `charmer providers apply` the first row suggests: 2.5.0's gateway would
then serve the GPT-6.2 selectors, but on a ChatGPT account it identifies
itself as Codex 0.159.1, and OpenAI refuses GPT-6.2 to that client. A
session launched or resumed on these lines before the restart gets
selectors the running gateway does not serve.

A session that was running on one of these lines keeps its model (GPT-6.1
Sol, GPT-6 Astra, GPT-6.1 Sol on OpenRouter, or Gemini 3.8 Flash) until its
next resume (`/exit`, then `charmer -r <id>`), which moves it to GPT-6.2
or Gemini 4 Hydrogen at the same effort. On the ChatGPT account and on
OpenRouter, the retired keys `sol@6.1`, `astra@6`, `or-gpt@6.1` and
`or-gemini@3.8` keep its selectors served until then. With an OpenAI API key
in use, the restarted gateway serves only the lines reviewed for the key,
not `sol@6.1` and `astra@6` (`or-gpt@6.1` and `or-gemini@3.8` stay served on
OpenRouter): exit such a session before the restart and resume it after.
Until the resume, doctor names the move instead of both the
`fence/picker differ …` row and the status-line row, for example
`session <id8>: the installed catalog resolves lead to gpt-multi-sol-6-2-high[1m] (recorded gpt-multi-sol-6-1-high[1m]); it applies at the next resume (charmer -r <id>)`.
`charmer doctor --repair <id>` waits for that resume (exit 3, “Repair
deferred”). `charmer doctor --repair-all` skips a session that is still
running and counts a stopped one as deferred, so its subagent status line,
too, goes at the resume.

If you declared GPT-6.2 Sol or Astra on the ChatGPT account yourself (for
example as `custom-gpt-6-2-sol`), the shipped line now serves it and yours
yields ([models you added that now ship](#models-you-added-that-now-ship)):
`charmer models rm custom-gpt-6-2-sol --successor sol` removes it. Doctor
also shows an Attention row for each alias the gateway captured for your
line whose recorded limits differ from the shipped line's,
`oauth-extra-models: overlay codex/gpt-6.2-sol: captured alias <alias> differs from the winning catalog line sol; the winning definition is served`,
until those aliases are pruned: once no running session uses them, review
the plan with `charmer doctor --prune-aliases --preview` (it lists every
captured alias, with a row or without), then run
`charmer doctor --prune-aliases`.

#### Before going back to 2.5.0

If 2.5.0 is the retained previous release, `charmer update --rollback`
returns to it. The rollback first checks your state against 2.5.0's own
packaged catalog data ([what the preflight checks](#what-the-preflight-checks))
and lists what it finds before it asks; with a finding the default is No.
Prepare first, between turns:

- Fix what the check lists under declarations, profiles and named
  bindings: each item names its file or profile and the command that fixes
  it.
- Exit the sessions it lists as
  `session <id8> uses <selector>, which 2.5.0 does not serve`: they run on
  a selector only 2.6.0 serves, such as the GPT-6.2 selectors of `sol`,
  `astra` and `or-gpt` (for example `gpt-multi-sol-6-2-high`), or
  `or-gemini`'s Hydrogen selectors (`charmer-or-gemini-4-<effort>`). After
  the rollback, `charmer -r <id>` resumes each on 2.5.0's model for that
  line: GPT-6.1 Sol, GPT-6 Astra, GPT-6.1 Sol on OpenRouter, or Gemini 3.8
  Flash. Before going back, change any `or-gpt` or `or-gemini` binding at an
  added effort to high or xhigh: 2.5.0 serves only those two efforts on
  these OpenRouter lines.
- `or-gemini-carbon` and `or-gemini-neon` have no line in 2.5.0. If you
  updated straight from 2.5.0 to 2.6.1, check for both. Before going back,
  rebind any profile, named binding or session using either line, and change
  or clear any Settings workflow default that uses one. Exit sessions still
  running on either line's selectors.

After the rollback the gateway still runs the version you left. Run the guarded
`charmer gateway restart` between turns, then `charmer update --verify`:
on Linux, 2.5.0 reports `running-gateway: different-running-build` until
the restart and `running-gateway: current-match` after it (on macOS,
`unavailable`).

2.5.0 compiles charmer's subagent status line again (unless your own
settings set one), at each session's next resume or repair. Until then its
doctor reports the sessions 2.6.0 launched
or resumed as Attention, `its subagent status line differs from this release's compile`
and `fence/picker differ from the installed catalog (catalog changed since launch); applies at the next resume`;
`charmer doctor --repair-all` clears the first for stopped sessions. A
session recorded on a GPT-6.2 or `or-gemini` Hydrogen selector gets neither
row; 2.5.0's doctor names the move back instead, for example
`session <id8>: the installed catalog resolves lead to gpt-multi-sol-6-1-high[1m] (recorded gpt-multi-sol-6-2-high[1m]); it applies at the next resume (charmer -r <id>)`,
and its repair waits for that resume (`charmer doctor --repair-all` counts
it as deferred). A session still running Claude Code 2.1.326 shows
`session <id8> runs Claude 2.1.326, not the pinned 2.1.322` until it
resumes, on 2.1.322, from its summary.

### Updating from 2.4.0

charmer 2.5.0 moves the gateway to CLIProxyAPI 8.0.67 with 30 local patches,
built with Go 1.27.4, and Claude Code from 2.1.312 to 2.1.322. Catalog 44
ships with this release; the bundled CPython stays 3.15.2, rebuilt from
python-build-standalone 20261110. The gateway's bytes change, so this
update needs a gateway restart. Updating switches the installation, not the
running gateway. Between turns, in a plain terminal outside Claude Code, run
each step after assessing the previous result:

```bash
charmer update
charmer --version
charmer gateway status
charmer gateway restart
charmer update --verify
charmer doctor
```

Before the restart, `charmer gateway status` shows one restart row and
doctor two, and one restart clears them. Both say that the running
gateway's binary differs from the one this installation provides
(different on-disk SHA-256 hashes); doctor adds
`gateway restart pending: the running gateway was started by charmer 2.4.0 and this release changes its start arguments or environment filter, which apply only at a restart`.
With a Claude sign-in, doctor also reports BLOCKED until the restart:
`the running gateway does not serve rendered selector(s) claude-fable-5-2 although a claude credential record exists`,
because 2.4.0's gateway has no Fable 5.2 route.
On Linux, `charmer update --verify` reports
`running-gateway: different-running-build` until the restart and
`running-gateway: current-match` after it; on macOS running-byte
verification reports `unavailable`. The restart checks ownership and the
persistence hold before stopping. If it is held, keep the gateway running
and follow [the persistence hold](guides/gateway.md#the-persistence-hold);
never bypass a held stop with a service-manager stop or a signal.

The update installs charmer's verified **Claude Code 2.1.322** copy before
switching the release: it copies a matching local file or downloads and
verifies the pin when none is available. Your own Claude Code installation
is untouched. A session already running keeps its client and its compiled
settings until its next launch or resume (`/exit`, then `charmer -r <id>`);
until then doctor says
`fence/picker differ from the installed catalog (catalog changed since launch); applies at the next resume`
for it. State and record formats remain 4; no record migration is needed.

After the gateway restart, a session still running Claude Code 2.1.312
keeps working, but CLIProxyAPI 8.0.67 treats a Claude Code older than
2.1.321 as a client it imitates rather than passes through: requests on a
Claude sign-in go out in the gateway's emulated client form. charmer's lead
prompt is kept (moved into a system message) and tool names are mapped back
to the real ones, so the session runs its tools as before; requests with an
API key are not changed. Resuming the session (`charmer -r <id>`) moves it to
Claude Code 2.1.322, which the gateway passes through unchanged.

Managed sessions launched before the update pick up three changes at their
next resume: Claude Code's background service is off (`/background`,
`--bg` and its choice to move a session to the background), the model may not use Claude Code's
plugin-authoring skill or write into `~/.claude/dev-mods`, and Claude Code's
newer host switches are cleared at launch
([security](security.md)). They do not change your own settings or plain
`claude`.

An interactive profile save now applies to the profile's eligible running
sessions by default: Enter accepts Apply; choose Later or Esc to keep their
current lineups. A save without a terminal still leaves them unchanged
unless you pass `--apply` ([profiles](guides/profiles.md#sessions-that-follow-a-profile)).

The `fable` line now selects Fable 5.2. Profiles and bindings that name
`fable` follow it without reseeding or effort changes; a running Fable 5.1
session keeps its route until it is resumed. If you declared Fable 5.2
yourself (for example as `custom-fable-5-2`), the shipped line now serves
it and yours yields, with one Attention row and its remedy. When the
live-record check allows it, `charmer models rm custom-fable-5-2 --successor fable`
removes your line, and sessions recorded on it resume on `fable` directly;
then `charmer doctor --repair-all` brings the scopes of ended sessions
recorded on it up to date ([models guide](guides/models.md)).

#### Before going back to 2.4.0

If 2.4.0 is the retained previous release, `charmer update --rollback`
returns to it. The rollback first checks what 2.4.0 can read of your state
([what the preflight checks](#what-the-preflight-checks)) and lists what it
finds before it asks; the default is then No. Prepare first, between turns:

- Remove a provider you declared on a private network: 2.4.0 has no
  private-network routes, so the check lists the declaration, the profiles
  that use its lines and the sessions running on its selectors.
  `charmer providers rm <id>` names the lines still in use.
- Exit sessions running on Fable 5.2: the check lists each as
  `session <id8> uses claude-fable-5-2, which 2.4.0 does not serve`. After
  the rollback, `charmer -r <id>` resumes it on 2.4.0's `fable` line,
  Fable 5.1.
- 2.4.0 ignores the successor of a line you removed with `--successor`: a
  session recorded on that line needs a model choice there.

After the rollback the running gateway is still 2.5.0's. Run the guarded
`charmer gateway restart` between turns, then `charmer update --verify`:
on Linux, 2.4.0 reports `running-gateway: different-running-build` until
the restart and `running-gateway: current-match` after it (on macOS,
`unavailable`).

2.4.0's doctor then reports BLOCKED for each session 2.5.0 launched or
resumed, because 2.5.0 compiles settings and a `/cm` skill 2.4.0 does not:
`session <id8>: scope differs from the record-authoritative compile`, naming
`settings.json` and `.claude/skills/cm/SKILL.md`, and
`1 scope(s) diverged from record authority`. Run
`charmer doctor --repair-all` (or `charmer doctor --repair <id>` for each);
it defers a session that was on Fable 5.2 until that session resumes, on
Fable 5.1. The session's
`fence/picker differ from the installed catalog (catalog changed since launch); applies at the next resume`
row stays until its next resume. A fork that 2.5.0 created, resumed under
2.4.0, sends its parent's recorded system prompt until its first
compaction. A lineup message 2.5.0 queued for a session's next prompt is
not delivered by 2.4.0, which shows its own notice instead.

### Updating from 2.3.0

charmer 2.4.0 changes the launcher and the pinned Claude Code, from 2.1.311
to 2.1.312, and ships catalog 43. The gateway (CLIProxyAPI 8.0.45 with 27
local patches), Go 1.27.3 and the bundled CPython 3.15.2 stay the same, so
no gateway restart is needed: the running gateway's bytes are 2.4.0's, and
`charmer update --verify` reports `running-gateway: current-match` at once.
Between turns, in a plain terminal outside Claude Code, run each step after
assessing the previous result:

```bash
charmer update
charmer --version
charmer gateway status
charmer update --verify
charmer doctor
```

The update installs charmer's verified **Claude Code 2.1.312** copy before
switching the release: it copies a matching local file or downloads and
verifies the pin when none is available. Your own Claude Code installation
is untouched. A session already running keeps its client and its compiled
settings until its next launch or resume (`/exit`, then `charmer -r <id>`);
until then doctor says
`fence/picker differ from the installed catalog (catalog changed since launch); applies at the next resume`
for it. State and record formats remain 4; no record migration is needed.
Profiles, bindings, declared models and continuity aliases are unchanged:
2.4.0 adds no model, effort contract or selector.

Managed sessions launched before the update pick up one new compiled setting
at their next resume: `CLAUDE_CODE_MODEL_CAPABILITIES`, which marks every
fenced non-Anthropic selector of the lineup `-refusal_fallback`, so Claude
Code 2.1.312 never answers a refusal from one of them with another model
([compatibility](reference/compatibility.md#refusal-fallback)). It does not
change your own settings or plain `claude`.

After the update, `charmer doctor` may list aliases that removed models left
behind as one row; review the plan with `charmer doctor --prune-aliases
--preview` before removing them ([models guide](guides/models.md)).

#### Before going back to 2.3.0

If 2.3.0 is the retained previous release, `charmer update --rollback`
returns to it. The rollback first checks what 2.3.0 can read of your state
([what the preflight checks](#what-the-preflight-checks)). 2.4.0 adds no
model, effort contract or selector that 2.3.0 lacks, so the check should
find nothing when you roll back to 2.3.0; it lists what it checked before it
asks.

After the rollback the running gateway needs no restart: 2.3.0 ships the
same gateway bytes. Sessions 2.4.0 compiled show 2.3.0's Attention
`session <id8>: fence/picker differ from the installed catalog (catalog changed since launch); applies at the next resume`
until their next resume (`charmer -r <id>`); a session still running the
2.1.312 client shows `session <id8> runs Claude 2.1.312, not the pinned 2.1.311`
until then, and resumes on 2.1.311 from its summary. Neither needs
`charmer doctor --repair-all`.

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

Doctor lists the aliases left by earlier lines only when no running session
uses them, as one finding. `charmer doctor --prune-aliases --preview` shows
the plan without writing; without names, `charmer doctor --prune-aliases`
prunes all of them (**H** → Enter previews and confirms the listed group).
Name aliases only when you want to keep some. It keeps captures of current
model declarations; naming an alias still used by a live session refuses the
whole operation, and removing a stale captured copy never removes an alias a
shipped line owns.

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
no installed release needs. A copy in use (locked by a running session, or
seen executing in a running process) is kept, and setup and updates name up
to eight processes seen using it by PID and program name, never their
arguments; `charmer doctor -v` shows the same detail.

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

### What the preflight checks

Before it asks, `charmer update --rollback` checks what the previous
version can read of the state on disk. It reads the retained release's
packaged catalog data and evaluates your state against it with this
release's own rules; nothing is changed by the check:

1. **Declarations** — the models and providers you declared
   (`providers.d`, the ledger, the legacy `custom.json`) that the previous
   version's catalog would reject, used or not: one such line makes its
   providers apply, models add and models rm commands refuse. Each is
   listed with its file and line and its fix (`charmer models rm <line>`, or
   `charmer models edit <line>`).
2. **Profiles and named bindings** — errors for a profile, a named binding
   or the Settings workflow default under the previous catalog. Each finding
   says where to rebind it; the workflow default can also be cleared in
   Settings (**O** on the card).
3. **Running sessions** — a session whose lead or agent runs on a selector
   this release serves and the previous version would not (its model picker
   does not count: the previous version recompiles it at the next resume).
   Exit it first; after the rollback, `charmer -r <id>` resumes it on the
   previous version's models.

Items both versions report are not listed; the line
“Also reported by” … “unchanged by the rollback” counts them, zero included.
The previous version's own doctor is not run before you answer: its report
would repoint the shared hook and token shims to the previous launcher, a
change before consent. Run `charmer doctor` after the rollback, as before.

With a finding, the question defaults to No, an empty answer changes nothing
(exit 3), and `--yes` refuses with exit 1 (“cannot read what is listed
above”): prepare first, or run without `--yes` on a terminal and confirm to
roll back anyway. With nothing found, `--yes` proceeds as before. When the
retained release's data is missing or unreadable, or the check itself fails,
the rollback says so (“Could not check what” …) and asks as before.

The check applies this release's rules, its model-id grammar included, to
the previous version's data: the previous version may still refuse
something these rules accept.

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
