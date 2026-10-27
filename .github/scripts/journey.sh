#!/bin/sh
# Fixture-only user journeys on release bundles (CI; never a provider call).
#
#   sh .github/scripts/journey.sh install DIST
#       a fresh install as a new user would make it (install.sh from DIST,
#       verified with an ephemeral key made here), then the installed journey
#   sh .github/scripts/journey.sh installed VERSION
#       the installed journey alone, on the installation already in $HOME
#       (tools/release.py verify-draft installs the signed draft with the
#       release trust first): the version line, the first-run checks of
#       the new installation (doctor --first-run: ready, or not ready only
#       for what a fresh install has not set up yet, providers included:
#       nothing connected yet), the installation channel (plain doctor:
#       "this launcher is the bundle channel"), the pinned Claude Code from
#       a verified local copy ($JOURNEY_CLIENT: a file, or "fetch" to
#       download the installed release's pin), the on-demand gateway
#       lifecycle (setup --step gateway, status, stop) with its loopback
#       confinement, a first managed turn against a fixture provider and
#       its resume (with a client), a foreign listener on the gateway port
#       refused, and an uninstall that keeps the state (charmer
#       uninstall --keep-setup --yes on a pseudo-terminal, never --force,
#       once every managed session recorded its end: it removes the
#       launchers and the installed release, and keeps the credentials,
#       which only a typed phrase deletes). JOURNEY_NATIVE=1 (the CI
#       journeys, on disposable runners: these steps read the host's
#       process table and Claude Code daemon root) adds the native checks:
#       the installed release's own process observation against disposable
#       processes (journey_fixture.py processes), and with a client the
#       first interactive managed turn on a pseudo-terminal with the
#       one-time screens recorded (before the print-mode turns), the session
#       verbs on a running client (a turn the fixture holds: mark-ended
#       refused and answered at once, stop finds nothing in the background;
#       once the client is stopped, mark-ended records its end), the
#       session's compiled credential rules, sessions forget, and the
#       user's own Claude Code launcher, version files and install and
#       update preferences left untouched. JOURNEY_WSL=1 (the Windows
#       journey, started in a directory on a Windows drive) adds the WSL
#       checks: doctor detects WSL and states that the Windows-side and
#       WSL-side Claude configurations are separate, a working directory on
#       a Windows drive gets the guidance and a Linux one does not, a state
#       root on a Windows drive refuses the gateway start, and the compiled
#       session carries the rules for the Windows side's Claude
#       configuration (elsewhere it carries none). JOURNEY_KEEP=DIR keeps
#       what the restarted journey needs there and leaves the gateway
#       running for the shutdown.
#   sh .github/scripts/journey.sh restarted VERSION KEEPDIR
#       after the installed journey (JOURNEY_UNINSTALL=skip, JOURNEY_KEEP)
#       and a shutdown of the system that ran it with the gateway running
#       (wsl --terminate): what gateway status says is recorded, the next
#       managed launch starts the gateway on demand and completes its turn,
#       the gateway stops, and the uninstall keeps the state
#   sh .github/scripts/journey.sh update KEYDIR DIST_A DIST_B
#       install A, check, update to B, "up to date", roll back to A, the
#       installation channel checked after each step; both releases trust
#       the ephemeral key in KEYDIR (key, allowed_signers), because the
#       release key never enters CI
#   sh .github/scripts/journey.sh service DIST
#       a fresh install, then the supervised gateway service under the
#       user's own systemd user manager (Linux; a disposable user): the
#       first port a new install takes is held by another listener, so the
#       gateway records another; gateway service install, install again
#       (the refresh), the unit's FragmentPath (systemctl --user show) is
#       the rendered file, the unit is active and its main process is the
#       installed release's, the gateway answers on its port, gateway
#       service status; then gateway service uninstall removes the unit
#   sh .github/scripts/journey.sh prepare DIST OUT
#       an ephemeral key (OUT/key) and the release signed with it, with its
#       install.sh (OUT/release): what the Windows journey's install.ps1
#       installs from (its test seam: install.sh over loopback https, the
#       release from this directory)
#
# JOURNEY_GATEWAY=skip and JOURNEY_UNINSTALL=skip leave those steps out
# (the repository's own tests run this script on fake releases that carry
# no gateway); JOURNEY_INTERACTIVE=skip leaves out the interactive turn.
# JOURNEY_TARGET=<target> requires a fresh install to install that bundle
# target (linux-aarch64 on the arm64 runner). Each step prints
# "journey: <step>: ok"; any failure stops the journey with a nonzero exit.
#
# Providers are fixtures only: the managed turn's provider is
# journey_fixture.py on 127.0.0.1 (keyless, declared and admitted like any
# provider; the admission's consent is answered on a pseudo-terminal), and
# no other provider has a credential. The confinement checks are negative
# controls with a positive twin each: the gateway answers on 127.0.0.1 but
# not on the host's other addresses, where a listener bound to every
# interface does answer (macOS must have such an address; elsewhere, as in
# a network namespace, the check says it does not apply); and with
# another program listening on the gateway port, starting the gateway fails
# without sending that listener a credential, and a managed launch fails.

