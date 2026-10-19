# Quickstart

Connect one provider, choose a profile, launch, change one agent, then
resume the same session. No admission badge or provider inference test is
required to follow this path.

## Before you start

| You use | Install with |
| --- | --- |
| Linux | the release installer: [install/linux.md](install/linux.md) |
| macOS | the release installer: [install/macos.md](install/macos.md) |
| Windows | the Linux release inside WSL2: [install/windows-wsl2.md](install/windows-wsl2.md) |
| Nix (optional) | the flake package: [install/nix.md](install/nix.md) |

A release bundle carries its own Python runtime and gateway: you need
neither Python nor a preinstalled Claude Code. The installer verifies the
signed release, then starts `charmer setup` unless you pass `--no-setup`.
Run setup in a terminal outside Claude Code, with terminal input and output.

Setup gets charmer's own verified copy of the Claude Code version the
release pins. It copies an exact size-and-sha256 match from your existing
installation without changing it, or shows the download's address and size
and asks first (no credentials sent; answering no sends nothing).
`charmer setup --step claude` runs just this step.
[Compatibility details](reference/compatibility.md).

## 1. Connect one provider

Run `charmer`. On the launch card, **W — Get started** opens the setup
steps; **A** there connects a provider. Enter opens the highlighted step.
Without the full-screen interface, `charmer setup` runs the same steps as
plain lines; `charmer setup --status` shows progress.

Every route is optional; connect only what you want, and any single
provider gives you a working setup once it has a usable model. The picker
lists accounts and built-in providers, then presets, providers you added
and your own endpoints and servers. `charmer setup --step providers`
lists the same choices.

| Route | Credential | In the launcher | From a terminal | Alone gives you |
| --- | --- | --- | --- | --- |
| Claude account, ChatGPT account | an account sign-in, for personal use | **G** → **L** on Anthropic or OpenAI | `charmer providers sign-in anthropic` (or `openai`) | the shipped `claude` or `openai` profile |
| Anthropic or OpenAI API key | the provider's API key | **G** → Enter on Anthropic or OpenAI → the API key | `charmer providers transport anthropic api-key` (or `openai`) | the same Claude lines; for OpenAI, the models reviewed for its API (see the note below) |
| a built-in keyed provider: DeepSeek, Kimi, Meta, Qwen | the provider's API key | **G** → **K** on the provider | `charmer providers set-key <provider>` | its shipped lines, through a starter profile |
| OpenRouter | an OpenRouter API key | **G** → **K** on OpenRouter | `charmer providers set-key openrouter` | the shipped `openrouter` profile |
| a preset of a vendor's Anthropic-compatible endpoint (Z.ai, Moonshot AI, Model Studio, Novita AI, Vercel AI Gateway) | the vendor's API key | **W** → **A** → the preset | `charmer providers add --preset <name>` | the valid models you add on its approved route |
| a preset of an OpenAI-compatible endpoint (Groq, Mistral AI, xAI, Cerebras, Google Gemini) | the vendor's API key | **W** → **A** → the preset | `charmer providers add --preset <name>` | the valid models you add on its approved route |
| your own Anthropic-compatible endpoint (recommended for anything not listed) | the vendor's API key | **G** → **N** → Anthropic-compatible | `charmer providers add <name> --kind anthropic-compatible <options>` | the valid models you add on its approved route |
| your own OpenAI-compatible endpoint | the vendor's API key | **G** → **N** → OpenAI-compatible | `charmer providers add <name> --kind openai-compatible <options>` | the valid models you add on its approved route, through Messages-to-chat translation ([limits](providers/openai-compatible.md#translation-limits)) |
| a server on your network (Ollama, LM Studio, vLLM, llama.cpp, any OpenAI-compatible server) | none | **G** → **N** → the server | `charmer providers add --preset ollama` | the valid models you add, as leads or agents; no key required |

