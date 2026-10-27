# Sign in with a Claude account

You can run the Claude models (Opus, Sonnet, Fable, Haiku) on your own Claude
account (Pro or Max) instead of an Anthropic API key. This route is for
your own, personal use, and you confirm that before the first sign-in.

## 1. What you need

A Claude Pro or Max account of your own. The sign-in runs in your terminal
and opens claude.ai in a browser.

## 2. The personal-use confirmation

Before the first sign-in charmer shows its personal-use text once and
asks you to type `personal`. The text says, in short: charmer is an
independent tool, not affiliated with Anthropic; Anthropic's terms
restrict third parties from offering Claude.ai login or routing requests
through Free, Pro or Max credentials on behalf of users; signing in stores
your own account's tokens on this computer for your own sessions only; and
an Anthropic API key is the supported alternative. Your confirmation is
recorded in `~/.config/charmer/choices.json`. This is the product's
own condition for the route, not legal advice; read Anthropic's terms
yourself.

## 3. Sign in

In the launcher: **G** Providers → Anthropic → **L**, or Get started's
providers step. From a terminal outside Claude Code:

```bash
charmer providers sign-in anthropic
charmer providers sign-in anthropic --no-browser   # print the address instead
```

A browser page opens at claude.ai; approve the sign-in there and it
finishes in the terminal. Over SSH or without a display, open the printed
address on any device and paste back the address the browser ends on.
Ctrl-C cancels and changes nothing.

This sign-in is separate from your Claude Code login: plain `claude` and
its login stay as they are.

## 4. Accounts, sign-out and backups

- **G** → **L** lists the saved accounts. With more than one account
  signed in, requests may use any of them.
- Sign out with **G** → **L**, or:

  ```bash
  charmer providers sign-out anthropic
  ```

  The sign-in records move into a kept backup,
  `~/.local/share/charmer/auth.signed-out.claude.<date>`, which is
  never deleted for you; the gateway reloads and the account's models
  disappear.
- Sign-in records are plaintext token files in
  `~/.local/share/charmer/auth/` (mode 0600, private directory). Managed
  sessions cannot read them. Do not copy that directory between computers;
  sign in again on the new one.

## 5. Use it

With the account signed in, the shipped profiles that use Claude lines are
connected. `charmer providers test anthropic` sends one small request
after you agree.

Haiku 5.5 ships as `haiku`, for explorer and light-writer agents, not the
lead. It is the same family as Opus, Sonnet and Fable, not an independent
reviewer of them. Its Claude-account checks passed on 2026-10-27; the
[models guide](../guides/models.md#haiku-on-the-claude-account) names the
measured checks and explains when it handles the session's background
requests. Bind it with **E** on the launch card; no model declaration is
needed.

## 6. Limits and quota

Your plan's usage limits apply, and Anthropic does not publish them as
fixed numbers for this use; charmer cannot tell you what your
account is entitled to. Quota readings are available in release bundles and
the Nix package. `charmer quota` (or `/cm quota` in a session) shows what
the gateway observed from recent traffic: the condition and reset time when
the provider reported them, with their age. No reading means no data yet,
not zero use. See [quota readings](../guides/gateway.md#quota-readings) for
availability after an update. Use `charmer doctor` for per-model 429/402/403
counts and `/cm fallback <provider>`
to move affected roles; those counts are not a remaining balance or a reset
time. A profile that runs several agents on one account spends its limits faster.

Anthropic counts your use of Claude, Claude Code and Claude Desktop
against one shared limit, and requests through this sign-in draw on your
plan's limits as well. Anthropic's own pages, checked on 2026-10-04:

- [How do usage and length limits work?](https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work):
  what uses your limits (conversation length, features, the model and
  its effort) and that they are shared;
- [Use Claude Code with your Pro or Max plan](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan):
  limits that reset, and the choices when you reach one;
- [What is the Max plan?](https://support.claude.com/en/articles/11049741-what-is-the-max-plan):
  Max's allowance relative to Pro's.

A paid Claude plan does not include API usage
([Anthropic's note](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-do-i-have-to-pay-separately-to-use-the-claude-api-and-console)):
an Anthropic API key is billed separately, per token
([api-keys.md](api-keys.md#6-costs-and-limits)).

## 7. When it fails

| You see | Next step |
| --- | --- |
| doctor: the claude OAuth pool has no credential record | sign in: `charmer providers sign-in anthropic` |
| doctor counts `invalid_grant` lines since the sign-in | the sign-in expired or was revoked: sign in again |
| requests fail with a usage-limit message | check `charmer quota` for an observed reset and `charmer doctor` for per-model 429/402/403 counts; move affected roles with `/cm fallback <provider>` |
| “Not signed in — type personal to continue (nothing changed).” | type the word `personal` (case and spaces around it do not matter) |

## 8. The API-key alternative

An Anthropic API key runs the same models with pay-per-token billing and
published terms; see [api-keys.md](api-keys.md#anthropic). Switching
between the two moves the same Claude lines; there is no fallback from one
to the other.
