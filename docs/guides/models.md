# Models

A **model line** is one model charmer can bind to a role, under a
stable key (`opus`, `sol`, …). The release's catalog ships reviewed lines;
you can add your own. `charmer models` (or **M** in the launcher)
lists them all.

## Kinds of lines

| Kind | Where it comes from | Before you can bind it |
| --- | --- | --- |
| catalog line | the release, reviewed | connect its provider |
| your line | `charmer models add <provider> <wire> <options>`, or **A** in Providers | a valid declaration and a usable provider route; admission and qualification are optional |
| candidate | the pinned gateway's registry or a provider's listing | nothing: a candidate is advice, never a grant; declare it to make it your line |

A line belongs to a **provider** (where it runs: OpenRouter, Anthropic, a
server of yours) and has a model **family** label (who makes it: Anthropic,
OpenAI, DeepSeek, …). You may supply any nonempty, single-line printable
label up to 64 characters, including `mistral`; controls and secret-bearing
values are refused. A line's own label overrides its provider's default;
an omitted aggregator family is `unknown`. Supply `--family LABEL` on
`models add` or `discover PROVIDER --add WIRE` to override the default for
that one model. On `discover`, `--family` needs exactly one `--add` wire;
it is not a listing filter. Omitting it keeps the existing defaults,
including `unknown` for OpenRouter stealth models. No maker is inferred
from a model id, and the label changes no review-independence rules.

A label is not a certificate of review independence. Review routing prefers
recognized different families, calls recognized equal families **same-family**,
and otherwise reports **independence unknown**. Unrecognized families remain
usable for leads, agents and reviews.

## The release's lines

<!-- generated: model-lines (tools/docs_gen.py) -->

| Key | Model | Provider | Family | Efforts (default) | Context | Roles | `/model` selectors |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `astra` | GPT-6 Astra | OpenAI | openai | low, medium, high, xhigh, max (high) | 1M class, provider window 872K | lead and agents | `gpt-multi-astra-<effort>[1m]` |
| `deepseek-flash` | DeepSeek V4.1 Flash | DeepSeek | deepseek | high, max (high) | 1M class, provider window 1M | lead and agents | `charmer-deepseek-flash-<effort>[1m]` |
| `fable` | Fable 5.1 · 1M selector | Anthropic | anthropic | low, medium, high, xhigh, max (high) | 1M class, provider window 1M | lead and agents | `claude-fable-5-1[1m]` (the effort is chosen in Claude Code) |
| `haiku` | Haiku 5.5 · 1M selector | Anthropic | anthropic | low, medium, high, xhigh, max (medium) | 1M class, provider window 1M | agents | `claude-haiku-5-5[1m]` (the effort is chosen in Claude Code) |
| `kimi-code` | Kimi for Coding | Kimi | moonshot | low, high, max (max) | 1M class, provider window 1M | lead and agents | `charmer-kimi-code-<effort>[1m]` |
| `luna` | GPT-6 Luna | OpenAI | openai | low, medium, high, xhigh, max (max) | 1M class, provider window 872K | lead and agents | `gpt-multi-luna-<effort>[1m]` |
| `muse` | Muse Wonder 1.0 | Meta | meta | low, medium, high, xhigh (xhigh) | 1M class, provider window 1M | lead and agents | `charmer-muse-1-0-<effort>[1m]` |
| `muse-contributor` | Muse Spark 1.3 Contributor | Meta | meta | low, medium, high, xhigh (high) | 1M class, provider window 1M | lead and agents | `charmer-muse-contributor-<effort>[1m]` |
| `opus` | Opus 5.5 · 1M selector | Anthropic | anthropic | low, medium, high, xhigh, max (high) | 1M class, provider window 1M | lead and agents | `claude-opus-5-5[1m]` (the effort is chosen in Claude Code) |
| `or-deepseek` | DeepSeek V4.1 Flash 1017 · OpenRouter | OpenRouter | deepseek | high, xhigh (high) | 1M class, provider window 1M | lead and agents | `charmer-or-deepseek-4-1-1017-<effort>[1m]` |
| `or-gemini` | Gemini 3.8 Flash · OpenRouter | OpenRouter | google | high, xhigh (high) | 1M class, provider window 1M | lead and agents | `charmer-or-gemini-<effort>[1m]` |
| `or-gpt` | GPT-6.1 Sol · OpenRouter | OpenRouter | openai | high, xhigh (high) | 1M class, provider window 1M | lead and agents | `charmer-or-gpt-<effort>[1m]` |
| `or-grok` | Grok 4.7 · OpenRouter | OpenRouter | x-ai | high, xhigh (high) | 200K class, provider window 500K | lead and agents | `charmer-or-grok-<effort>` |
| `or-kimi` | Kimi K3.1 Pro · OpenRouter | OpenRouter | moonshot | low, high, xhigh, max (high) | 1M class, provider window 1M | lead and agents | `charmer-or-kimi-3-1-<effort>[1m]` |
| `or-opus` | Opus 5.5 · OpenRouter | OpenRouter | anthropic | high, xhigh (xhigh) | 1M class, provider window 1M | lead and agents | `charmer-or-opus-<effort>[1m]` |
| `qwen-flash` | Qwen3.8 Flash | Qwen | alibaba | low, medium, xhigh (xhigh) | 1M class, provider window 983,616 | lead and agents | `charmer-qwen-flash-<effort>[1m]` |
| `qwen-max` | Qwen3.8 Max | Qwen | alibaba | low, medium, xhigh (xhigh) | 1M class, provider window 983,616 | lead and agents | `charmer-qwen-max-<effort>[1m]` |
| `sol` | GPT-6.1 Sol | OpenAI | openai | low, medium, high, xhigh, max (high) | 1M class, provider window 872K | lead and agents | `gpt-multi-sol-6-1-<effort>[1m]` |
| `sonnet` | Sonnet 5.5 · 1M selector | Anthropic | anthropic | low, medium, high, xhigh, max (high) | 1M class, provider window 1M | lead and agents | `claude-sonnet-5-5[1m]` (the effort is chosen in Claude Code) |

