# The local gateway

Claude Code speaks Anthropic's Messages protocol. To reach other
providers, every managed session sends its requests to a local gateway: a
patched build of CLIProxyAPI that charmer configures, starts and
checks. It listens on loopback (127.0.0.1) only. Plain `claude` never uses
it.

## On demand by default

The gateway starts when a launch or a resume needs it and keeps running.
Each new installation records a free port (in the range 18317–18336) in
`~/.config/charmer/endpoint.json` on its first start.

```bash
charmer gateway status     # backend, endpoint, instance, log, recent starts, persistence hold (sends no token)
charmer gateway start      # start it and wait until it is ready (up to 45 s)
charmer gateway restart    # stop, then start
charmer gateway stop
charmer gateway logs       # the newest log lines (-n N; --instance <id>)
charmer gateway ensure --quiet   # for scripts: exit 0 only when your gateway is ready
```

In the launcher, **H** (doctor) offers start, restart (**r**) and the log
(**l**); the Providers and Direct screens offer a start when the gateway
is down. Stopping stays a terminal command.

Exit statuses: 0 ok; 1 refused, paused, failed, not ready, busy, held or
inhibited (the message says which); 2 usage; 3 a declined confirmation;
130 cancelled.

### Only a gateway proven to be yours

`stop` and `restart` act only on a gateway proven to be yours: its lock,
its start record, the process and the listening socket must agree. A port
held by anything else is refused, and no token is sent to it.

### A bounded graceful stop

On an ordinary stop or restart, the gateway cancels queued credential refreshes
and waits for already-running refresh workers, including their credential saves
and save reports. Its 30-second shutdown budget starts when shutdown begins,
not when the gateway starts. HTTP requests still use graceful draining.

This reduces the chance of losing a newly rotated sign-in token; it is not an
unconditional token-survival guarantee. An active provider refresh can itself
use 30 seconds, leaving no time for persistence or other cleanup. A deadline
exit is reported as an incomplete refresh shutdown, not a confirmed save.
SIGKILL, a crash, power loss, a lost provider response, filesystem failure or a
stop-budget overrun can still lose a replacement token and require sign-in
again. The service manager's own stop limit also applies. Keep the persistence
hold checks; a clear hold is an observation, not a fence against a new refresh.

### After a crash

Sessions recover from a gateway that exited: each session's token helper
starts a stopped gateway (waiting at most 10 seconds) and gives the
session its token only when the gateway is proven yours and ready. Three
starts within ten minutes pause automatic starts until you start it
yourself (`charmer gateway start`), so a crashing gateway does not
loop; `charmer gateway logs` shows why it stopped.

## The supervised service (Linux)

On Linux with a user service manager, the gateway can run as a hardened
user service instead, restarted on failure and confined:

```bash
charmer gateway service install     # install or refresh the unit and hand the gateway to it
charmer gateway service status
charmer gateway service uninstall   # back to the on-demand start
```

The unit is `charmer-gateway` (`~/.config/systemd/user/charmer-gateway.service`,
written and marked by charmer; `--name` chooses another name). The
install stops an on-demand gateway only after the persistence hold
allows it, starts the unit, proves the gateway yours and records the
backend in `endpoint.json`; a failed step brings the on-demand gateway
back. Run it again after an update: it refreshes the unit and says whether
a restart is needed. It needs a release bundle or the Nix package, not a
source checkout. There is no service on macOS.

The hardened systemd user unit needs unprivileged user namespaces.
Ubuntu 23.10 and later restrict them by default through AppArmor. With
that restriction, the unit cannot start: `charmer gateway service install`
reports that the gateway exited during its start and keeps the on-demand
gateway. The on-demand gateway does not need unprivileged user namespaces.

An administrator can lift the restriction with
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, or persist
`kernel.apparmor_restrict_unprivileged_userns=0` in a file under
`/etc/sysctl.d/`. This is a system-wide security trade-off, not a
requirement for using charmer: keeping the on-demand gateway avoids
that change.

| | On demand | Supervised service |
| --- | --- | --- |
| started by | a launch, a resume, a session's token helper, `gateway start` | the user service manager |
| restarted on failure | no (the next launch starts it) | yes |
| confinement | runs as you | a private temporary home, read-only configuration; only its credential, trace and working directories are writable |
| logs | one file per start, `charmer gateway logs` | the service journal, `charmer gateway logs` |

