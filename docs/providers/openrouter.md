# Connect OpenRouter

OpenRouter routes one API key to models from many vendors. charmer
ships OpenRouter as a built-in provider with reviewed model lines and a
shipped `openrouter` profile, so OpenRouter alone gives you a working
setup: connect the key, choose the profile, launch.

## 1. What you need

An OpenRouter account with credit and an API key from it. The key is
saved as `OPENROUTER_CLAUDE_API_KEY` and sent only to
`https://openrouter.ai/api`.

## 2. Connect, then choose a profile

1. **G** Providers → OpenRouter → **K**, type the key (hidden). Or:

   ```bash
   charmer providers set-key openrouter
   ```

2. Choose a profile. The shipped `openrouter` profile runs every role on
   OpenRouter's reviewed lines; `charmer profile starter` previews a
   starter built from what is connected. On the card, **P** lists the
   profiles and **D** makes one the default.

   ```bash
   charmer profile show openrouter
   charmer --profile openrouter
   ```

The OpenRouter catalog lines are usable once the provider is connected.
The two New lines, Gemini 4 Carbon and Neon, have optional admission
badges. Neither needs admission to use. The `models qualify` command is
for lines you declare yourself, not catalog lines.

<!-- generated: openrouter-lines (tools/docs_gen.py) -->

| Key | Model | OpenRouter model | Family | Efforts | Context |
| --- | --- | --- | --- | --- | --- |
| `or-deepseek` | DeepSeek V4.1 Flash 1017 · OpenRouter | `deepseek/deepseek-v4.1-flash-1017` | deepseek | high, xhigh | 1M class, provider window 1M |
| `or-gemini` | Gemini 4 Hydrogen · OpenRouter | `google/gemini-4-hydrogen` | google | low, medium, high, xhigh | 1M class, provider window 1M |
| `or-gpt` | GPT-6.2 Sol · OpenRouter | `openai/gpt-6.2-sol` | openai | low, medium, high, xhigh, max | 1M class, provider window 1M |
| `or-grok` | Grok 4.7 · OpenRouter | `x-ai/grok-4.7` | x-ai | high, xhigh | 200K class, provider window 500K |
| `or-kimi` | Kimi K3.1 Pro · OpenRouter | `moonshotai/kimi-k3.1-pro` | moonshot | low, high, xhigh, max | 1M class, provider window 1M |
| `or-opus` | Opus 5.5 · OpenRouter | `anthropic/claude-opus-5.5` | anthropic | high, xhigh | 1M class, provider window 1M |

The shipped `openrouter` profile: OpenRouter only (billed per token): Opus lead and strong writer, GPT analysts and writer, DeepSeek explorer and light writer, Gemini and Grok reviewers.

| Role | Line | Effort |
| --- | --- | --- |
| lead | `or-opus` | ultracode |
| `cm-analyst` | `or-gpt` | high |
| `cm-analyst-strong` | `or-gpt` | xhigh |
| `cm-explorer` | `or-deepseek` | high |
| `cm-implementer` | `or-gpt` | high |
| `cm-implementer-light` | `or-deepseek` | high |
| `cm-implementer-strong` | `or-opus` | xhigh |
| `cm-reviewer` | `or-gemini` | high |
| `cm-reviewer-strong` | `or-grok` | xhigh |

<!-- end of generated: openrouter-lines -->