On the OpenAI API key, Claude Code's per-request output cap is not
applied: one answer can run up to the model's own output limit, billed
per token. Set a hard spend limit on the key's OpenAI project first
([OpenAI's spend limits](https://developers.openai.com/api/docs/guides/spend-limits);
[details](providers/api-keys.md#openai)).

Pages: [API keys](providers/api-keys.md),
[OpenRouter](providers/openrouter.md),
[Claude account](providers/claude-account.md),
[ChatGPT account](providers/chatgpt-account.md),
[Anthropic-compatible endpoints and presets](providers/anthropic-compatible.md),
[OpenAI-compatible endpoints](providers/openai-compatible.md),
[servers on your network](providers/lan.md).

For an endpoint you add, approve where its key is sent before using it.
"Connected" means its key or sign-in is saved, or it needs no key; served
models are shown separately. Neither proves your account is entitled to a
model or that a request will succeed. The optional connection test sends
one small request per selected provider after you agree, default **No**,
and may be billed. You can skip it and choose a profile.

If the provider has no models yet, add one by hand or from a listing you
explicitly agree to. Admission is optional: its local badge sends zero
inference requests and is not route approval. Optional diagnostics need
explicit consent at a terminal outside Claude Code, default **No**, may be
billed and never run automatically. Missing or failed evidence warns, not
prohibits use; route approval and technical limits still apply.
[Adding models and optional diagnostics](guides/models.md#add-a-model-of-your-own).

## 2. Choose a profile

A **profile** is a saved lineup: the lead model and the agents bound to
roles. Get started's profile step offers a connected profile or builds a
**starter** from what is connected. Confirming its starter offer (also
`charmer setup --step profile`) saves the starter and makes it your default.

On the launch card, **P** opens Profiles. **Enter** selects a profile for
this launch; **D** makes it the default. The card otherwise prefers the
profile last used in this directory, then your connected default, then
another connected profile.

From a terminal, `charmer profile starter` is a preview only. With
`--apply`, it asks and saves the profile, but does not make it the default;
launch it explicitly with `charmer --profile starter`. To choose a default
separately: `charmer profile default <name>`.
[More about profiles](guides/profiles.md).

## 3. Launch

Before starting long multi-agent work, set spending limits with your
providers: the lead and every bound agent bill separately on pay-per-token
providers. The card, Get started and the starter preview show a spend note
for the providers the profile uses.

```bash
charmer                      # review the launch card; Enter launches
```

Claude Code may show its own first-run and folder-trust prompts. Read and
answer them before continuing; they depend on its existing configuration,
not charmer. Release installation checks use print-mode sessions, not a
particular interactive first-run screen sequence on Linux, macOS or WSL2.

## 4. Change one agent

Inside the session, type these yourself, using the agent and model you want:

```text
/cm show
/cm set <agent>=<model>[:<effort>]
```

`/cm show` lists the lineup and the profiles you can switch to. Read the
change command's output before taking the next step:

- If it says `next: type /reload-plugins`, type `/reload-plugins`, then
  start fresh agents. Running agents keep their binding; do not
  `SendMessage`-continue an old agent expecting it to use the change.
- If it says `relaunch needed`, or records a `pending` change with
  `nothing in the running session changed`, exit the session, then run
  `charmer -r <id>`. Reload alone does not apply that pending change.
- If it says no change, there is nothing to apply.

Only you can run `/cm`, not the model. The lineup describes charmer's
bindings; user-installed spawn mods can redirect fresh agents
([compatibility limits](reference/compatibility.md)).
[More ways to change a lineup](guides/lineup.md).

## 5. Exit and resume

Exit Claude Code as usual. Later:

```bash
charmer -c          # continue the last managed session in this directory
charmer -r          # open the sessions picker
charmer -r <id>     # resume a particular session
```

The last form accepts a full id, a unique id prefix of at least 8 characters,
or a session name; an ambiguous match is refused, never guessed.
The session keeps its lineup across exits, updates and restarts. A different
`--profile` cannot override a resumed session's lineup: record the change
with `/cm profile <name>` first, or use **S** → **T** in the launcher.
[More about sessions](guides/sessions.md).

## One provider, start to finish

These terminal alternatives each work alone after installation and the
Claude Code setup step. Answer each preview or confirmation yourself.

**A keyed provider alone** (DeepSeek here; Kimi, Meta and Qwen work the
same way):

```bash
charmer providers set-key deepseek     # the key, hidden
charmer setup --step profile           # confirm the starter: save it and make it the default
charmer                               # Enter on the card launches it
```

**An account sign-in alone** (your Claude account):

```bash
charmer providers sign-in anthropic    # type personal once, then sign in in the browser
charmer --profile claude
```

**OpenRouter alone**, from the answers file in
[A prepared setup](#a-prepared-setup) below:

```bash
charmer setup --answers <file>
charmer
```

**A custom endpoint alone** (the Z.ai preset; your own endpoint works the
same way after `charmer providers add`):

```bash
charmer providers add --preset zai     # approve where the key goes
charmer providers set-key zai
charmer models add zai <wire> --context <n> --source docs --source-ref "https://docs.z.ai/ 2026-10" --as custom-example
charmer profile starter --apply        # confirm saving; this does not set the default
charmer --profile starter
```

## A prepared setup

`charmer setup --answers <file>` applies a prepared setup after one preview
and confirmation. API keys are named by file only (an absolute path to a
private, one-line key file). Sign-ins and connection tests are refused in
an answers file: they need you at the terminal. Entries are planned against
the state before the run; an entry depending on a provider declared by an
earlier entry refuses the whole file before anything is shown.
For example, OpenRouter alone with a starter profile:

```json
{
  "version": 1,
  "claude": {"source": "auto"},
  "gateway": {"start": true},
  "providers": [{"id": "openrouter", "api_key_file": "/home/user/keys/openrouter.key"}],
  "profile": "starter"
}
```

The same file with `{"id": "openai", "transport": "api-key", "api_key_file": "…"}`
as its provider entry sets up the OpenAI API key alone.

## When something is wrong

`charmer doctor --first-run` shows the first-run checks in order;
`charmer doctor` names one fix for each finding.
[troubleshooting.md](troubleshooting.md) maps symptoms to fixes and
[CHEATSHEET.md](CHEATSHEET.md) is the one-page lookup.