## Reading the logs

`charmer gateway logs` prints the gateway's own log lines, redacted:
no key, token, cookie or account identifier the gateway logged is shown
(`<redacted>` stands in), including a private key that spans several
lines and a value split by terminal control characters. It reads up to
200 lines above the ones it shows, so a value that begins earlier is
still recognized; a failed start's log tail is redacted the same way. The
log files themselves (one per start, or the service journal) are not
redacted: they can name account files and credential indexes, so read
them locally and never paste them into a public issue.
`charmer doctor` summarizes what matters from them (counts per model
and error code, refresh failures) without account details, and
`charmer doctor --json` is the report to attach. See
[privacy.md](../privacy.md#diagnostics-you-share).

## The persistence hold

When a signed-in account's refreshed credentials may not have been saved
(a failed or unconfirmed save, or a log not fully read), charmer
holds the gateway: stop, restart, an update or an uninstall that would
replace it are refused, because restarting could lose the only good copy.
Keep it running, free disk space if that was the cause, and wait for a
confirmed save; sign in again only if none comes. Once you have checked
the credentials (`charmer providers list`),
`charmer gateway clear-hold` acknowledges the evidence you checked (a terminal
outside Claude Code, typed confirmation). It cannot waive an unavailable
journald process observation: restore that observation first, as described
below. A generic restart is never the fix for a held gateway.

The hold reads only what it needs, over the gateway's whole run: on the
supervised service, the credential-save lines and the running gateway's
start line in the service journal, from that start; on demand, the
credential-save lines of every instance log from the position last
cleared. Only lines the gateway's credential-save reporter wrote count; a
request path that merely contains the same words does not. How long the
gateway has run and how much it logged do not matter. It still holds when
evidence is missing, and the reason says which: the journal no longer
reaches back to the gateway's start or to the last clear-hold (journald
keeps less than the gateway's run; `SystemMaxUse=` and `MaxRetentionSec=`
in journald.conf set how much it keeps), journald's rate
limit for your user services, which the gateway shares, dropped log lines,
a read did not finish within 30 seconds, or a credential-save line does
not parse. charmer sees journald's rate-limit notices only when your account
may read the system journal (for example in the `systemd-journal`, `adm` or
`wheel` group); otherwise `charmer gateway status` and `charmer doctor` say
that they were not checked, and that alone does not hold.

After the journal read, a known running supervised gateway also needs one
read-only observation from the **system** service manager of the default
journal namespace's `systemd-journald.service`: loaded, active, a positive
main PID and a valid process start time. A journald start strictly after
the later of the gateway's start and the last explicit clear holds with
“journald restarted; credential-save evidence may be missing”, even if the
gateway's start line survived and older journal entries are still readable.
Equality is not later. This is restart risk, not a report that credentials
were lost. A routine journald restart can hold even when no evidence was lost.
On-demand logging and an absent running instance do not make this query.

What to do depends on the reason:

- **Known journald restart or other incomplete log coverage, with no failed
  save seen:** check the credentials (`charmer providers list`), then run
  `charmer gateway clear-hold` once. A clear later than a known restart
  acknowledges it; another restart after the clear holds again.
- **Journald's process start could not be observed:** keep the gateway running
  and restore system-manager observation access and journald availability,
  then retry the hold check. A missing tool or system bus, denied access,
  timeout, inactive service or invalid properties all hold. Clearing refuses
  without writing; repeated clears cannot waive an unavailable observation.
  This differs from unreadable system-journal drop notices, which remain
  advisory as described above.
- **A credential save failed:** keep the gateway running and wait for a
  persisted save of that credential (sign in again only if none comes),
  and clear only after checking it. A restart-risk or observation problem
  never turns a failed save into a coverage-only hold.

The process observation requests the system manager's properties with
`--timestamp=us+utc`, `LC_ALL=C` and `TZ=UTC`: the documented
`Day YYYY-MM-DD HH:MM:SS.UUUUUU UTC` format, including six zero digits for
whole seconds. That style is available since systemd 248; the timestamp
property's formatting path was checked in systemd 252 and 262.1 sources.
An unsupported format is unknown, never guessed from a PID or a startup
message. This check covers charmer's default journal namespace only.
It does not solve silent loss without restart, other collectors, realtime
clock discontinuities or a restart after the final observation. A successful
observation is not an atomic safety or journal-completeness guarantee.
The persistence-hold document format is unchanged.