`or-gpt` moved to GPT-6.2 Sol in 2.6.0; its agent checks (smoke,
high and xhigh, forced named tool, streaming) passed on 2026-10-02 on
GPT-6.1 Sol, not on 6.2. A managed high turn on 6.2 passed on 2026-11-18
(HTTP 200, exit 0; Claude Code 2.1.326 / CLIProxyAPI 8.0.76), not a new
full battery or context-limit test. Low, medium and max are contract-level
only (listing-advertised, not measured); xhigh has not been live-checked
on 6.2. Default `high` stays. A session running on GPT-6.1 Sol keeps it
until it resumes
([models guide](../guides/models.md#lines-moved-in-260)).

`or-gemini` moved to Gemini 4 Hydrogen (`google/gemini-4-hydrogen`) in 2.6.0,
with low, medium, high and xhigh, default `high`. Low and medium are
contract-level only (listing-advertised, not measured); the earlier
high/xhigh battery ran on Gemini 3.8 Flash, not Hydrogen. The `openrouter`
profile follows the key without reseeding. Its new selectors are
`charmer-or-gemini-4-<effort>[1m]`; a running Gemini 3.8 Flash session keeps
its old wire through `or-gemini@3.8` until resume.

`or-gemini-carbon` is a New line on `google/gemini-4-carbon`, with low,
medium and high, default `high`, and selectors
`charmer-or-gemini-carbon-<effort>[1m]`.

`or-gemini-neon` is a New line on `google/gemini-4-neon`, with low,
medium, high and xhigh, default `high`, and selectors
`charmer-or-gemini-neon-<effort>[1m]`. Neither Carbon nor Neon is in a
shipped profile. Bind either as lead or agent in the profile editor, or
select it in the Direct picker. Enter in Models adds the optional
admission badge; it does not bind the line.

OpenRouter's public models list, read 2026-11-18, gives all three Gemini 4
lines 1,048,576 context tokens and 943,718 max completion tokens. One
AI Studio endpoint for Neon caps completion at 65,536. The listing dates
are 2026-11-12 for Hydrogen, 2026-11-10 for Carbon and 2026-11-18 for
Neon. All three list `tools`, `tool_choice`, `structured_outputs`,
`reasoning` and `reasoning_effort`. Every Neon endpoint lists all five,
plus `response_format` and `include_reasoning`. Google AI Studio lists Neon
as stable with four thinking levels. These are listing facts, not tests
through the gateway. The 1M provider bound is listing-based;
the validated floor of 200,000 is not a Gemini 4 capacity measurement.
Hydrogen's inherited `minimum_tested` pair (Claude Code 2.1.286 /
CLIProxyAPI 7.3.15) describes Gemini 3.8 Flash's 2026-10-02 tests. For
Carbon and Neon the pair (2.1.286 / 8.0.76) records the inherited client
floor and the gateway baseline, not a test. Their efforts come from the
listings.

Live checks on Gemini 4: in charmer 2.6.1's live check (2026-11-18 and 2026-11-19, effort `high`),
`or-gemini` and `or-gemini-carbon` passed one-shot turns, a remember turn and a subagent's report. A Read-tool turn, in a
fresh session and in a session resumed after a removal (which recalled a value from its first turn),
completed with the right content only after OpenRouter refused its first request with HTTP 400 three
times; in these runs the turn then completed by itself. `or-gemini-neon` was checked with one-shot turns
only. Context bounds were not measured, and the other efforts are contract-level.

### One-shot results

OpenRouter can end a Gemini 4 reply with its reasoning record (a
`redacted_thinking` block whose data starts `openrouter.reasoning:`) after
the text. Since 2.6.1 the gateway leaves that record out when it directly
follows the reply's text, the reply contains only text and reasoning, and
it ends the turn (`end_turn`). A one-shot result (`claude -p` or an SDK
caller) is then the text. The record never reaches Claude Code, so it is
missing from that conversation from then on, a resume included. Replies
with tool calls keep their records. Each removal logs
`openrouter reasoning record removed: records=N`.

## 3. Provider and model family

On OpenRouter, the provider is OpenRouter but each model belongs to its
own vendor's family (Anthropic, OpenAI, DeepSeek, Google, xAI, Moonshot
and so on). Reviews are routed by family, so an OpenRouter profile can
still get an independent review from another family. The card and
`charmer profile show <name>` show each slot's family.

