# Move an installation from before the rename

charmer was released as claude-multi up to 1.1.0. From 2.0 on, charmer
installs beside an installation of claude-multi and never updates it in
place.
`charmer migrate from-legacy` moves that installation onto charmer once:
your sessions, profiles, keys, account sign-ins, Claude Code copies and the
supervised gateway keep working, under charmer's names. Until you remove the
old installation, `--back` returns everything to claude-multi.

`charmer migrate` converts pre-public record formats v1–3 (written before
charmer 1.0.0) to the current format, keeping byte-exact backups for
`charmer restore-2x`. `charmer restore-2x` restores those pre-public backups;
it is not a release rollback — use `charmer update --rollback` for that.

## Before you start

- Install charmer with its installer ([Linux](../install/linux.md),
  [macOS](../install/macos.md), [Windows (WSL2)](../install/windows-wsl2.md)).
  When it finds claude-multi on the account it does not start setup; it
  points here instead. Do not run `charmer setup` first: a setup gives
  charmer folders of its own, and the move never merges two.
- Close every managed session, the lead's included, and work from a plain
  terminal, not inside a Claude Code session.
- Only an installation made by claude-multi's installer moves. One that Nix
  manages does not: remove it through your Nix setup, then
  [start fresh instead](#start-fresh-instead).
- A copy of `~/.config/claude-multi`, `~/.local/state/claude-multi` and
  `~/.local/share/claude-multi` (stopped, not running) is a good idea.

## 1. Preview

```bash
charmer migrate from-legacy --dry-run
```

The preview checks every condition and lists, by name, each folder it would
move, each value it would rewrite, the gateway units it would stop and start,
the files it would remove, what stays for `--back`, and what is left for you
to check. It writes nothing. It ends with `Ready` (exit 0) or names what to
fix first (exit 1).

## 2. Move

```bash
charmer migrate from-legacy
```

| Step | What happens |
| --- | --- |
| units | the claude-multi gateway's unsaved-credential hold is checked; its two unit files are saved for `--back`, then the gateway is stopped and disabled |
| roots | `~/.config/claude-multi`, `~/.local/state/claude-multi` and each folder of `~/.local/share/claude-multi` but `install/` become charmer's, by rename on one file system (nothing is copied); keys, sign-in records and rotation files move unopened |
| authorities | one value each is rewritten in place: the gateway's state folder in `continuity.json`, a key file's path in `secret-file.json` when that file moved, the service name in `endpoint.json` |
| shims | the hook and token helpers of claude-multi give way to charmer's |
| gateway | a gateway that ran as a service before the move runs as `charmer-gateway.service`: it is installed, enabled and started, and its start renders the gateway configuration again; a gateway that started on demand keeps starting on demand, and no service is installed |
| remnants | the claude-multi launchers in `~/.local/bin` and its unit files are removed |
| verify | `charmer gateway ensure` and a doctor pass without a BLOCK |

A step that stopped is finished by running the command again;
`charmer migrate from-legacy --status` shows where it is. Until the move
finishes (or `--back` does), every other charmer command refuses and says so;
only `charmer gateway status`, `logs` and `clear-hold` run, from when the
folders, values and helpers are in place until `--back` changes any of them,
so a persistence hold can be resolved.

## After the move

- A session resumes as before: `charmer -r <id>`. The first resume
  compiles the session's files again; until then doctor lists the session
  under Attention (its hooks still name claude-multi's helpers), never as
  BLOCKED.
- Catalog 39 renamed 31 model names from `claude-multi-<model>-<effort>` to
  `charmer-<model>-<effort>`. A session or setting that saved one of the
  old names is read under the new one when it launches or resumes, on the
  same provider, model, effort and context window, and the gateway serves
  those 31 under their `charmer-` names only. Names of retired models that
  the gateway keeps serving for recorded sessions (continuity aliases) keep
  the name they were saved under: a `claude-multi-qwen38-max` or
  `claude-multi-glm52-max` alias stays in both model lists as it was.
- Status lines and scripts that read `CLAUDE_MULTI_*` variables must read
  `CHARMER_*` (same suffix).
- An operator export claude-multi wrote can still be read: `charmer import <file>`.

## Left for you to check

The move changes none of these; the preview and doctor name them:

- a credential name that starts with `CHARMER_`, which charmer reserves:
  give it another name. A provider whose secret uses such a name is
  ignored for routing, and it blocks every change that writes the gateway
  configuration (selecting a key file again, for example) until you rename
  its secret or remove it (`charmer providers rm <id>`);
- a `.env` file in the gateway's configuration folder: it moved as it is;
- developer drafts and reviews that name claude-multi's paths: check and
  review them again with `charmer-dev`;
- a session whose project folder was inside the moved folders;
- files an interrupted write left behind (`.<name>.<random>.tmp`).

## Going back

```bash
charmer migrate from-legacy --back
```

`--back` works from any step, a finished move included. It first checks
that charmer's gateway holds no unsaved credential (as the move checks
claude-multi's), then stops charmer's gateway, the service or the one
started on demand, and goes on only once it has stopped. It puts the folders
and the rewritten values back, restores claude-multi's unit files exactly
as they were, its launchers and its helpers. Its gateway service is enabled
again if it was enabled before the move, and started again if it was
enabled or running. charmer stays installed.

Before it moves anything back, `--back` checks every folder the move
renamed. A folder that is under claude-multi's name again and not empty,
while charmer's name still holds what the move put there, stops it: two
folders are never merged. So does a state or configuration folder missing
under both names. An empty folder something created again under
claude-multi's name is removed, and a folder of `~/.local/share/claude-multi`
deleted after the move is named and skipped. When it stops, nothing has
been moved back and `--back` can run again once the folder is fixed.

Sessions you resumed or started with charmer go back too. Their records,
and the launch settings charmer compiled for them (model list, model
picker, session variables), name the `claude-multi-` models and variables
again, exactly as claude-multi writes them. `--back` then ends by running
the kept release's own `claude-multi doctor --repair-all --include-live`
once, which compiles the rest of their files again in claude-multi's
format. Afterwards `claude-multi doctor` shows no BLOCK for them, and
`claude-multi -r <id>` resumes each, a session first started with charmer
included. If a session still carries a charmer name after that repair,
`--back` says which and exits 1; the folders are back either way, and
`claude-multi doctor --repair <id>` (or one resume with claude-multi)
finishes it.

## Removing the old installation

```bash
charmer migrate from-legacy --purge
```

Once you are content, `--purge` removes the kept claude-multi release in
`~/.local/share/claude-multi/install` (which `--back` needs) and
claude-multi's build caches, each whole. The folders the move emptied
(`~/.local/share/claude-multi` itself, and claude-multi's state and
configuration folders where they still exist) go only while they are
empty folders: a file written there after the move, or a file or link
standing where such a folder was, stays, and `--purge` names it.
It lists what it removes and asks first (default No); afterwards `--back`
is no longer possible.

It refuses, removing nothing, when any folder on the way to what it
would remove, from `/` down (your home folder included), is a link; when
a key file charmer can read (the one selected, the default when none is,
or the one its gateway service reads) is reached through a link or a
`..`, or lies inside what it would remove (select it by a plain path outside
`~/.local/share/claude-multi` first: `charmer setup --keys-file <path>`); or
when a path leads to a folder it never touches. It checks and removes
through the same open folders, so nothing swapped in between is followed.
It removes nothing else: every other folder, Claude Code's own `~/.claude`
and charmer's own folders included, stays as it is.

## Start fresh instead

An installation that cannot move (Nix manages it, its release bundle is
gone, or it never ran a session) still leaves its folders, and charmer's
installer and `charmer setup` point here as long as they exist. To set
charmer up beside them instead:

1. Retire the earlier installation and stop its gateway. While its
   launcher works: `claude-multi gateway service uninstall` (it hands the
   gateway back to the on-demand start and may start it again), then
   `claude-multi gateway stop`, until `claude-multi gateway status` says it
   is not running. A Nix installation: remove it through your Nix setup.
   Either way, and also when its launcher is gone, check before you rename
   anything that none of its gateways still runs: its gateway service is
   stopped or gone, and no `cli-proxy-api` process of its installation is
   left in your process list (end any that is).
2. Rename its folders aside and keep them; they hold its profiles, keys
   and sign-ins. They are its configuration and state folders,
   `~/.config/claude-multi` and `~/.local/state/claude-multi` (the same
   names under `XDG_CONFIG_HOME` and `XDG_STATE_HOME` when you set them).
   When `XDG_CONFIG_HOME` is set, the gateway's `~/.config/claude-multi`
   in your home folder is a third. For example:
   `mv ~/.local/state/claude-multi ~/.local/state/claude-multi.retired`.
   The preview, the refusal and `charmer setup` name each one that exists.
3. Run `charmer setup`.

Nothing of the earlier installation is read or removed; a folder renamed
aside is yours to keep or delete later.

## When something refuses

| The move says | Fix |
| --- | --- |
| a managed session runs | close it (and its lead), then run the move again |
| charmer already keeps its own state or configuration | move charmer's own folders aside; the move never merges two |
| a move would cross file systems | move that folder onto the same file system yourself |
| the gateway may hold an unsaved credential | finish it with claude-multi first: `claude-multi gateway clear-hold` |
| an install or update of claude-multi did not finish | let the kept 1.1.0 launcher recover it first: `claude-multi update` |
| its gateway service is installed under another name | remove it with claude-multi, move, then install charmer's under that name |
| it is a Nix installation, or its release bundle or state folder is gone | [start fresh instead](#start-fresh-instead) |
| `--back`: charmer's gateway may hold an unsaved credential | `charmer gateway status` names it; once the credential is saved again, `charmer gateway clear-hold`, then run `--back` again |
| `--back`: charmer's gateway was not stopped | `charmer gateway status` says why; fix that, then run `--back` again (it stops the gateway itself) |
| `--back`: a folder exists again under claude-multi's name | move the extra folder aside, then run `--back` again |

See also [Update](../update.md) and [Troubleshooting](../troubleshooting.md).