`charmer gateway status` shows where a hold-free evaluation started and how
many credential saves it read. In the launcher, **H** and the gateway dialog
(**W** on Providers and Direct) show the hold and offer no restart while it
holds; a restart there first lists the managed sessions running now and asks.

## When gateway changes are paused

An installer, an update, a service install or uninstall, or a move to
another computer records an **inhibition** while it changes the gateway or
its installation. Until it ends, every other command that would start,
stop, restart, reconfigure or install the gateway refuses with one
message naming the owner and the fix: “the gateway is inhibited by …”. A
gateway already proven yours keeps serving running sessions. If the
owner was interrupted, its own command finishes it (for example
`charmer gateway service install` again for an interrupted hand-off,
or `sh install.sh --repair` for an interrupted install).

## Applying changes

Provider, key and model changes re-render the gateway's configuration and
verify that the running gateway reloaded it (a render sentinel): no
restart is needed. When a reload cannot be verified, the command says
`gateway did not reload — restart required`; restart between turns with
`charmer gateway restart`. `charmer providers apply` re-renders
on request; `charmer plan` previews what the gateway would serve
after pending changes and writes nothing.

## What the gateway changes in a request

- It adds the provider's key, or the signed-in account's credentials;
  sessions never hold either.
- It sends a key only to its provider's address: the one the release
  ships, or the one you approved. A redirect that would carry the
  credential elsewhere is not followed: that request fails with a local
  HTTP 502, `upstream redirect refused`.
- On the OpenAI API key it sends the key as a plain API client (no ChatGPT
  client version or session header), adds no hosted tool the client did
  not ask for, and replaces any error the provider returns with fixed
  local text, so an error that quotes the key never reaches a session.
- On both OpenAI routes (the API key and the ChatGPT account), Claude
  Code's per-request output cap is not passed on: the model's own output
  limit applies ([providers/api-keys.md](../providers/api-keys.md#openai)).

## Thinking from content chunks

For OpenAI-compatible upstreams such as Mistral, responses can carry arrays of
`text` and `thinking` chunks rather than one text string. The gateway decodes
these into text and unsigned Claude thinking in their original order, for
streaming and non-streaming responses. Repeated `closed: true` chunks do not
prematurely close a thinking block, and the gateway does not invent signatures.
String responses and the existing reasoning fields keep their previous behavior;
Claude and the Z.ai preset use the separate Anthropic-compatible route.

This fixes display of Mistral thinking, not faithful replay of Mistral reasoning
history. The request translator drops unsigned thinking on replay; Mistral's
signed history format is not supported by this fix. Unsupported or malformed
chunks retain each translator path's existing fallback, including omission of
unknown chunk types by the registered non-streaming converter.

## The gateway token

Sessions get the gateway's local key only from their token helper, never
from their environment or files. `charmer doctor --rotate-token` (or
Settings → gateway token) replaces it without dropping sessions: the
gateway accepts the old and the new key for about five minutes, then only
the new one. It asks first, verifies every reload, and resumes if
interrupted. The key never appears in output.

## Retired models stay served

When a release retires a model line, sessions started earlier still name
its selector. The gateway keeps serving each such selector on its original
model as a continuity alias (listed in `~/.config/charmer/continuity.json`)
until no live session uses it; `charmer doctor --prune-aliases`
removes the unused ones.

## Quota readings

`charmer quota` (and `/cm quota`, and the Providers screen's quota
column for signed-in accounts) reports what the gateway observed in recent
responses: one local read, no provider call, nothing stored. No reading
means no data yet, not zero use; a reset time is shown only when the
provider sent one. The command exits 1 when no reading was made. Quota
readings need the gateway's read-only management access, which only the
Nix package enables in this release; a release bundle or a source
checkout reports the reading as unavailable in this build, and there is
nothing to fix.

This depends on the installation channel, not on whether the gateway is
on demand or supervised. Stale or exhausted readings still exit 0;
`/cm quota` reports the same condition with status 0.

A quota reading is an observation of recent traffic, not a balance, and
the request counts charmer shows are not billed tokens.

## Outbound proxy and certificates

The gateway uses the outbound proxy you set in setup, never proxy
variables it inherits: `charmer setup --step gateway --proxy <url>`
(no credentials in the address), `--no-proxy` to remove it. Certificate
settings: [networking.md](networking.md).