When you add a listed OpenRouter model, charmer suggests a family from a
small set of vendor namespaces: `anthropic/` → `anthropic`, `openai/` →
`openai`, `google/` → `google`, `deepseek/` → `deepseek`, `x-ai/` → `x-ai`,
`moonshotai/` → `moonshot`, `qwen/` → `alibaba`, and `meta-llama/` or
`meta/` → `meta`. It also recognizes the prefixes `minimax/`, `mistralai/`
and `z-ai/`, but a suggestion is used only if the installed catalog already
recognizes that family; otherwise it stays `unknown`. This is an **editable suggestion from the id**, not verified model
provenance. Unknown vendors, `stealth/`, `openrouter/` routers and ids
without a vendor prefix stay `unknown`; an unknown family cannot establish
review independence. Providers you declare yourself do not get these
OpenRouter suggestions, even if their endpoint looks similar.

## 4. Other OpenRouter models

List models with **G** Providers → OpenRouter → **A** → **List models**, or
run this in a terminal outside Claude Code:

```bash
charmer discover openrouter
```

The public listing makes one anonymous request to `/api/v1/models`, after
confirmation. Every regular model stays visible, even with missing facts.
With a configured key, a **second, separately confirmed** request to
`/api/v1/models/user` adds account-only rows. It uses the same key, in the
Bearer header only. This listing is filtered by the account's provider
preferences, privacy settings and guardrails; it is not a complete inventory.
Declining or a failed account listing keeps the public rows. Without a key,
only the public listing is requested. Each request is bounded to 20 seconds
and 4 MiB, with no redirects or retries.

Both surfaces show advertised context, modality, tool support and
prompt/completion prices per token; missing or malformed facts are **unknown**.
Zero prompt and completion prices are marked **free (advertised)**, not a
promise that every feature or request is free. **?** in the TUI listing shows
the full facts when a row is wider than the terminal.

To declare a listed model, use `charmer discover openrouter --add <wire>`.
For a recognized vendor prefix, the result explains the default, for example:

```text
family: deepseek (suggested from deepseek/; --family LABEL to change)
```

`--family LABEL` always wins, including `--family unknown`, and adds no
family-suggestion note. In the TUI, the declaration form prefills the
family and shows **suggested from the deepseek/ prefix — edit to change**
on its family step. Review or edit it before saving. Accepted suggestions
are recorded in the declaration's notes. Existing model lines are not
relabelled automatically; editing, export and import never infer a family.
Manual `models add` remains a no-lookup path with an explicit `--family`
when you want one.

### Efforts for models you add

OpenRouter accepts the `output-config-low`, `output-config-medium` and
`output-config-max` contracts as well as high and xhigh. Map only the efforts
the model supports, for example `--effort low=output-config-low`,
`--effort medium=output-config-medium` and `--effort max=output-config-max`.
These contracts describe what charmer sends, not a live pass for every
model. The shipped `or-kimi` line adds low and max at contract level;
its measured high/xhigh checks passed, forced tool choice was rejected and
automatic tool choice passed on 2026-10-27.