set -eu
# Exercise ordinary permissive user environments; product modes must be explicit.
umask 0002

say() { printf 'journey: %s\n' "$*"; }
fail() { printf 'journey: FAILED: %s\n' "$*" >&2; exit 1; }
native() { [ "${JOURNEY_NATIVE:-}" = 1 ]; }

start_dir=$(pwd)
here=$(CDPATH='' cd "$(dirname "$0")" && pwd)
installer_default=$here/../../packaging/install.sh
fixture=$here/journey_fixture.py
work=$(mktemp -d "${TMPDIR:-/tmp}/charmer-journey.XXXXXX")
helpers=''
kill_helper() {
	# The interpreter owns a group, so cleanup also stops its descendants.
	# Kill the pid too: cleanup may run before the interpreter creates the group.
	kill -KILL "$1" 2>/dev/null || true
	kill -s KILL -- "-$1" 2>/dev/null || true
	wait "$1" 2>/dev/null || true
	if [ -n "${JOURNEY_HELPERS_DIR:-}" ]; then
		rm -f "$JOURNEY_HELPERS_DIR/$1"
	fi
}
cleanup() {
	for pid in $helpers; do kill_helper "$pid"; done
	rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cm=$HOME/.local/bin/charmer
install_dir=$HOME/.local/share/charmer/install/current

make_key() {
	# $1 = directory: key, key.pub, allowed_signers
	mkdir -p "$1"
	ssh-keygen -q -t ed25519 -N '' -C 'ephemeral journey key' -f "$1/key"
	printf 'release@charmer %s\n' "$(cut -d ' ' -f 1,2 "$1/key.pub")" >"$1/allowed_signers"
}

signed_copy() {
	# $1 = release directory, $2 = key directory, $3 = destination
	mkdir -p "$3"
	cp "$1"/*.tar.gz "$1/MANIFEST.json" "$1/SHA256SUMS" "$3/"
	ssh-keygen -Y sign -f "$2/key" -n charmer-release "$3/SHA256SUMS" >/dev/null 2>&1
	mv "$3/SHA256SUMS.sig" "$3/SHA256SUMS.sshsig"
}

install_from() {
	# $1 = signed release directory, $2 = key directory, $3 = installer
	sh "$3" --from-dir "$1" --allowed-signers "$2/allowed_signers" --no-setup --no-modify-path \
		>"$work/install.txt" 2>&1 || { cat "$work/install.txt" >&2; fail "install.sh from $1"; }
	cat "$work/install.txt"
	[ -x "$cm" ] || fail "no launcher at $cm"
}

manifest_version() {
	sed -n 's/^  "version": "\(.*\)",*$/\1/p' "$1/MANIFEST.json" | head -n 1
}

show_tail() {
	# $1 = file: its last lines on stderr (journey logs carry no secrets)
	[ -f "$1" ] && tail -n 20 "$1" >&2
	return 0
}

fx() {
	# journey_fixture.py under the installed release's own Python
	"$install_dir/runtime/python/bin/python3" -I "$fixture" "$@"
}

background() {
	# $1 = pid file, then fixture arguments. Start the interpreter, not an
	# fx subshell: $! stays the listener's pid across exec, in its own group.
	pidfile=$1
	shift
	"$install_dir/runtime/python/bin/python3" -I -c \
		'import os, sys
from pathlib import Path
registry = os.environ.get("JOURNEY_HELPERS_DIR")
if registry:
    (Path(registry) / str(os.getpid())).touch()
os.setpgrp()
os.execv(sys.executable, [sys.executable, "-I", *sys.argv[1:]])' \
		"$fixture" "$@" &
	echo "$!" >"$pidfile"
	helpers="$helpers $!"
}

stop_helper() {
	# $1 = pid file; wait before a later step reuses the port or uninstalls.
	pid=$(cat "$1")
	kill_helper "$pid"
	remaining=''
	for helper in $helpers; do
		[ "$helper" = "$pid" ] || remaining="$remaining $helper"
	done
	helpers=$remaining
}

wait_for() {
	# $1 = what, then a command that succeeds once it is ready (20 s at most)
	what=$1
	shift
	wait_up_to 20 "$what" "$@"
}

wait_up_to() {
	# $1 = seconds at most, $2 = what, then a command that succeeds once it is ready
	limit=$(($1 * 2))
	what=$2
	shift 2
	tries=0
	until "$@"; do
		tries=$((tries + 1))
		[ "$tries" -le "$limit" ] || fail "$what did not become ready"
		sleep 0.5
	done
}

gateway_port() {
	"$cm" gateway status 2>/dev/null |
		sed -n 's|^  endpoint: http://127\.0\.0\.1:\([0-9][0-9]*\).*|\1|p' | head -n 1
}

reply_number() {
	# $1 = a turn's output: the number of the fixture reply it printed
	sed -n 's/.*fixture reply \([0-9][0-9]*\).*/\1/p' "$1" | head -n 1
}

fetch_client() {
	# the installed release's pinned Claude Code; charmer checks its
	# size and sha256 when it copies it
	pin=$(fx client --install "$install_dir") || fail "the installed release names no Claude Code for this host"
	url=${pin%% *}
	curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$work/claude" "$url" ||
		fail "download of the pinned Claude Code"
	chmod 755 "$work/claude"
	JOURNEY_CLIENT=$work/claude
}

confinement() {
	# $1 = the running gateway's port
	fx reach --host 127.0.0.1 --port "$1" || fail "the gateway does not answer on 127.0.0.1:$1"
	addresses=$(fx addresses)
	if [ -z "$addresses" ]; then
		[ "$(uname -s)" != Darwin ] || fail "no non-loopback address to check the gateway's confinement from"
		say "loopback confinement: no non-loopback address here (does not apply)"
		return 0
	fi
	background "$work/open.pid" listen --host 0.0.0.0 --port 0 --port-file "$work/open.port" \
		--log "$work/open.log"
	wait_for "the control listener" test -s "$work/open.port"
	open=$(cat "$work/open.port")
	for address in $addresses; do
		fx reach --host "$address" --port "$open" ||
			fail "control: a listener on every interface does not answer on $address:$open"
		if fx reach --host "$address" --port "$1"; then
			fail "the gateway answers on $address:$1 (it must listen on loopback only)"
		fi
	done
	stop_helper "$work/open.pid"
	say "loopback confinement: ok (refused on $(printf '%s' "$addresses" | tr '\n' ' '); the control listener answered)"
}

first_interactive_turn() {
	# The first interactive managed turn, on a pseudo-terminal in a new
	# folder: the one-time screens Claude Code shows are answered and
	# recorded (journey_fixture.py interactive), with whether this account
	# had a Claude Code configuration before it.
	if [ "${JOURNEY_INTERACTIVE:-run}" = skip ]; then
		say "first interactive managed turn: not run (JOURNEY_INTERACTIVE=skip)"
		return 0
	fi
	mkdir "$work/first"
	fx interactive --log "$work/screens.log" --cwd "$work/first" -- "$cm" --line direct \
		--model custom-journey-fixture >"$work/screens.txt" 2>&1 ||
		{ cat "$work/screens.txt" >&2; fail "the first interactive managed turn"; }
	sed 's/^/journey:   /' "$work/screens.txt"
	say "first interactive managed turn: ok (Claude Code configuration before it: $claude_config_before)"
}

user_claude_installation() {
	# The user's own Claude Code launcher and version files (a disposable
	# stand-in where the account has none; an existing one is only read),
	# recorded before charmer copies its pinned client.
	fx user-claude plant || fail "the user's Claude Code installation could not be prepared"
	fx user-claude record --what installation --file "$work/user-claude-installation.json" ||
		fail "the user's Claude Code installation could not be recorded"
}

user_claude_preferences() {
	# Its install and update preferences in ~/.claude.json, recorded before
	# the print-mode turns: stand-in values only when the account had no
	# Claude Code configuration before this journey's first managed launch
	# (an existing one is only read).
	if [ "$claude_config_before" = empty ]; then
		fx user-claude plant --preferences || fail "the user's Claude Code preferences could not be prepared"
	fi
	fx user-claude record --what preferences --file "$work/user-claude-preferences.json" ||
		fail "the user's Claude Code preferences could not be recorded"
}

user_claude_untouched() {
	fx user-claude check --file "$work/user-claude-installation.json" ||
		fail "the managed client changed the user's Claude Code launcher or version files"
	fx user-claude check --file "$work/user-claude-preferences.json" ||
		fail "the managed client changed the user's Claude Code install or update preferences"
	say "the user's own Claude Code: untouched (launcher, version files, install and update preferences)"
}

open_session() {
	# sets held_session: the open managed session records, one per line
	held_session=$(fx session-ids --install "$install_dir" --open) && [ -n "$held_session" ]
}

session_verbs() {
	# A resumed turn the fixture provider holds keeps its client running
	# while the session verbs read this host's process table and daemon
	# root. Each verb runs bounded: a session lock that outlived the
	# launch's exec would block it. Every earlier session has recorded its
	# end first, so the held one is the only open record.
	fx ended --install "$install_dir" --timeout 60 >/dev/null || fail "an earlier managed session has not ended"
	background "$work/held.pid" exec --cwd "$work/project" -- "$cm" -c -- -p "journey turn held" \
		>"$work/held.txt" 2>&1 </dev/null
	wait_up_to 120 "the held managed turn" fx held --log "$work/fixture.log"
	wait_up_to 30 "the held session's open record" open_session
	[ "$(printf '%s\n' "$held_session" | wc -l)" -eq 1 ] ||
		fail "expected one running managed session, found: $(printf '%s ' "$held_session")"
	fx processes --install "$install_dir" --session "$held_session" ||
		fail "the process table does not show the running client"
	if fx run --timeout 60 -- "$cm" sessions mark-ended "$held_session" >"$work/mark-live.txt" 2>&1; then
		fail "sessions mark-ended recorded the end of a running session"
	fi
	grep -q 'is live' "$work/mark-live.txt" ||
		{ show_tail "$work/mark-live.txt"; fail "sessions mark-ended did not refuse the running session"; }
	if fx run --timeout 60 -- "$cm" sessions stop "$held_session" >"$work/stop.txt" 2>&1; then
		fail "sessions stop acted on a session that is not in the background"
	fi
	grep -q 'not live in the background' "$work/stop.txt" ||
		{ show_tail "$work/stop.txt"; fail "sessions stop did not read the daemon root"; }
	say "session verbs on a running client: ok (mark-ended refused it, stop found nothing in the background)"
	wsl_rules=absent
	[ "${JOURNEY_WSL:-}" != 1 ] || wsl_rules=present
	rules=$(fx scope-rules --install "$install_dir" --session "$held_session" --wsl "$wsl_rules") ||
		fail "the session's compiled credential rules"
	say "compiled credential rules: ok ($rules)"
	# Stopped without its end hook (SIGKILL to its process group): the record
	# stays open until mark-ended, which now finds no process.
	stop_helper "$work/held.pid"
	: >"$work/release-held"
	[ "$(fx session-ids --install "$install_dir" --open)" = "$held_session" ] ||
		fail "the stopped client's session does not stay open"
	fx run --timeout 60 -- "$cm" sessions mark-ended "$held_session" >"$work/mark.txt" 2>&1 ||
		{ show_tail "$work/mark.txt"; fail "sessions mark-ended after the client stopped"; }
	grep -q 'marked ended' "$work/mark.txt" || { show_tail "$work/mark.txt"; fail "the session was not marked ended"; }
	fx run --timeout 60 -- "$cm" sessions mark-ended "$held_session" >"$work/mark-again.txt" 2>&1 ||
		{ show_tail "$work/mark-again.txt"; fail "sessions mark-ended again"; }
	grep -q 'already ended' "$work/mark-again.txt" ||
		{ show_tail "$work/mark-again.txt"; fail "a second mark-ended did not say already ended"; }
	say "session verbs after the client stopped: ok (marked ended, then already ended)"
}

forget_session() {
	# $1 = a managed session whose end is recorded
	fx run --timeout 60 -- "$cm" sessions forget "$1" --yes >"$work/forget.txt" 2>&1 ||
		{ show_tail "$work/forget.txt"; fail "sessions forget"; }
	grep -q "Forgot: $1" "$work/forget.txt" || { show_tail "$work/forget.txt"; fail "sessions forget did not forget $1"; }
	if fx session-ids --install "$install_dir" | grep -qx "$1"; then
		fail "the forgotten session's record remains"
	fi
	say "sessions forget: ok (its record and generated scope deleted)"
}

managed_turn() {
	background "$work/fixture.pid" serve --port-file "$work/fixture.port" --log "$work/fixture.log" \
		--hold-release "$work/release-held"
	wait_for "the fixture provider" test -s "$work/fixture.port"
	base="http://127.0.0.1:$(cat "$work/fixture.port")/v1"
	"$cm" providers add --preset lan-openai-compatible --as journey-fixture --base-url "$base" \
		>"$work/declare.txt" 2>&1 || { show_tail "$work/declare.txt"; fail "providers add (the fixture provider)"; }
	"$cm" models add journey-fixture fixture-model-1 --context 131072 --source operator \
		--as custom-journey-fixture >>"$work/declare.txt" 2>&1 ||
		{ show_tail "$work/declare.txt"; fail "models add (the fixture line)"; }
	fx answer -- "$cm" models admit custom-journey-fixture >"$work/admit.txt" 2>&1 ||
		{ show_tail "$work/admit.txt"; fail "models admit (the fixture line)"; }
	say "fixture provider declared and admitted: ok"
	if native; then
		claude_config_before=$(fx claude-config) || fail "the Claude Code configuration could not be read"
		first_interactive_turn
		user_claude_preferences
	fi
	mkdir "$work/project"
	(cd "$work/project" && "$cm" direct --model custom-journey-fixture -- -p "journey turn one") \
		>"$work/turn1.txt" 2>"$work/turn1.err" </dev/null ||
		{ show_tail "$work/turn1.err"; fail "the first managed turn"; }
	first=$(reply_number "$work/turn1.txt")
	[ -n "$first" ] || { show_tail "$work/turn1.txt"; fail "the first managed turn printed no fixture reply"; }
	say "first managed turn: ok (fixture reply $first)"
	(cd "$work/project" && "$cm" -c -- -p "journey turn two") \
		>"$work/turn2.txt" 2>"$work/turn2.err" </dev/null ||
		{ show_tail "$work/turn2.err"; fail "resuming the managed session"; }
	second=$(reply_number "$work/turn2.txt")
	[ -n "$second" ] || { show_tail "$work/turn2.txt"; fail "the resumed turn printed no fixture reply"; }
	fx carried --log "$work/fixture.log" --reply "$second" --earlier "$first" ||
		fail "the resumed turn did not carry the first turn's reply"
	say "resumed managed session: ok (fixture reply $second carried reply $first)"
	if native; then
		session_verbs
	fi
	# Keep the provider reachable for the foreign-listener launch's preflight.
}

no_credential_sent() {
	# $1 = what may have sent one, $2 = the listener's port
	if grep -q '"credentials": true' "$work/foreign.log" 2>/dev/null; then
		fail "$1 sent a credential to the listener on port $2"
	fi
}

foreign_listener() {
	# $1 = the stopped gateway's port
	background "$work/foreign.pid" listen --port "$1" --log "$work/foreign.log"
	wait_for "the foreign listener" fx reach --host 127.0.0.1 --port "$1"
	if "$cm" gateway start >"$work/foreign-start.txt" 2>&1; then
		fail "gateway start accepted another program's listener on port $1"
	fi
	grep -q 'no token was sent' "$work/foreign-start.txt" ||
		{ show_tail "$work/foreign-start.txt"; fail "gateway start did not refuse the listener on port $1"; }
	no_credential_sent "gateway start" "$1"
	if [ -n "${JOURNEY_CLIENT:-}" ]; then
		if (cd "$work/project" && "$cm" -c -- -p "journey turn three") >"$work/foreign-turn.txt" 2>&1 </dev/null; then
			fail "a managed launch ran against another program's listener on port $1"
		fi
		grep -q 'the gateway was not started' "$work/foreign-turn.txt" ||
			{ show_tail "$work/foreign-turn.txt"; fail "the managed launch did not refuse the listener on port $1"; }
		no_credential_sent "the managed launch" "$1"
	fi
	stop_helper "$work/foreign.pid"
	say "another program on the gateway port refused: ok"
}

wsl_checks() {
	# $1 = the stopped gateway's port. Under WSL 2 (JOURNEY_WSL=1; the journey
	# starts in a directory on a Windows drive).
	[ "${JOURNEY_WSL:-}" = 1 ] || return 0
	case $start_dir in
	/mnt/?/*) ;;
	*) fail "the WSL journey must start in a directory on a Windows drive, not $start_dir" ;;
	esac
	status=0
	(cd "$start_dir" && "$cm" doctor) >"$work/wsl-doctor.txt" 2>&1 || status=$?
	[ "$status" -le 1 ] || { show_tail "$work/wsl-doctor.txt"; fail "charmer doctor under WSL exited $status"; }
	for line in 'WSL: running under' \
		'WSL: the Windows-side and WSL-side Claude configurations are separate' \
		'is on a Windows drive: file access is slow'; do
		grep -qF "$line" "$work/wsl-doctor.txt" || { show_tail "$work/wsl-doctor.txt"; fail "doctor under WSL lacks '$line'"; }
	done
	status=0
	(cd "$work" && "$cm" doctor) >"$work/linux-doctor.txt" 2>&1 || status=$?
	[ "$status" -le 1 ] || { show_tail "$work/linux-doctor.txt"; fail "charmer doctor in a Linux directory exited $status"; }
	if grep -qF 'is on a Windows drive: file access is slow' "$work/linux-doctor.txt"; then
		fail "doctor gives the Windows-drive guidance for a directory in the Linux file system"
	fi
	say "WSL: detected, the separate configurations stated, Windows-drive guidance only on a Windows drive: ok"
	windows_state=$start_dir/.journey-windows-state
	mkdir -p "$windows_state"
	status=0
	XDG_STATE_HOME=$windows_state "$cm" gateway start >"$work/wsl-state.txt" 2>&1 || status=$?
	rm -rf "$windows_state"
	[ "$status" -ne 0 ] || fail "gateway start accepted a state root on a Windows drive"
	grep -q 'on a Windows drive' "$work/wsl-state.txt" ||
		{ show_tail "$work/wsl-state.txt"; fail "gateway start did not refuse the state root on a Windows drive"; }
	if fx reach --host 127.0.0.1 --port "$1"; then
		fail "a gateway answers on port $1 after the refused start"
	fi
	say "WSL: a state root on a Windows drive refused the gateway start: ok"
}

uninstall_keeping_state() {
	state=${XDG_STATE_HOME:-$HOME/.local/state}/charmer
	release=$(readlink "$install_dir") || fail "no installed release at $install_dir"
	session_ends
	# Uninstall needs a terminal: the fixture's pseudo-terminal confirms
	# nothing (--yes) and keeps the credentials at their question.
	fx answer -- "$cm" uninstall --keep-setup --yes >"$work/uninstall.txt" 2>&1 ||
		{ show_tail "$work/uninstall.txt"; fail "charmer uninstall --keep-setup --yes"; }
	grep -q 'charmer is uninstalled' "$work/uninstall.txt" ||
		{ show_tail "$work/uninstall.txt"; fail "uninstall did not finish"; }
	[ ! -e "$HOME/.local/bin/charmer" ] || fail "the launcher was not removed"
	if [ -e "$install_dir" ] || [ -e "${install_dir%/current}/$release" ]; then
		fail "the installed release was not removed"
	fi
	[ -d "$state" ] || fail "the state ($state) was not kept"
	say "uninstall keeping the state: ok"
}

first_run() {
	# doctor --first-run on the new installation must succeed: ready, or not
	# ready for nothing but what a fresh install has not set up yet (the
	# check names, states, details and fixes matched in journey_fixture.py);
	# a nonzero exit for any other reason fails the journey
	result=$(fx first-run --launcher "$cm" --install "$install_dir") || fail "doctor's first run (see above)"
	say "first run: ok, $result"
}

bundle_channel() {
	# $1 = after which step: plain doctor names this launcher's channel as
	# the bundle's; a report exits 0, or 1 for a blocked check, anything
	# else fails
	status=0
	"$cm" doctor >"$work/doctor.txt" 2>&1 || status=$?
	[ "$status" -le 1 ] || { show_tail "$work/doctor.txt"; fail "charmer doctor ($1) exited $status"; }
	grep -q 'Installation: this launcher is the bundle channel' "$work/doctor.txt" ||
		{ show_tail "$work/doctor.txt"; fail "charmer doctor ($1) does not report the bundle channel"; }
	say "installation channel ($1): bundle"
}

session_ends() {
	# uninstall refuses (without --force, which the journey never passes)
	# while a managed session has not recorded its end; a print-mode turn
	# records it as its hooks finish, so wait for it, 60 s at most
	result=$(fx ended --install "$install_dir" --timeout 60) ||
		fail "a managed session has not recorded its end (named above)"
	say "managed sessions ended: ok ($result)"
}

expect_version() {
	shown=$("$cm" --version) || fail "charmer --version"
	case $shown in
	"charmer $1 ("*) say "version: $shown" ;;
	*) fail "charmer --version printed '$shown', expected version $1" ;;
	esac
}

permission_diagnostics() {
	[ "${JOURNEY_PERMISSIONS:-}" = 1 ] || return 0
	for path in "$HOME" "$HOME/.local" "$HOME/.local/share" "$HOME/.local/share/charmer" "$HOME/.local/share/charmer/install"; do
		[ ! -e "$path" ] || stat -c '%a %U:%G %n' "$path"
	done
	if command -v getfacl >/dev/null 2>&1; then getfacl -p "$HOME" || true; fi
}

private_directories() {
	fx private-dirs || fail "product state directories are not private"
	say "private state directories under umask 0002: ok"
}

fresh_install() {
	# $1 = release directory; sets version
	installer=$1/install.sh
	[ -f "$installer" ] || installer=$installer_default
	version=$(manifest_version "$1")
	[ -n "$version" ] || fail "$1/MANIFEST.json names no version"
	make_key "$work/key"
	signed_copy "$1" "$work/key" "$work/release"
	permission_diagnostics
	install_from "$work/release" "$work/key" "$installer"
	permission_diagnostics
	private_directories
	if [ -n "${JOURNEY_TARGET:-}" ]; then
		grep -q "^charmer: installed charmer $version ($JOURNEY_TARGET) " "$work/install.txt" ||
			fail "install.sh did not install the $JOURNEY_TARGET bundle"
		say "bundle target: $JOURNEY_TARGET"
	fi
	say "fresh install: ok"
}

journey_install() {
	dist=${1:?usage: journey.sh install DIST}
	fresh_install "$dist"
	journey_installed "$version"
}

journey_installed() {
	version=${1:?usage: journey.sh installed VERSION}
	[ -x "$cm" ] || fail "no launcher at $cm"
	expect_version "$version"
	first_run
	# after the first run, which must meet the installation untouched
	bundle_channel "installed $version"
	if native; then
		observed=$(fx processes --install "$install_dir") ||
			fail "the installed release's process observation (see above)"
		printf '%s\n' "$observed" | sed 's/^/journey:   /'
		say "native process observation: ok"
	else
		say "native checks: not run (JOURNEY_NATIVE=1 runs them, on a disposable host)"
	fi
	[ "${JOURNEY_CLIENT:-}" != fetch ] || fetch_client
	if [ -n "${JOURNEY_CLIENT:-}" ]; then
		if native; then
			user_claude_installation
		fi
		"$cm" setup --step claude --claude-from "$JOURNEY_CLIENT" || fail "setup --step claude --claude-from"
		say "pinned Claude Code from a verified local copy: ok"
		smoke=$("$JOURNEY_CLIENT" --version) || fail "the pinned Claude Code does not run here"
		say "client smoke: $smoke"
	fi
	if [ "${JOURNEY_GATEWAY:-run}" != skip ]; then
		"$cm" setup --step gateway || fail "setup --step gateway"
		"$cm" gateway status || fail "gateway status after the start"
		port=$(gateway_port)
		[ -n "$port" ] || fail "gateway status names no endpoint port"
		confinement "$port"
		if [ -n "${JOURNEY_CLIENT:-}" ]; then
			managed_turn
		else
			say "managed turn: no Claude Code here (set JOURNEY_CLIENT); not run"
		fi
		"$cm" gateway stop || fail "gateway stop"
		"$cm" gateway status >"$work/status.txt" 2>&1 || true
		grep -qi 'not running\|stopped' "$work/status.txt" || fail "the gateway did not stop"
		say "on-demand gateway lifecycle: ok"
		private_directories
		foreign_listener "$port"
		wsl_checks "$port"
		if [ -n "${JOURNEY_CLIENT:-}" ]; then
			stop_helper "$work/fixture.pid"
			if native; then
				forget_session "$held_session"
				user_claude_untouched
			fi
			if [ -n "${JOURNEY_KEEP:-}" ]; then
				# The restarted journey's fixture port, and a gateway that is
				# running when the system shuts down.
				mkdir -p "$JOURNEY_KEEP"
				cp "$work/fixture.port" "$JOURNEY_KEEP/fixture.port"
				"$cm" gateway start || fail "gateway start before the shutdown"
				say "gateway left running for the shutdown: ok"
			fi
		fi
	fi
	if [ "${JOURNEY_UNINSTALL:-run}" != skip ]; then
		uninstall_keeping_state
	fi
}

journey_restarted() {
	version=${1:?usage: journey.sh restarted VERSION KEEPDIR}
	keep=${2:?usage: journey.sh restarted VERSION KEEPDIR}
	[ -x "$cm" ] || fail "no launcher at $cm"
	[ -s "$keep/fixture.port" ] || fail "$keep has no fixture port (run the installed journey with JOURNEY_KEEP first)"
	expect_version "$version"
	# What status says of a gateway the shutdown stopped is recorded; the
	# check is that the next launch starts it.
	"$cm" gateway status >"$work/status.txt" 2>&1 || true
	sed 's/^/journey:   /' "$work/status.txt"
	# The fixture provider comes back on the port the declared provider names.
	background "$work/fixture.pid" serve --port "$(cat "$keep/fixture.port")" --port-file "$work/fixture.port" \
		--log "$work/fixture.log"
	wait_for "the fixture provider" test -s "$work/fixture.port"
	mkdir "$work/project"
	(cd "$work/project" && "$cm" direct --model custom-journey-fixture -- -p "journey turn after the restart") \
		>"$work/turn.txt" 2>"$work/turn.err" </dev/null ||
		{ show_tail "$work/turn.err"; fail "the first managed launch after the shutdown"; }
	reply=$(reply_number "$work/turn.txt")
	[ -n "$reply" ] || { show_tail "$work/turn.txt"; fail "the launch after the shutdown printed no fixture reply"; }
	"$cm" gateway status || fail "gateway status after the launch"
	port=$(gateway_port)
	if [ -z "$port" ] || ! fx reach --host 127.0.0.1 --port "$port"; then
		fail "the launch after the shutdown did not start the gateway"
	fi
	say "on-demand start after the shutdown: ok (the launch started the gateway; fixture reply $reply)"
	"$cm" gateway stop || fail "gateway stop"
	stop_helper "$work/fixture.pid"
	uninstall_keeping_state
}

journey_update() {
	keys=${1:?usage: journey.sh update KEYDIR DIST_A DIST_B}
	first=${2:?usage: journey.sh update KEYDIR DIST_A DIST_B}
	second=${3:?usage: journey.sh update KEYDIR DIST_A DIST_B}
	installer=$first/install.sh
	[ -f "$installer" ] || installer=$installer_default
	old=$(manifest_version "$first")
	new=$(manifest_version "$second")
	signed_copy "$first" "$keys" "$work/a"
	signed_copy "$second" "$keys" "$work/b"
	install_from "$work/a" "$keys" "$installer"
	expect_version "$old"
	bundle_channel "installed $old"
	"$cm" update --check --from-dir "$work/b" >"$work/check.txt" || fail "update --check"
	grep -q "charmer $new is available" "$work/check.txt" || fail "update --check does not offer $new"
	say "update check: ok"
	bundle_channel "update check"
	"$cm" update --from-dir "$work/b" --yes || fail "update to $new"
	expect_version "$new"
	bundle_channel "updated to $new"
	"$cm" update --from-dir "$work/b" >"$work/again.txt" || fail "update at the same version"
	grep -q "is up to date" "$work/again.txt" || fail "an equal version is not reported up to date"
	say "update and up to date: ok"
	bundle_channel "up to date"
	"$cm" update --rollback --yes || fail "update --rollback"
	expect_version "$old"
	say "rollback: ok"
	bundle_channel "rolled back to $old"
}

unit_property() {
	# $1 = unit, $2 = property
	systemctl --user show --property "$2" --value "$1"
}

journey_service() {
	dist=${1:?usage: journey.sh service DIST}
	command -v systemctl >/dev/null 2>&1 || fail "systemctl is not installed"
	systemctl --user show-environment >/dev/null 2>&1 ||
		fail "no user service manager answers (XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-unset})"
	fresh_install "$dist"
	expect_version "$version"
	# The first port a new install takes is held: the gateway records another.
	busy=18317
	background "$work/busy.pid" listen --port "$busy" --log "$work/busy.log"
	wait_for "the listener on port $busy" fx reach --host 127.0.0.1 --port "$busy"
	"$cm" setup --step gateway || fail "setup --step gateway"
	private_directories
	port=$(gateway_port)
	if [ -z "$port" ] || [ "$port" = "$busy" ]; then
		fail "the gateway did not record another port than $busy (${port:-none})"
	fi
	say "alternate port: ok ($port)"
	"$cm" gateway service install || fail "gateway service install"
	"$cm" gateway service install || fail "gateway service install again (the refresh)"
	unit=charmer-gateway.service
	rendered=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit
	[ -f "$rendered" ] || fail "no rendered unit at $rendered"
	fragment=$(unit_property "$unit" FragmentPath) || fail "systemctl --user show $unit"
	[ "$fragment" = "$rendered" ] || fail "the manager loads $unit from '$fragment', not $rendered"
	active=$(unit_property "$unit" ActiveState)
	[ "$active" = active ] || { systemctl --user status "$unit" >&2 || true; fail "$unit is $active, not active"; }
	pid=$(unit_property "$unit" MainPID)
	exe=$(readlink "/proc/$pid/exe") || fail "the unit's main process ($pid) is gone"
	case $exe in
	"$HOME/.local/share/charmer/install/"*) ;;
	*) fail "the unit runs $exe, not the installed release" ;;
	esac
	fx reach --host 127.0.0.1 --port "$port" || fail "the service's gateway does not answer on 127.0.0.1:$port"
	"$cm" gateway service status >"$work/service.txt" 2>&1 ||
		{ show_tail "$work/service.txt"; fail "gateway service status"; }
	for line in 'gateway service: installed' 'backend: systemd' 'unit: current'; do
		grep -q "$line" "$work/service.txt" || { show_tail "$work/service.txt"; fail "gateway service status lacks '$line'"; }
	done
	say "supervised gateway service: ok ($fragment, $active, main process $exe)"
	"$cm" gateway service uninstall || fail "gateway service uninstall"
	[ ! -e "$rendered" ] || fail "uninstall left $rendered"
	loaded=$(unit_property "$unit" LoadState)
	[ "$loaded" = not-found ] || fail "$unit is still $loaded after the uninstall"
	stop_helper "$work/busy.pid"
	say "gateway service uninstall: ok (the unit is gone)"
}

journey_prepare() {
	dist=${1:?usage: journey.sh prepare DIST OUT}
	out=${2:?usage: journey.sh prepare DIST OUT}
	[ -f "$dist/install.sh" ] || fail "$dist has no install.sh"
	make_key "$out/key"
	signed_copy "$dist" "$out/key" "$out/release"
	cp "$dist/install.sh" "$out/release/install.sh"
	say "signed release with its key in $out: ok"
}

mode=${1:-}
[ "$#" -gt 0 ] && shift
case $mode in
install) journey_install "$@" ;;
installed) journey_installed "$@" ;;
restarted) journey_restarted "$@" ;;
update) journey_update "$@" ;;
service) journey_service "$@" ;;
prepare) journey_prepare "$@" ;;
*) fail "usage: journey.sh install DIST | installed VERSION | restarted VERSION KEEPDIR | update KEYDIR DIST_A DIST_B | service DIST | prepare DIST OUT" ;;
esac
say "done"