20 retired keys resolve to their successors; `charmer models` lists them.

<!-- end of generated: model-lines -->

### Haiku on the Claude account

`haiku` is Haiku 5.5 on the Claude account, for agents only: an explorer
(`cm-explorer`) and light writer (`cm-implementer-light`). It is the same
family as Opus, Sonnet and Fable, not an independent reviewer of them.
Measured on 2026-10-27 with Claude Code 2.1.310 and CLIProxyAPI 8.0.42:
smoke, all five efforts, automatic tool use, streaming and the offline
exact-client check passed. The 1M context is documented, not measured near
its limit; the validated floor remains 200,000 tokens.

Every managed session names the model for Claude Code's own background requests
(summaries and the built-in claude-code-guide): Haiku 5.5 when the profile uses
the haiku model, otherwise the lead, so a value in your own or your project's
settings cannot send them outside the session's models.

### Lines moved in this release

- `muse` uses Muse Wonder 1.0 (`muse-wonder-1.0`) on Meta's Model API.
  Smoke, low/medium/high/xhigh, automatic tool use and streaming passed on
  2026-10-27. Thinking stays on; xhigh is the top effort on this route.
- `or-deepseek` uses DeepSeek V4.1 Flash 1017
  (`deepseek/deepseek-v4.1-flash-1017`) on OpenRouter. Smoke, high/xhigh,
  forced tool use and streaming passed on 2026-10-27.
- `or-kimi` uses Kimi K3.1 Pro (`moonshotai/kimi-k3.1-pro`) on OpenRouter.
  Smoke, high/xhigh and streaming passed on 2026-10-27; forced named tool
  choice was rejected (HTTP 400), and automatic tool choice passed. Claude
  Code's agent loop uses automatic tool choice. Low and max are
  contract-level support, not live-qualified efforts.

Those measurements used Claude Code 2.1.310 and CLIProxyAPI 8.0.42; they
are not new measurements on this release's pins. Earlier models keep
serving running sessions until they resume. The first-party
`deepseek-flash` line keeps DeepSeek's canonical alias: the provider's docs
checked on 2026-10-27 say it serves V4.1-Flash-1017, but that snapshot has
not had a live acceptance check on the direct route.