See the [MiniMax M3.1 Flash example](../guides/models.md#minimax-m31-flash-on-openrouter)
for a declaration using all five contracts.

charmer 2.2.1 does not accept the low, medium and max contracts on
OpenRouter; see [before going back to 2.2.1](../update.md#before-going-back-to-221)
before a rollback.

### Stealth models and add by id

A `stealth/` id, such as `stealth/space-bunny-alpha`, is marked **stealth**:
**maker hidden; pre-release; may retain prompts; may disappear**. Its family
is `unknown`, so it cannot establish review independence. The `openrouter/`
namespace instead contains **routers**, not stealth models. Stealth models
use the same OpenRouter API and key, but the public listing may omit them.
The account listing may also omit them; whether an account lists a stealth
model is not established by the offline fixtures.

Add a known stealth id directly, even when neither listing contains it:

```bash
charmer discover openrouter --add stealth/space-bunny-alpha
```

This asks to fetch the anonymous
`/api/v1/models/stealth/space-bunny-alpha/endpoints` metadata, then explicitly
declares that id. Its advertised name, modality, context and tools are used
when present, otherwise left unknown. Facts stated per upstream endpoint
are used only when every endpoint agrees; disagreements stay unknown.
If the endpoint states no context,
repeat with `--context N` using a bound you have verified in the provider's
documentation; a declined or failed lookup also permits that manual bound.
No inference request is made. In the TUI use **A** → **Add stealth id**;
review the facts and the prefilled declaration before saving. **Manual entry**
(and `models add … --context N --source docs --source-ref URL`) remains a
no-lookup path.

Use lowercase dated ids the provider documents. Mixed-case vendor ids,
moving ids such as `~deepseek/deepseek-flash-latest`, colon-suffixed ids
such as `vendor/model:free`, and ids ending in `[1m]` cannot be declared in
this release. A listing can still show them as **not addable in this
release**; `models add` explains the unsupported part and writes nothing.
Do not strip a suffix or lowercase an id to declare a different model by
accident: choose a documented alternative. See
[model id limits](../guides/models.md#model-id-limits).

Listing alone never declares, enables, admits, binds or chooses a default.
Only `--add` or the TUI declaration preview writes a model of your own;
it changes no profile or provider enablement. Admission and qualification
are optional badges, not prerequisites to explicit use on an available
route. See [guides/models.md](../guides/models.md#add-a-model-of-your-own).

## 5. Replace or remove the key

Set the key again to replace it (charmer asks first);
`charmer providers remove-key openrouter` removes it. Profiles that
use OpenRouter then show the provider as not connected until you set a
key again.

## 6. Costs and limits

OpenRouter bills per token from your account's credit, at each model's
listed price, and every agent of a profile bills separately: a profile
that runs nine agents on OpenRouter can spend several times what a
single-model session does. charmer shows listing prices as observations
and enforces no spending cap. Use OpenRouter's own credit limits and per-key limits, and watch
usage in its dashboard. `charmer usage` counts requests, not tokens
or cost.

OpenRouter's own pages, checked on 2026-10-04:

- [Pricing](https://openrouter.ai/pricing): how OpenRouter charges;
  each model's per-token price is on its page in
  [the model list](https://openrouter.ai/models).
- [Limits](https://openrouter.ai/docs/api_reference/limits): the
  account's credit balance and the optional credit limit of each API key.
  A key at its limit, or a balance that cannot cover a request, returns
  HTTP 402; the response says which.
- [Activity](https://openrouter.ai/docs/guides/administration/activity-export):
  the activity page shows spend, tokens and requests, grouped by model or
  by API key.

A key of its own for charmer, with a credit limit, bounds what its
sessions can spend.

## 7. When it fails

| You see | Next step |
| --- | --- |
| 401 from OpenRouter | the key is wrong or revoked: set a new one |
| 402 in doctor's gateway lines | the account is out of credit: add credit, or move agents with `/cm fallback <provider>` |
| a Gemini 4 reply has text but the one-shot result is empty | check `charmer --version` and `charmer gateway status`; [update from 2.6.0](../update.md#updating-from-260), or, if updated but the restart is pending, run the guarded `charmer gateway restart` between turns, then `charmer update --verify` |
| one model refuses requests while others work | that model's upstream vendor may be unavailable on OpenRouter; bind another line with `/cm set <agent>=<model>` |
| a model of your own fails qualification | the check names the failing part; choose another model, never a weaker tool contract |
| a long session stops with a context-length error | the provider's context-window error does not start Claude Code's automatic compaction on this route: run `/compact`, then continue |

## 8. Terms

OpenRouter's terms and each upstream vendor's terms apply to your
requests. A model OpenRouter lists is not proof that your account may use
it at the price or limits you expect.
