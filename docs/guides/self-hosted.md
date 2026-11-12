# A server with a key on your private network

A model server you run yourself, on this computer, your local network or
your tailnet, often wants an API key but serves plain HTTP. charmer can
send that key over plain HTTP only when you declare the network the server
is on, and the gateway then checks every connection against it. This page
covers keyed OpenAI-compatible servers; a server that needs no key is
simpler ([a server on your network](../providers/lan.md)).

## The three networks

You declare one of three classes. Each is a promise you make about the
network, and charmer refuses an address outside it:

| Class | Addresses | What plain HTTP means there |
| --- | --- | --- |
| `loopback` | 127.0.0.0/8, ::1 | plain HTTP on this computer: any local program can listen on that port |
| `lan` | 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7 except the tailnet prefix and the never-usable addresses below | plain HTTP: anyone on this network can read the key and your prompts |
| `tailnet` | 100.64.0.0/10, fd7a:115c:a1e0::/48 | plain HTTP: the key is encrypted only while the connection runs through your tailnet; on a carrier-grade NAT network these addresses are not yours |

Some addresses are never usable, whatever you declare: unspecified,
link-local, multicast and broadcast addresses, `0.0.0.0/8`, IPv4 forms
written as IPv6 (write the IPv4 address instead), addresses with a zone
id, and cloud instance metadata addresses, the IPv6 ones in the
unique-local range included. The tailnet overlay's own service addresses
(its resolver at 100.100.100.100 and fd7a:115c:a1e0::53, and its other
reserved ranges, 4via6 included) are refused too. Every other address is
public and refused for a private network, NAT64, 6to4 and Teredo forms and
documentation ranges included. The gateway's own ports are refused for
every class.

## Declare it

From a terminal outside Claude Code (the key and the approval are
terminal-only):

```bash
charmer providers add box --kind openai-compatible --base-url http://box.example:8000/v1 --auth bearer --secret-ref env:BOX_API_KEY --family unknown --network tailnet
charmer providers set-key box --secret-file ~/box-key   # a private 0600 file with the key on one line
charmer models add box local-model --context 131072 --source operator --as custom-box-model
charmer profile edit <name>         # bind custom-box-model as a lead or agent
```

`charmer providers template --kind openai-compatible --network tailnet`
prints a declaration to start from. In the launcher, **G** Providers → **N** (or **W** Get started
→ **A**) offers “A server on your network — with an API key”; the
OpenAI-compatible forms ask for the network, and **E** on the provider
changes it later. A server that needs no key can carry a network too
(`--kind openai-compatible-lan --auth none --network <class>`).

The approval names the class, its sentence from the table above and what
the server's name resolves to from here now. An answer outside the class
refuses the declaration and writes nothing; a name that does not resolve
from here is not refused (the server may be on another network now).
Changing the network or the address leaves the route changed until you
approve it again (`charmer providers approve <provider>`).

## What the gateway enforces

The gateway receives the base URL and the class, never a plain base URL
for such a provider; a gateway without the private-network guard renders,
serves and sends nothing of it. For each connection the gateway:

- uses no proxy, from any source, and follows no redirect; it speaks
  HTTP/1.1;
- resolves the name once, connects only to addresses in the class, and
  checks the connected address again;
- on a tailnet, also requires this computer's own address on that
  connection to be a tailnet address;
- writes nothing on a connection whose provider declaration a reload has
  changed or removed: a request already connecting or waiting then fails
  instead of sending the key under the old declaration.

A refused connection answers HTTP 502
`upstream address refused: outside the provider's private network` with
the class. `charmer discover` follows the same rules. Doctor and readiness
checks connect with TCP only, never with the key, and say what the name
resolves to.

## What it cannot check

- A tailnet address is a routing fact. It proves neither that the traffic
  is encrypted nor who answers; your tailnet's access rules decide that.
- A tailnet client in userspace mode gives this computer no tailnet
  address, so the gateway refuses the connection: run the client in kernel
  mode.
- Behind a subnet router the server's address is a LAN address: declare
  `lan`. 4via6 addresses are refused.
- The name must resolve inside the gateway's sandbox, where its resolver
  configuration may differ from your shell's.

## Changes and restarts

Adding, editing or removing such a provider, its network or its lines
reaches the running gateway by hot reload; the reload check requires the
private-network selectors to be served. A gateway older than 2.5.0 reloads
without them (`gateway reloaded without the private-network selector(s)`):
after `charmer update`, run the guarded `charmer gateway restart` between
turns, as doctor says ([update](../update.md#updating-from-240)).

## Moving from a local forwarder

If you reach the server today through a forwarder on this computer (a
small service that adds the key and relays to the server), move in this
order, between turns:

1. Declare the server directly with `--network`, save its key with
   `charmer providers set-key <provider> --secret-file <file>` and read the
   approval.
2. Add the model under a temporary key:
   `charmer models add <provider> <wire> --context <n> --source operator --as <new-id>`.
3. Check without sending anything: `charmer providers show <provider>`,
   `charmer doctor` and
   `charmer direct --model <new-id> --no-subagents --print-launch`.
4. Send one real request: `charmer models qualify <new-id> --smoke`
   or a short direct session. If you decline it or it fails, stop and leave
   everything as it is.
5. Only after it succeeds: `charmer models rm <line> --successor <new-id>`,
   remove the forwarder's provider and line, and stop the forwarder last.

You need a kernel-mode tailnet client, the name resolving inside the
gateway's sandbox, and the key in a private 0600 file with one line.

## Known limits

- No per-provider concurrency limit: a server with one slot queues the
  others on its own side.
- Claude Code abandons a request that has received no byte after about six
  minutes and sends it again, whatever `API_TIMEOUT_MS`,
  `CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS` or `CLAUDE_STREAM_IDLE_TIMEOUT_MS`
  say (measured with Claude Code 2.1.322 through the gateway's Claude
  sign-in route, 8.0.67). An upstream that holds its first byte longer never
  answers: each attempt is cancelled, upstream too, and sent again, up to
  300 times in a managed session. Sending response headers early does not
  help. On that Claude route, SSE comment lines or ping events every 20
  seconds kept an 11-minute answer open; this is not a workaround for a
  keyed OpenAI-compatible server, whose stream the gateway reads without
  passing comments on, so plan for answers that start within six minutes.
- A 524,288-token line as the lead sets the session's window to 524,288
  tokens and compacts at about 453,859; as an agent under a larger window
  it runs in the 200K class.
- A line declares at least one effort;
  `charmer models add <provider> <wire> --context <n> --source operator`
  declares `high` when you name none. On this route a request without thinking sends
  no reasoning level, and a request with thinking disabled still sends the
  lowest declared level; otherwise the requested effort is mapped into the
  levels the line declares. Declare levels the server accepts and check
  the line with `charmer models qualify <line> --smoke`.
- An answers file for `charmer setup` cannot declare a network yet.