`muse-contributor` stays on Muse Spark 1.3 Contributor: Meta answered
HTTP 404 for `muse-wonder-1.0-contributor` on 2026-10-27. Meta may train on
prompts and outputs sent to the contributor tier; do not send confidential
work. Muse Wonder Full is restricted to Muse Code, not Meta's public Model
API. Candidates and declarations name these restrictions; listing an id
does not make that route available.

## Inspect

```bash
charmer models                    # every line: generation, provider, class, efforts, status
charmer models show <line>        # one line: origin, selectors, efforts, context, admission, evidence
charmer models --candidates       # advisory candidates (nothing declared or admitted)
```

The listing shows each line's generation, provider, context class,
efforts and status, its typed `/model` selectors, and the retired keys
with their successors. Three separate facts matter:

- **Use availability:** a valid declaration, an enabled provider and a usable
  route. Route approval, a required credential and a supported selected
  transport still matter; an unavailable line stays visible with its remedy.
- **Admission:** an optional, definition-bound local badge. A new line is
  **New · not admitted**, not off. Missing or stale admission is Attention,
  not a reason to hide it or refuse a binding.
- **Qualification:** optional diagnostic evidence, shown as not run, passing,
  failed or stale. Failure stays visible; being usable never turns it into
  a pass or promises that a provider request will work.

A line whose provider has no key or sign-in is shown as not connected on the
card and in **M**; a line the running gateway does not serve is a doctor
finding with its fix. Local readiness means locally configured and served,
not upstream verified.

**Direct-mode exception:** `charmer direct --model <line>` treats a
missing credential as advisory ("requests may fail") and still launches.
The TUI Direct screen warns and asks **Launch anyway?**, default **No**;
answering Yes continues despite the missing credential. Profile and binding
paths retain their credential refusals. This narrow Direct exception does
not bypass route approval, an explicitly disabled provider or an unusable
selected transport; no credential or alternate transport is supplied implicitly.

## Add a model of your own

No step happens by itself; every request to a provider is listed and
needs your yes, in a terminal outside Claude Code (no flag waives this).

In the launcher: **G** Providers → select the provider → **A** add models
(a listing you agree to, with checkboxes, or entry by hand), then return to
**E** on the launch card to bind the line in the profile editor. Skipping
admission does not prevent selection. In **M** Models, Enter changes the
optional admission badge and **Q** offers optional diagnostics.

The model form lets you choose a **Display name** (the listing's name or
wire id by default). It is separate from the stable local key. On a new
model, leave the key blank to derive a `custom-…` key from the wire id;
collisions get a numeric suffix, and the declaration preview shows the
final key before anything is written. Editing can change the display name,
not the key. Cancel at either the form or preview writes nothing.

The `custom-` prefix reserves your local ids against current and future
catalog keys, so a catalog update cannot take over their names. Your
display name can be chosen freely (`models add --display` on the CLI).
Text fields show the insertion cell, scroll horizontally as you edit,
and support Left/Right, Home/End, Backspace and Delete; F1 opens their help.

With the provider already connected and its route usable, add and bind
without either optional step:

```bash
charmer models add <provider> <wire> --context <n> --source docs --source-ref "URL, date" --as custom-example
charmer profile edit <name>         # choose custom-example for the lead or a cm-* agent
```

A declaration records the wire id, a sourced context size
(`--source docs|registry` needs `--source-ref` with a URL and date),
efforts and role recommendations. A listing without a context needs one
you source; a context above the listed one needs `--over-listed REASON`.
An explicit binding overrides capability and role recommendations with a
warning, including on supported LAN routes and legacy custom lines. It
does not create a missing selector, effort mapping or route contract;
an agent does not inherit a model's lead-only environment.

### Optional admission and diagnostics

```bash
charmer models admit custom-example                 # optional local badge; zero inference requests
charmer models qualify custom-example --smoke        # optional minimal request, after explicit consent
charmer models qualify custom-example --agents       # optional effort, tools and streaming checks too
```

- **Admit** validates the local definition and asks before recording your
  attestation. It sends no inference request and needs no smoke pass,
  running gateway or provider credential just to record the badge. Changing
  the wire, context or efforts makes the badge stale; the display name does
  not. Admission never approves a credential destination.
