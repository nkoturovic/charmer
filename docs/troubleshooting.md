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
to repair: repair-all, stopped sessions by default; an unused alias:
prune-aliases; generated leftovers: the prune preview, then y/N; sessions
that do not run and have no recorded end: mark-ended for each; sessions
that need a lead: Sessions; a newer shipped profile: reseed; an advisory
model: its optional diagnostics, **M** → **Q**). Any other row shows its exact
command. **V** shows the full report as `doctor -v` prints it, **W** the
gateway's actions (start, restart, log). The separate **Verify this installation**
row runs only when selected: choose downloaded release files for fully offline
verification, or confirm the exact-version online fetch (Cancel is focused).
It cannot repair or restart anything; [verification](update.md#verify-an-installation)
is not the doctor's readiness verdict. On a scope-repair row,
**Including running sessions** previews the affected sessions and asks
again with Cancel focused first; only those previewed ids are repaired.
Lineups the installed catalog cannot recompile are reported as deferred,
not repaired.

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

## Installation and update failures

| Symptom | Fix |
| --- | --- |
| an update reports “response failed” while reading release files | the response was interrupted, truncated or timed out; no release switch occurred. Retry the update. A local write error instead names the local problem: resolve that before retrying |
| the installer says an older bundle cannot safely reclaim its lock | first confirm no installer or updater is running, then follow the printed `rm -r` command for only the named lock and retry. Keep `.lock-guard`; do not run an older release's stale-lock reclamation concurrently with another installer or updater |
| installing an older release says it used system sync | its bundle lacks the durability helper; the installer uses system `sync` with a notice. Away from Linux this does not provide the newer helper's per-file guarantee ([update details](update.md#what-happens-to-old-files)) |

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
| every change is refused while a transaction runs | “the gateway is inhibited by <owner> (…)” | wait for that installer, update or service hand-off; if it was interrupted, run the command the message names. An unfinished service install or refresh must be completed with `charmer gateway service install`; uninstall refuses and does not clear that hand-off. Never remove the inhibition record by hand | `charmer gateway status` shows no inhibition |
| service removal or restoration refuses because the gateway changed meanwhile | the instance changed after its persistence hold was checked | inspect `charmer gateway status`, then retry the named service operation between turns; do not bypass the refusal with a signal or service-manager stop | the operation completes against the checked instance |
| a gateway command names “the recorded managed state root” as missing or unusable | it cannot safely lock that root; nothing was changed | follow the printed access or move-aside remedy and `charmer-proxy init --state-root <root>` command. This recreates directories, not lost session records | retry the refused command, then doctor |
| a persistence hold says “its log name has an invalid UTC start time” | an instance-log filename is not valid evidence; pruning is skipped | keep the gateway and its logs; assess the missing evidence through [the persistence hold](guides/gateway.md#the-persistence-hold). Clearing a hold does not repair the filename | status reports whether evidence is still incomplete |
| commands refuse: another state directory | “gateway managed for root <A>; this command used <B>” | run with the managed state directory (check `XDG_STATE_HOME`) | doctor |
| `charmer gateway service install` on Ubuntu 23.10 or later: the gateway exited during its start | the on-demand gateway is kept; doctor does not diagnose the AppArmor restriction | keep the on-demand gateway, or ask an administrator to assess the system-wide security trade-off of allowing unprivileged user namespaces ([service requirements](guides/gateway.md#the-supervised-service-linux)) | `charmer gateway status`; after an approved system change, retry service install and check `charmer gateway service status` |
| the gateway service keeps restarting | “gateway working directory <path> holds a .env file” | move that `.env` aside, then `charmer gateway restart` | `charmer gateway service status` |
| doctor: the installed service unit differs, or its restart is pending | the unit or the package it runs changed | `charmer gateway service install` (refreshes it), then follow its line | `charmer gateway service status` |
| status or doctor says `gateway same binary on another path; restart optional` | the recorded and installed files have matching on-disk SHA-256 hashes; running bytes are not verified by this check | no restart is needed for the path change alone; to move to this installation, use `charmer gateway restart` between turns, subject to the persistence hold | [verify the running bytes separately](update.md#verify-an-installation); the old running path stays protected from cleanup |
| status or doctor says `gateway restart pending` for the binary | the on-disk files differ, or a file or comparison is unavailable | restore a missing or unreadable binary as directed, then `charmer gateway restart` between turns; never bypass a persistence hold | `charmer gateway status`, then `charmer update --verify` and doctor |
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
| a provider listing has a redacted invalid-file row | credential-bearing names are withheld; its count is problems, not files | run `charmer providers validate` and follow its rename remedy; model edit/removal cannot select the rejected declaration. Do not paste the file's secret-bearing name into a report |
| listing or qualification reports a malformed or incomplete response | a body shorter than its declared length is not a valid result; qualification can be inconclusive | check the provider or network, then repeat only the intended check with its usual consent; an absent management reading does not mean zero usage |
| a provider shows not connected | its key is missing, or no sign-in is saved | `charmer providers set-key <provider>`, or `charmer providers sign-in anthropic` |
| an account's requests fail after a while | `invalid_grant` lines since the sign-in, or the pool has no credential record | sign in again: `charmer providers sign-in anthropic` (or `openai`) |
| 401 or 403 from a keyed provider | the provider refused the key | create a new key in its console and set it again |
| 402 or 429, agents of one provider stall | per-model counts of 402/429/403 from the gateway's log | in the session, `/cm fallback <provider>`, then `/reload-plugins`; for new launches pick a fallback profile |
| a provider has no models | its key is set but no model is declared | **G** → **K** continues into adding a model, or `charmer discover <provider>` |
| a provider or model command: “needs a terminal outside Claude Code sessions” | the command itself refuses | run it yourself in a terminal outside Claude Code; no flag waives this |
| a model answered like another one | “gateway substitution:” | the provider served a different model; check `charmer models` and rebind the agent if it matters |
| requests fail saying a spending or usage limit was reached | per-model counts of 402 or 429 (a limit you set yourself can answer 400) | raise or wait out the limit in the provider's console ([costs per provider](providers/api-keys.md#6-costs-and-limits)); meanwhile `/cm fallback <provider>` |
| Anthropic or OpenAI on its API key: nothing of it is served | “its selectors are not served”, with the reason: the key is not set, the route is not approved, or no model is reviewed for it | a missing key: `charmer providers set-key openai` (or `anthropic`); a route not approved: `charmer providers transport openai api-key`; or back to the account: `charmer providers transport openai oauth-pool` |
| OpenAI on its API key: some OpenAI models are gone | doctor lists the lines reviewed for the key route and those not served | expected: the key serves only the reviewed models; bind one of those, or switch back to the ChatGPT account |
| OpenAI on its API key: long answers cost more than expected | none: each answer is bounded by Claude Code's output cap, not by a spend limit | set a hard project spend limit with OpenAI ([providers/api-keys.md](providers/api-keys.md#openai)) |
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
| a model you added now ships; doctor or `charmer models show <line>` says `operator line KEY (P/W) is now provided by the catalog as S: yours yields to it — remove it: charmer models rm KEY --successor S` | `KEY` is your key, `P/W` its provider/wire and `S` the shipped line; your key resolves to `S`, not a broken route | run the named `charmer models rm <line> --successor <line>` after reviewing it; if a live session blocks removal, exit it first. Profiles and named bindings move to `S`; the update leaves running sessions on their captured routes until resume ([update details](update.md#models-you-added-that-now-ship)) |
| `charmer providers validate` says `providers.d/P.json: lines.KEY: P/W is now provided by the catalog line "S" — yours yields to it; remove it: charmer models rm KEY --successor S` | a non-blocking finding (exit 0): the catalog now provides that model | use the named successor-removal command, then validate again; other models you added remain usable |
| `charmer models` shows `KEY\tW\tnow provided by the catalog as S\tremove yours: charmer models rm KEY --successor S` | the duplicate declaration yields to `S`; `\t` represents a tab and `W` is the default display name (a name you chose is kept) | remove your duplicate with `charmer models rm <line> --successor <line>`, then check `charmer models` |
| `alias <alias> (captured for KEY) has no line — prune when unused: charmer doctor --prune-aliases` | a removed model left a captured route; it stays available for sessions that still use it | once unused, remove the named route with `charmer doctor --prune-aliases <alias>`; a live reference refuses removal |
| adding Haiku says `charmer: models add: anthropic/claude-haiku-5-5 ships with charmer as the line haiku — nothing written; bind it in a profile (charmer profile edit NAME) or see charmer models` | the release already ships it; no declaration is needed | bind `haiku` with **E** on the launch card or `charmer profile edit <name>` |
| adding Kimi says `charmer: models add: openrouter/moonshotai/kimi-k3.1-pro ships with charmer as the line or-kimi — nothing written; yours (custom-moonshotai-kimi-k3-1-pro) yields to it — remove it: charmer models rm custom-moonshotai-kimi-k3-1-pro --successor or-kimi` | your earlier declaration still exists, but resolves to `or-kimi` | run the named removal command; bind the shipped line instead of adding another copy |
| `charmer: models add: <wire> cannot be declared: <why> — nothing written` | the wire id is outside this release's grammar: capitals, a leading `~`, a colon suffix such as `:free`, or a trailing `[1m]` | use a documented lowercase id or lowercase dated OpenRouter id; do not guess by changing case or stripping a suffix. [Model id limits](guides/models.md#model-id-limits) give alternatives |
| an operator ledger or evidence store is unreadable or invalid | malformed collection data is refused, not repaired automatically | follow the named diagnostic; restore a damaged ledger from a known backup, never delete it to clear the warning. Optional evidence can remain unavailable without rewriting its store |
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
| setup or doctor says “Claude Code copy cleanup blocked” | ownership or process/install facts could not be checked safely; nothing was removed | resolve the reported cause, then `charmer setup --step claude`; do not force-delete copies | doctor no longer reports the blocker |
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
| “scope differs from the record-authoritative compile” | also covers unexpected files or directories and incorrect ownership or permissions, even when compiled contents match: `charmer doctor --repair <id>` |
| “scope unreadable” | doctor could not verify the scope, including a read failure during a concurrent resume's scope swap; use the printed `charmer doctor --repair <id>` command |
| “its subagent status line differs from this release's compile” | expected after a release or your status-line settings change: resume with `charmer -r <id>`, or run `charmer doctor --repair <id>` now (`charmer doctor --repair-all` for stopped sessions); this is Attention, not damage |
| after updating, doctor says `fence/picker differ from the installed catalog (catalog changed since launch); applies at the next resume` | exit and resume with `charmer -r <id>`; the new catalog and the background-request and compaction settings apply then, not to the already running client ([update details](update.md#settings-at-the-next-resume)) |
| after a live Haiku bind, doctor says `session <id8>: background requests move to claude-haiku-5-5[1m] at the next resume` | exit and resume with `charmer -r <id>` to change the model for background requests; after unbinding, the message names the lead's selector instead. Reloading plugins alone does not change this launch-time setting |
| doctor groups the finding as `{n} sessions: background requests move with their lineup's haiku binding; each at its next resume` | resume each affected session when ready; `charmer doctor -v` names every session separately |
| single-session repair is deferred (exit 3) | `charmer doctor --repair <id>` could not complete scope repair under the installed catalog; resume interactively with `charmer -r <id>` to apply the catalog or choose a model. The sessions screen reports the same deferral |
| relink says “scope convergence is deferred until resume or a model choice” (exit 1) | the identity change was saved, but scope repair is deferred; use the printed `charmer -r <id>` command |
| relink says “scope convergence is unfinished” (exit 1) | the identity change was saved, but scope repair failed or was skipped; resolve the reported cause, then run the printed `charmer doctor --repair <id>` command |
| a plain session listing exits 1, or its JSON says unavailable | the inventory could not be read completely; use `charmer sessions list` to see readable rows and the errors, then `charmer doctor`. Do not treat the JSON result as an empty list |
| “<n> session(s) need a profile or model choice (lead removed)” | the lead each was bound to left the catalog: resume one interactively (`charmer -r <id>`, which asks for a profile or model), or `charmer lineup --session <runtime-id> --relaunch profile <name>`; **H** → Enter on the row opens Sessions. Doctor names the first three ids; `doctor -v` lists every one |
| “<n> session scope(s) were compiled by <earlier name>” | the scopes of sessions from before the rename, one line per remedy: `charmer doctor --repair-all` recompiles the stopped ones the installed catalog can recompile now (**H** → Enter on the row, after a confirmation); a lineup the catalog defers (it no longer evaluates, or resolves an agent differently) is recompiled at the session's next resume (`charmer -r <id>`); a lineup whose lead left the catalog needs a profile or model choice first (`charmer sessions choose --needs-choice --profile <name>`, or resume interactively). A running one is recompiled at its next resume |
| “session record <id> is unreadable” | `charmer sessions show <id>` names the error; never edit the record; forget a disposable session with `charmer sessions forget <id>` |
| resume says the session does not exist | `charmer -r <id>` (it resumes from the recorded directory) |
| a session is missing from the list | **C** on the sessions screen (all directories) |

More: [guides/sessions.md](guides/sessions.md).

## Inside a session

| Symptom | Fix |
| --- | --- |
| your own agent rows appear instead of charmer's | a user, project or local `subagentStatusLine` setting wins; doctor reports that choice. To use charmer's rows, remove your override, fix any unreadable settings layer, then resume ([the agent panel](guides/sessions.md#the-agent-panels-rows)) |
| only Claude Code's native agent rows appear | run `charmer doctor`: an earlier scope compile needs a resume or `charmer doctor --repair <id>`; if a settings file cannot be read, fix its JSON, size or access first, then resume. Repair alone does not fix unreadable user settings |
| `/cm` fails: `charmer` not found | `charmer` must be on the session's PATH |
| `/cm` changed nothing for new agents | type `/reload-plugins`, then start agents fresh |
| `/model` refuses a model | it is outside the lead set: relaunch with a profile whose lead is that model |
| an MCP server cannot find its API key | managed sessions remove every `*_API_KEY` variable except the names you keep: add its name in Settings (**O**) → kept environment; provider keys are never kept. **V** on the card shows kept and removed names, doctor too. Applies at the next launch or resume |
| an HTTP MCP tool call was interrupted | Claude Code retries after a closed connection only when the server marks the tool read-only or idempotent; a state-changing tool marked idempotent can still run twice. An interrupted call may have run even when it reports an error: check the external system before asking again ([client limits](reference/compatibility.md#deferred-client-fixes)) |
| an MCP tool, WebFetch, WebSearch or SendMessage is missing, or ToolSearch runs first | expected: behind the gateway some tools are listed by name and fetched with ToolSearch when needed; this is not a missing permission or installation |
| a provider refusal takes a while to appear, or the same refused request appears six times | Claude Code 2.1.311 resends an unrecognised refusal up to five times before showing it; assess the final error before retrying ([client limits](reference/compatibility.md#verified-behavior-and-limits)) |
| a Codex capacity refusal (429) has no `Retry-After` delay | on both OpenAI routes the refusal reaches Claude Code without the provider's delay; wait and retry, or switch route |
| a refusal is retried despite the provider's retry headers | the gateway does not forward a provider's `x-should-retry` or `Retry-After` on gateway routes, and the client applies its own retry rules; assess the final error before retrying |
| doctor reports `DISABLE_AUTO_COMPACT` as Info | managed sessions re-enable compaction despite this user setting; plain `claude` does not compact. No managed-session remedy is needed |
| managed compaction is disabled; doctor reports `DISABLE_COMPACT` as Attention | remove `DISABLE_COMPACT` from your Claude Code user settings env, then `/exit` and resume with `charmer -r <id>`; the compiled setting cannot override it on 2.1.311 |
| an installed mod changes an agent's model | an `agent.spawn` mod can choose another lineup model; a `settings.read` mod can also widen the allowed model list. Disable the mod if the compiled lineup must hold; charmer's fence does not constrain user-installed code ([client limits](reference/compatibility.md#verified-behavior-and-limits)) |
| implementers “unavailable here” | start the session in a Git repository (`git init`) |
| Remote Control, `/schedule` or claude.ai connectors are missing | expected in managed sessions; use plain `claude` for these |

Doctor and **H → Advisories** inspect bounded text from manifest-declared
hook modules without executing them. A literal `agent.spawn` declaration
does not establish that the mod is enabled or active. The report names the
locations it scanned and excluded; this check does not cover every load
location, imported code or other hook kinds. It does not change the
readiness verdict or certify that the lineup is safe from user-installed
code.

The pinned client carries the sandbox-boundary, worktree and Bash-permission
fixes from Claude Code 2.1.304–2.1.309 and 2.1.311's Linux sandbox fix for
overlapping added or plugin directories; charmer has not tested those fixes.
Do not rely on its Bash sandbox as the only boundary for untrusted code;
charmer does not replace Claude Code's sandbox or worktree protections.
See [client fixes](reference/compatibility.md#deferred-client-fixes).

### Late-stream completion limit

Re-measured on 2026-10-27 with Claude Code 2.1.311 and CLIProxyAPI 8.0.44,
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
| a write fails on a full or read-only disk | resolve the named local problem, then retry. A failure before replacement leaves the old file; “file was written; durability unconfirmed” means new bytes are present but directory persistence was not confirmed. Follow the command's recovery advice rather than assuming nothing changed |
| export says it wrote the file but durability is unconfirmed | no export receipt was recorded; fix the storage problem, then rerun `charmer export --out <file>` and check its result |
| doctor reports an invalid export receipt | the receipt is kept unchanged and is not proof of a completed export; create a fresh one with `charmer export --out <file>` |
| catalog promotion reports incomplete rollback | inspect the paths the command names before retrying promotion; some replacements remain. Individual file writes are atomic, not the whole multi-file promotion |
| the full-screen interface misbehaves | `charmer --line` uses plain lines; `--no-color` (or `NO_COLOR`) turns colour off |
| a profile says “cannot start the editor” | fix `$VISUAL` (used first) or `$EDITOR`, then retry; the profile was not saved and the message names the temporary copy kept for recovery |
| output has replacement characters | the selected stream encoding cannot show the text; use `PYTHONIOENCODING=utf-8` with a UTF-8 terminal or consumer, especially for JSON |

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
