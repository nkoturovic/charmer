# Troubleshooting

Start with `charmer doctor`. It reports **Ready**, **Attention**
(a warning to assess, with a remedy) or **BLOCKED** (something is
broken, and what). Plain `doctor` is read-only; repair, pruning and token
rotation require their explicit options. Use `charmer doctor --json` for a
machine-readable, read-only report. Neither plain doctor nor opening **H** fetches
release metadata.
The tables below go from a symptom to the doctor finding, the safest fix
and how to check it worked. [CHEATSHEET.md](CHEATSHEET.md) has the short
version.

Doctor lists the blocking items first, then the attention items grouped by
area, in this order: **Gateway** (a persistence hold always first),
**Installation**, **Providers**, **Sessions**, **Profiles** and **Models**.
A finding that repeats is one line: a model finding says how many profiles
and slots bind the model (`kimi-k3: … (3 profiles, 12 slots)`), and a kind
of session finding says how many sessions it concerns and names the first
three, then “(doctor -v lists them)”. `charmer doctor -v` prints every
original line. After Attention comes **Advisories (optional evidence; does
not change the verdict)**: doctor can say Ready while advisories are listed
(see [the advisories](#models-evidence-and-bindings)).

In the launcher, **H** shows the same findings as a list, in the same
order: Enter on a row does what it says, after asking first (the
persistence hold: why it holds and the clear-hold command, which stays a
typed confirmation outside Claude Code; a missing key: **G** → **K**; scopes
to repair: repair-all, stopped sessions only; an unused alias:
prune-aliases; generated leftovers: the prune preview, then y/N; sessions
that do not run and have no recorded end: mark-ended for each; sessions
that need a lead: Sessions; a newer shipped profile: reseed; an advisory
model: its optional diagnostics, **M** → **Q**). Any other row shows its exact
command. **V** shows the full report as `doctor -v` prints it, **G** the
gateway's actions (start, restart, log). The separate **Verify this installation**
row runs only when selected: choose downloaded release files for fully offline
verification, or confirm the exact-version online fetch (Cancel is focused).
It cannot repair or restart anything; [verification](update.md#verify-an-installation)
is not the doctor's readiness verdict.

An installation from before the rename is moved with
`charmer migrate from-legacy` ([the migration guide](guides/legacy-migration.md));
while that move is unfinished, other commands refuse and name it (read-only
`update --verify` remains available, as do `charmer gateway status`, `logs`
and `clear-hold` from when its folders
are in place until `--back` changes them, so a persistence hold can be
resolved).

Two rules come first:

- A signed-in account's credentials may be mid-save: when doctor reports a
  gateway credential save that failed or is unconfirmed, keep the gateway
  running and follow [the persistence hold](guides/gateway.md#the-persistence-hold)
  before any restart, update or uninstall.
- Never delete files under `~/.claude` to fix a session, never edit
  generated files or records by hand, and never turn TLS verification off.

## Installation verification

Run `charmer update --verify` explicitly, or add `--from-dir <dir>` to use
downloaded signed metadata with no network. See [what it verifies](update.md#verify-an-installation).

| Result | Meaning | Safest next step |
| --- | --- | --- |
| `mismatch` (exit 1) | signed metadata, installed content or a component differs | assess the named component; use the signed release installer or a compatible rollback, never hand-edit the manifest or ignore a file |
| `different-running-build` (exit 1) | installed links changed but the running gateway is not that build | between turns, explicit `charmer gateway restart`, then status, verify and doctor; never bypass a held stop |
| `stopped` with `verified` (exit 0) | installed files verified, no running gateway | not ready: start only through the supported guarded path, then check status and doctor |
| `unavailable` / incomplete (exit 3) | signed files, ownership or process bytes cannot be proved, or the install/platform is unsupported | follow the stated limit; a downloaded exact-release directory can replace an unavailable online fetch, not a missing process proof |

The 2.0.1 updater leaves the old gateway running after switching links. After
rollback to 2.0.1, `--verify` is unavailable, not successful. Running-byte
verification is Linux-only; before updating a Mac, check the **macOS 13** floor
because the old updater does not guarantee refusal. [The update sequence](update.md#updating-from-201)
keeps restart, integrity and readiness checks separate.

## The gateway

| Symptom | Doctor says | Fix | Check |
| --- | --- | --- | --- |
| sessions cannot connect; Claude Code reports a connection refused | “local gateway: <error> — <fix>” | `charmer gateway start`; if it does not come up, `charmer gateway logs` shows why it stopped | `charmer gateway status` says ready; the session's next request succeeds (its token helper reconnects by itself) |
| a new installation: nothing renders or starts | the gateway is not set up yet | `charmer setup --step gateway` | `charmer gateway status` |
| automatic starts stopped after repeated crashes | three starts in ten minutes | read `charmer gateway logs`, fix the cause, then `charmer gateway start` | status shows one instance running |
| a change did not take effect | “gateway did not reload the current render (sentinel missing) — restart required” | between turns, `charmer gateway restart` (not while the persistence hold is active) | `charmer doctor` |
| the configuration on disk is stale | “the on-disk gateway config differs from a fresh render of the installed catalog” | `charmer providers apply` | doctor no longer reports it |
| a model is missing from the gateway | “the running gateway does not serve rendered selector” | `charmer gateway restart` between turns | `charmer doctor` |
| sessions are refused with 401 | “token mismatch” | `charmer-proxy init` (verifies the reload); if it persists, `charmer gateway restart` | doctor |
| the gateway's key files are unusable | “gateway key slots unusable” | `charmer doctor --rotate-token` in a terminal | doctor |
| every change is refused while a transaction runs | “the gateway is inhibited by <owner> (…)” | wait for that installer, update or service hand-off; if it was interrupted, run the command the message names | `charmer gateway status` shows no inhibition |
| commands refuse: another state directory | “gateway managed for root <A>; this command used <B>” | run with the managed state directory (check `XDG_STATE_HOME`) | doctor |
| `charmer gateway service install` on Ubuntu 23.10 or later: the gateway exited during its start | the on-demand gateway is kept; doctor does not diagnose the AppArmor restriction | keep the on-demand gateway, or ask an administrator to assess the system-wide security trade-off of allowing unprivileged user namespaces ([service requirements](guides/gateway.md#the-supervised-service-linux)) | `charmer gateway status`; after an approved system change, retry service install and check `charmer gateway service status` |
| the gateway service keeps restarting | “gateway working directory <path> holds a .env file” | move that `.env` aside, then `charmer gateway restart` | `charmer gateway service status` |
| doctor: the installed service unit differs, or a restart is pending | the unit or the package it runs changed | `charmer gateway service install` (refreshes it), then follow its line | `charmer gateway service status` |
| a held gateway refuses stop, restart, update | “gateway persistence hold” (a failed save, or incomplete evidence: the reason says which) | keep it running; incomplete coverage, a known journald restart or an unavailable observation: check the credentials (`charmer providers list`), then `charmer gateway clear-hold`; a failed save: wait for a persisted save (sign in again only if none comes) before clearing; see [the persistence hold](guides/gateway.md#the-persistence-hold) | `charmer gateway status` shows no hold |
| the card says “gateway held (log coverage, no failed save)”; doctor lists it first | no failed credential save was seen in the part read; the reason says what evidence may be missing | keep the gateway running; **H** → Enter shows the reason and its remedy. Check the credentials, then `charmer gateway clear-hold` in a terminal outside Claude Code (typed confirmation). An unavailable journald observation remains visible after acknowledgement | `charmer gateway status` shows no hold |
| “journald restarted; credential-save evidence may be missing” even though the gateway's start line survived | journald's current process started after both the gateway and the last clear | check the credentials, then clear once; another restart holds again. This is a conservative risk check, not proof of lost credentials or complete evidence | `charmer gateway status`; [limits and timestamp compatibility](guides/gateway.md#the-persistence-hold) |
| journald's current process start could not be observed | the system-manager query is missing, denied, timed out, inactive, invalid or unsupported on older systemd | keep the gateway running; check the credentials, then `charmer gateway clear-hold` to acknowledge the coverage gap for this running instance. Only a clear whose report showed it as unavailable counts; a clear made while it was available does not create an acknowledgement. The observation stays unavailable; later failed saves still hold | `charmer gateway status` shows the acknowledged limitation and no hold, unless another reason still holds |
| the gateway does not start after `charmer update` | “local gateway: …”, with “if this began after charmer update: charmer update --rollback, then charmer gateway start (returns to <version>'s gateway; sessions keep their records)” | read why (`charmer gateway logs`); to go back to the previous release's gateway: `charmer update --rollback`, then `charmer gateway start`. Shown only for an installed release whose previous version has another gateway and accepts the state on disk | `charmer gateway status` says ready |
| a continuity alias is not served, or an alias has no line | “continuity alias <alias> is not served … charmer doctor --prune-aliases <alias>”, or “alias <alias> … has no line — prune when unused” | once no live session uses it: `charmer doctor --prune-aliases <alias>` (**H** → Enter on the row prunes it after a confirmation); it is refused while a live session uses the alias | doctor no longer lists it |
| HTTP 400 `prompt_cache_retention is not supported on this model` | the running gateway predates the installed release | `charmer gateway restart` between turns | the next request succeeds |
| continuity set unreadable | “gateway continuity set unreadable” | move `~/.config/charmer/continuity.json` aside, then `charmer-proxy init` (it re-seeds the set) | doctor |

## Providers, keys and sign-ins

Connected, entitled and working are different: a key can be set but
revoked, a sign-in saved but expired, an account valid but not entitled
to a model.

| Symptom | Doctor says | Fix |
| --- | --- | --- |
| a provider shows not connected | its key is missing, or no sign-in is saved | `charmer providers set-key <provider>`, or `charmer providers sign-in anthropic` |
| an account's requests fail after a while | `invalid_grant` lines since the sign-in, or the pool has no credential record | sign in again: `charmer providers sign-in anthropic` (or `openai`) |
| one or more accounts' models disappear after a credential-file or auth-directory read error | the gateway can drop those accounts' models instead of retaining the last working credentials; doctor may report missing selectors | resolve the read failure and restore owner-only access, then check `charmer providers list` and `charmer doctor`; if models remain missing, follow [the persistence hold](guides/gateway.md#the-persistence-hold) before a guarded `charmer gateway restart`; do not delete the credential files |
| an account returns another 401 after a model or alias reload | a reload can forget the gateway's in-memory rejection of an account whose access and refresh tokens were already refused, so it may try that account again | sign in to the account again through charmer (`charmer providers sign-in anthropic` or `openai`); do not edit or delete its credential file |
| 401 or 403 from a keyed provider | the provider refused the key | create a new key in its console and set it again |
| 402 or 429, agents of one provider stall | per-model counts of 402/429/403 from the gateway's log | in the session, `/cm fallback <provider>`, then `/reload-plugins`; for new launches pick a fallback profile |
| a provider has no models | its key is set but no model is declared | **G** → **K** continues into adding a model, or `charmer discover <provider>` |
| a provider or model command: “needs a terminal outside Claude Code sessions” | the command itself refuses | run it yourself in a terminal outside Claude Code; no flag waives this |
| a model answered like another one | “gateway substitution:” | the provider served a different model; check `charmer models` and rebind the agent if it matters |
| requests fail saying a spending or usage limit was reached | per-model counts of 402 or 429 (a limit you set yourself can answer 400) | raise or wait out the limit in the provider's console ([costs per provider](providers/api-keys.md#6-costs-and-limits)); meanwhile `/cm fallback <provider>` |
| Anthropic or OpenAI on its API key: nothing of it is served | “its selectors are not served”, with the reason: the key is not set, the route is not approved, or no model is reviewed for it | a missing key: `charmer providers set-key openai` (or `anthropic`); a route not approved: `charmer providers transport openai api-key`; or back to the account: `charmer providers transport openai oauth-pool` |
| OpenAI on its API key: some OpenAI models are gone | doctor lists the lines reviewed for the key route and those not served | expected: the key serves only the reviewed models; bind one of those, or switch back to the ChatGPT account |
| OpenAI on its API key: long answers cost more than expected | none: each answer is bounded by Claude Code's output cap, not by a spend limit | set a hard project spend limit with OpenAI ([providers/api-keys.md](providers/api-keys.md#openai)) |
| ChatGPT account or OpenAI API key: a reply stops at a preamble or looks incomplete | none: if an earlier message streamed text, the gateway can omit a later message that arrives only in its final event | ask for the rest; this is a [gateway reply limit](guides/gateway.md#what-the-gateway-changes-in-a-request), separate from the late-stream completion limit below |
| OpenRouter or another OpenAI-compatible provider: a long session stops with the provider's context-length error | none: on these routes a context-window error is shown as the provider's error and does not start automatic compaction (on OpenAI it does) | run `/compact`, then continue |
| Qwen answers 401 | the key does not match the address | the Qwen provider takes a Token Plan key; a pay-as-you-go Model Studio key belongs to the `model-studio` preset |
| a preset or an OpenAI-compatible endpoint: “not available in this release” | a build whose keyed OpenAI-compatible audit is closed | use the vendor's Anthropic-compatible endpoint, OpenRouter, or a keyless server on your network ([providers/openai-compatible.md](providers/openai-compatible.md)) |
| adding a preset again: “is already the API key of …” | another provider uses that key name | follow the message: another provider name, or share or replace the key on purpose ([the same preset twice](providers/anthropic-compatible.md#the-same-preset-twice)) |
| setting a key asks to replace it “for each of them” | the key name is shared: other providers (a preset added twice, or a provider of the earlier custom registry) use the same key | answer y to replace it for every provider named, or N to keep it ([replace a shared key](providers/api-keys.md#4-replace-remove-disconnect)) |

## Models, evidence and bindings

Route availability, the optional admission badge and optional qualification
are separate. Local readiness means configured and served, not upstream
verified. Doctor never sends model qualification requests automatically.

These findings are **advisories**: optional evidence, listed after Attention,
that never change doctor's verdict (Ready can stand beside them, and
`--json` gives them the level `advisory`, which its status ignores): a
model not admitted (also a New model without its badge), qualification not
run, failed, stale for the definition or older than the current pins,
exact-client evidence, forced-tool evidence for the workflow default, a
capability or role recommendation an explicit binding overrides, an effort
not verified, an unknown family, unknown review independence, a missing
companion grade and an unbound Explore replacement. Each is one line per
model and finding with how many profiles and slots bind it, and the
optional next step (`charmer models admit <line>`, or
`charmer models qualify <line> --agents` / **M** → **Q**: optional diagnostics,
which may cost provider calls and never run automatically). A context
overflow risk and an ignored lead-only environment stay Attention: they
change what a session can do. A release that re-pins Claude Code or
changes the gateway makes every qualification older than the pins, so
expect these advisories after an update; they ask for nothing.

| Symptom | Meaning | What to do |
| --- | --- | --- |
| New · not admitted, or stale admission | the optional definition-bound badge is absent or stale; not a use block | bind the model on its usable route, or optionally use **M** → Enter / `charmer models admit <line>`; admission sends no inference request |
| qualification not run, failed or stale; exact-client evidence unavailable | an advisory, not a lead/agent/workflow ban; the failed check remains a failure | assess the named limitation; optionally run `charmer models qualify <line> --agents` at a terminal outside Claude Code, with explicit human consent to the plan, default No |
| tools evidence covers auto but not forced | automatic tool use is not evidence of forced-tool support | keep the warning or explicitly test forced tools; no automatic weaker-contract retry |
| a model remains usable after `models revoke` | expected: revoke removes only the badge, leaving qualification evidence unchanged | remove its binding/declaration or disable the provider if you want to stop using it |
| a model is disabled or its route is unavailable | a real configuration restriction, not missing qualification | enable the provider deliberately or approve/fix the exact route named; there is no implicit transport or credential fallback |
| Direct can launch despite a missing credential | expected: CLI Direct warns "requests may fail"; TUI Direct asks **Launch anyway?**, default **No**, and continues only on Yes | set the key or sign in before expecting requests to work; profile/binding credential refusals remain, and neither Direct path bypasses route approval, provider disablement or an unusable selected transport |
| a family says independence unknown | an unknown or unrecognized label cannot certify independent review | keep the model if wanted; use a recognized different family if review independence matters |
| a capability/role or missing companion-grade warning | the binding overrides a recommendation | assess it; the explicit lead/agent binding remains allowed, including LAN and legacy custom lines |
| client window or compaction trigger exceeds provider bound | overflow is possible; a small provider limit does not shrink a shared client window | inspect the actual class, process window, trigger and provider bound; reduce the session ceiling where appropriate or choose another binding, never inflate declared capacity |
| LAN server observed unreachable | Attention rather than a launch veto; requests may fail | connect to its network or choose another model; unknown observations are not promoted to ready |
| nondefault effort refused for a client-effort workflow default | that separate effort cannot be represented by its one selector | choose its default effort; admission or qualification cannot waive this limit |
| a new binding needs RELAUNCH | its selector is outside the proven launch fence or its class/process policy changes | resume to apply it; being operator-added or unqualified alone does not require relaunch |
| an older release refuses a profile after rollback | unchanged storage formats do not imply support for broader family labels or newly allowed bindings | use a compatible binding or return to the newer release; never hand-edit records to bypass the refusal ([rollback limits](guides/move-machines.md#moving-to-an-older-release-or-rolling-back)) |

Unknown roles/models, malformed declarations, missing selector mappings,
unsupported native efforts, unsafe routes and damaged state still refuse.
See [models](guides/models.md) and [profile limits](guides/profiles.md#recommendations-and-technical-limits).

## Claude Code's copy

| Symptom | Doctor says | Fix | Check |
| --- | --- | --- | --- |
| launches refuse: the copy is missing or changed | “is not set up for charmer”, or “does not match the verified sha256” | `charmer setup --step claude` | `charmer doctor` |
| the card looks ready but a launch refuses | the card checks the copy's size and metadata only; doctor and every launch check its full hash | `charmer setup --step claude` | the launch |
| after an upgrade a running session still uses the old Claude Code | “runs Claude <version>, not the pinned” | `/exit`, then `charmer -r <id>` | the session's next start shows no notice |
| doctor: a newer Claude Code than the pin is installed | the pin was verified some days ago, or your own Claude Code is newer | nothing to do: a charmer release brings a newer pin | doctor shows it as information |

A session started before an upgrade can keep its old client without a
prompt-time warning: an existing lineup marker suppresses the per-prompt
client check until the session's next start. Doctor and the next resume
report it.

## Sessions

| Symptom | Fix |
| --- | --- |
| ⚠ fork waiting; “has an unresolved native fork” | Enter on the row (adopt or discard), or `charmer sessions resolve-fork <id> <fork-id>` |
| ! repair needed; “identity is repair-needed” | `charmer -r <id>`, or `charmer sessions relink-runtime <id> <runtime-id>` |
| ● or ◐ but the session is gone | `charmer sessions mark-ended <id>` (all: `--all-dead`) |
| “<n> sessions do not run and have no recorded end”, or “session <id>: does not run and has no recorded end” | each exited without its end being recorded, so commands that refuse while a session may run count it (migration and uninstall among them): `charmer sessions mark-ended --all-dead` (one: `charmer sessions mark-ended <id>`); **H** → Enter on the row marks every one after a confirmation, and each mark checks again that the session does not run. Doctor names none while whether sessions run cannot be told (the process table or the background daemon unreadable, a state root on a network filesystem) |
| “<n> generated files left by forgotten or ended sessions” | scopes, lead prompts, notice markers or lineup logs no record owns any more: `charmer doctor --prune --preview` lists them, `charmer doctor --prune` removes them (**H** → Enter on the row shows the preview, then asks y/N); records, transcripts and backups are never among them. Doctor names none while a record cannot be read |
| “scope differs from the record-authoritative compile” | `charmer doctor --repair <id>` |
| “<n> session(s) need a profile or model choice (lead removed)” | the lead each was bound to left the catalog: resume one interactively (`charmer -r <id>`, which asks for a profile or model), or `charmer lineup --session <runtime-id> --relaunch profile <name>`; **H** → Enter on the row opens Sessions. Doctor names the first three ids; `doctor -v` lists every one |
| “<n> session scope(s) were compiled by <earlier name>” | the scopes of sessions from before the rename: `charmer doctor --repair-all` recompiles every stopped one now (**H** → Enter on the row, after a confirmation); a running one is recompiled at its next resume. Doctor shows one line for all of them |
| “session record <id> is unreadable” | `charmer sessions show <id>` names the error; never edit the record; forget a disposable session with `charmer sessions forget <id>` |
| resume says the session does not exist | `charmer -r <id>` (it resumes from the recorded directory) |
| a session is missing from the list | **C** on the sessions screen (all directories) |

More: [guides/sessions.md](guides/sessions.md).

## Inside a session

| Symptom | Fix |
| --- | --- |
| `/cm` fails: `charmer` not found | `charmer` must be on the session's PATH |
| `/cm` changed nothing for new agents | type `/reload-plugins`, then start agents fresh |
| `/model` refuses a model | it is outside the lead set: relaunch with a profile whose lead is that model |
| an MCP server cannot find its API key | managed sessions remove every `*_API_KEY` variable except the names you keep: add its name in Settings (**O**) → kept environment; provider keys are never kept. **V** on the card shows kept and removed names, doctor too. Applies at the next launch or resume |
| a connection drops during an HTTP MCP tool call | the pinned client can retry automatically; a state-changing call may already have taken effect. Treat its outcome as unknown and check the external system before retrying ([client limits](reference/compatibility.md#deferred-client-fixes)) |
| implementers “unavailable here” | start the session in a Git repository (`git init`) |
| Remote Control, `/schedule` or claude.ai connectors are missing | expected in managed sessions; use plain `claude` for these |

The pinned client also predates several native sandbox, worktree and
Bash-permission fixes in Claude Code 2.1.304–2.1.309. charmer does not replace
Claude Code's sandbox or worktree protections; see the
[deferred client fixes](reference/compatibility.md#deferred-client-fixes).

### Late-stream completion limit

With Claude Code 2.1.302 and the tested CLIProxyAPI 8.0.33 gateway candidate,
a stream missing `message_stop` after completed content and a terminal
`message_delta` can make the client report success without recovery, despite
the gateway's SSE error. A successful client result is not proof of complete
stream framing.

The corrected paired fixtures preserved final text and reported usage, with
one fixture-tool call per tool case. This is a bounded known negative, not
recovery or proof of harmlessness. The fixtures use buffered SSE
reconstruction: they do not establish eager delivery, timing, general
exactly-once behavior, broader no-loss behavior or a prior-version baseline.

Earlier-cut recovery remains a separate required gate. Changed client or
gateway bytes require fresh qualification.

## Platforms and files

| Symptom | Fix |
| --- | --- |
| WSL: the gateway is gone after closing every terminal | expected: WSL stopped; the next launch starts it ([install/windows-wsl2.md](install/windows-wsl2.md#when-wsl-shuts-down)) |
| WSL: Claude Code on Windows and in WSL see different sign-ins | WSL: the Windows-side and WSL-side Claude configurations are separate — sign-ins, settings and sessions of Claude Code on Windows are not seen here, and the reverse |
| WSL: “is on a Windows drive” | move the state directory into the Linux file system |
| this account's state belongs to another installation | use that installation, or follow the printed fix to move the account ([install/linux.md](install/linux.md#one-installation-per-account)) |
| a certificate error | [guides/networking.md](guides/networking.md#certificate-errors) |
| “session record directory is unavailable; records are unknown” | the state directory must be yours, mode 0700; fix ownership or mode, then `charmer doctor` |
| a write fails on a full or read-only disk | free space or make the path writable, then rerun the command; writes are atomic and leave the old file in place |
| the full-screen interface misbehaves | `charmer --line` uses plain lines; `--no-color` (or `NO_COLOR`) turns colour off |

## The launch card

A card row that does not fit keeps its remedy (the part after the dash)
and shortens the middle, so an 80-column terminal still shows what to do.
**V** shows every row in full.

| The card shows | Fix |
| --- | --- |
| “! CLAUDE_CONFIG_DIR ignored: settings, MCP, memory, resume use ~/.claude — V” | managed sessions read settings, MCP servers, memory and resume from `~/.claude`; **V** says which values that affects. Unset the variable, or keep it for your own `claude` |
| a new model is not admitted | the badge is optional: bind it on a usable route, or **M** → Enter to attest locally; see any separate route/credential remedy |
| “implementers unavailable: not a Git repository here (git init enables them)” | launch from a Git work tree, or `git init` |
| “! nothing is connected yet — Enter opens Get started” | Enter opens Get started (a session could not work yet); **L** launches anyway |
| “! gateway held: …” with “gateway ok, held” | the persistence hold: see [the gateway](#the-gateway). The card names the cause only when every reason is log coverage (“no failed save”); any other hold says “a credential save may be unresolved” (a failed save, an unreadable hold record or an evaluation that failed): **H** lists it first and Enter on it shows the reasons and the clear-hold command |
| the release row says “U checks for a newer release” | the card checks nothing by itself; **U** checks, shows the plan and asks |
| the lead or agents with “no <pool> OAuth credential record” | sign in where the row says: **G**, then **L** on the account provider (on a resume card, the row names the way back to a launch card first) |
| **H**: a key to save, “G (providers) → K on <provider>” | **G**, then **K** on that provider (from a terminal: `charmer providers set-key <provider>`); on a resume card, the report first names the way back to a launch card, then **S** resumes |
| the profile editor: what a role is for | the row of the selected lead or agent shows its role, grade and description; the model picker shows it as its subtitle and in **V** |