- **Qualify** lists every bounded request and requires a human's explicit
  yes at a terminal outside Claude Code; the default is **No**, and no flag
  waives it. Checks include `--smoke`, `--efforts`, `--tools` (with
  `--tool-choice forced` or `auto`), `--stream` and `--context`; `--agents`
  combines smoke, efforts, tools and streaming. Qualification writes evidence
  only: it never admits or edits a line. A network failure is inconclusive
  and never erases an earlier pass. Failed tool checks remain failures;
  automatic-only tool evidence is not a forced-tool pass.
- **Use is your choice.** Missing, failed or stale evidence warns for leads,
  agents and workflow defaults; it does not prohibit the binding. Selection,
  launch, doctor and import never run qualification automatically. Declining
  diagnostics sends no request.

When the client or gateway pin changes in a release, earlier evidence stays
recorded but is shown as stale, not a current verification. Doctor shows
Attention suggesting optional requalification.

A provider whose key you save before it has any model continues into this
journey by itself (G → K on a provider with no models).

### A new model on the Claude or ChatGPT account

A new first-party model on a signed-in account can be added the same way:
select Anthropic or OpenAI in **G**, **A** → enter it by hand with its
documented context and efforts. It needs no new route approval. Its agent
qualification also runs an offline check of the pinned Claude Code with
that model id. Missing, failed or unavailable exact-client evidence is a
warning, not an agent-use prohibition. The selected account or API-key
transport must still support the route; there is no implicit fallback.

### GPT-6.1 Terra on the ChatGPT account

Terra is not a shipped line. With OpenAI connected through your ChatGPT
account, declare it with a key of your own, then optionally qualify it in a
terminal outside Claude Code. The context below is the ChatGPT account's: the
gateway's model registry (CLIProxyAPI 8.0.45) lists Terra there with
500,000 tokens; the 1,050,000 in OpenAI's API documentation applies to the
API-key route. Recheck it before declaring.
The five efforts use the account route's reasoning-effort contracts:

```bash
charmer models add openai gpt-6.1-terra --as custom-terra --context 500000 --source registry --source-ref "https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.45/internal/registry/models/models.json, 2026-10-27" --effort low=reasoning-effort-low --effort medium=reasoning-effort-medium --effort high=reasoning-effort-high --effort xhigh=reasoning-effort-xhigh --effort max=reasoning-effort-max --default-effort medium --display "GPT-6.1 Terra"
charmer models qualify custom-terra --agents
```

The declaration makes no provider request. Qualification lists its requests
and asks before sending any; it also checks the pinned Claude Code offline.
There is no shipped Terra qualification result. On 2.3.0, agent
qualification for a ChatGPT-account line prepares that offline check before
consent: a preparation failure says nothing was sent, rather than losing
the results of a battery already sent. Once declared, bind `custom-terra`
with **E** on the launch card. This recipe does not enable the OpenAI API-key
route for Terra.

### Kimi K3.1 Pro on the Kimi provider

The built-in `kimi` provider is the Kimi Code subscription, not Moonshot's
pay-per-token API. To add K3.1 Pro by hand on that route:

```bash
charmer models add kimi k3.1-pro --as custom-kimi-pro --context 262144 --source docs --source-ref "https://www.kimi.com/code/docs/en/kimi-code/models.html, 2026-10-27" --effort low=output-config-low --effort high=output-config-high --effort max=output-config-max --default-effort high --display "Kimi K3.1 Pro"
```

This route offers low, high and max, not medium. The gateway strips the
client's thinking parameter for Kimi; the declared wire stays `k3.1-pro`.
The add path, effort mapping and tool/reasoning round trip were checked
against a fake provider through the pinned gateway, not a live Kimi account.
Check the provider's context documentation before declaring; the declared
bound does not give each agent a separately adjustable window.

### MiniMax M3.1 Flash on OpenRouter

With OpenRouter connected, use its lowercase id and all five output
contracts, with max as the default:

```bash
charmer models add openrouter minimax/minimax-m3.1-flash --as custom-minimax-flash --family minimax --context 1048576 --source docs --source-ref "https://openrouter.ai/minimax/minimax-m3.1-flash, 2026-10-27" --effort low=output-config-low --effort medium=output-config-medium --effort high=output-config-high --effort xhigh=output-config-xhigh --effort max=output-config-max --default-effort max --display "MiniMax M3.1 Flash"
```

The declaration and rendered effort mappings are checked offline; this is
not a live qualification or a near-limit context measurement. OpenRouter
may map effort levels to those a model offers, so accepting a level does
not prove a distinct reasoning depth. The `minimax` family label does not
establish review independence unless the catalog recognizes it. For either
model, bind it with **E** on the launch card; **M → Q** offers optional
qualification with a request plan and explicit consent.

charmer 2.2.1 does not accept the low, medium and max contracts on
OpenRouter; see [before going back to 2.2.1](../update.md#before-going-back-to-221)
before a rollback.

### Model id limits

Use a provider's documented lowercase id, or a lowercase dated id on
OpenRouter. Mixed-case vendor ids, moving ids starting with `~` (such as
`~deepseek/deepseek-flash-latest`), colon-suffixed ids such as
`deepseek/deepseek-v4.1-flash:free`, and ids ending in `[1m]` cannot be
declared in this release. `models add` says which part is unsupported and
that nothing was written. Do not just lowercase an id or strip its suffix:
that may name a different model. Use an alternative the provider actually
documents, such as `minimax/minimax-m3.1-flash` on OpenRouter instead of the
direct vendor id `MiniMax-M3.1-Flash`.

The wire id is what the provider receives; charmer adds `[1m]` to its own
selectors from `--context` for Claude Code's context class. On Kimi Code,
`k3.1-pro[1m]` is the provider's own id for its 1M-context model: it cannot be
declared in this release, and a larger `--context` on `k3.1-pro` does not
reach it. Declare `k3.1-pro` with its documented 262,144 tokens, or use the
shipped `or-kimi` line for K3.1 Pro's 1M context on OpenRouter.

## Revoke, remove, replace

**Revoke removes only the admission badge. The line remains usable, and
qualification evidence is unchanged.** It sends no inference request and
does not disable the route, remove a binding or change a session's fence.
To stop using a model, remove its binding or declaration; to stop using a
provider, disable that provider in Settings.

```bash
charmer models revoke <line>                      # optional badge only; not an availability control
charmer models rm <line>                          # a line you added
charmer models rm <line> --successor <line>       # also rewrite the profiles and bindings that use it
```

A line a live session uses is not removed out from under it: the gateway
keeps serving the session's selector (a continuity alias) until no live
session uses it; `charmer doctor --prune-aliases` removes unused
ones.

## Context windows

One **window ceiling** governs the lead and every agent: 200K to 800K
tokens, 800K by default.

```bash
charmer window-ceiling            # show it, and whether it is set or the default
charmer window-ceiling 400K       # set it (applies at the next launch or resume)
charmer window-ceiling --reset    # back to 800K
```

Settings (**O**) has the same row. Claude Code's own `--autocompact` flag is
refused after `--`: the window is charmer's. A session's process window is
the smaller of the ceiling and the smallest provider bound among its lead set. A
1M-class agent whose provider bound reaches that window keeps the 1M class
and shares that window; one whose bound falls below it uses the existing
200K fallback. In scalar-window sessions, agents share that process policy.
A new binding using a legacy suffixless selector uses the actual scalar or
200K client class, not 1M merely because its declaration says 1M. There is
no separately adjustable per-agent provider-sized window.

For example, with an 800K process window a line bounded at 872K can keep
its 1M class, while a 500K line falls back to 200K. A 128K provider bound
cannot make a 200K-class agent's window 128K: a request can overflow the
provider before the client compacts. The card, lineup summary (`/cm show`,
`charmer profile show <name>`) and `--print-launch` show the actual
client class and effective window; warnings name the process window,
compaction trigger and provider bound where they mismatch. Such predictions
warn rather than block. Never inflate declared capacity to hide a warning.
Concrete invalid-window or native selector limits still refuse.

A session recorded with agents in another class keeps them until its next
resume, which shows each move (for example "agent class 200K → 1M");
doctor lists pending moves as Attention.

Third-party providers' token counts are estimated locally by the gateway,
so context figures on those models are approximate.
