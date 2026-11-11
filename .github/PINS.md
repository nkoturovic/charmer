# CI pins

Every third-party action, tool and container image the workflows in
`workflows/` use is pinned, and each pin below was checked against its
upstream with read-only metadata: an action's or tool's release tag
resolved with `git ls-remote <repository> refs/tags/<tag>` (and the
release with `gh api repos/<owner>/<repo>/releases/tags/<tag>`), a Python
or Go tool's version on its package index, a container image's index
digest read from its registry. `tests/test_release_ci.py` keeps this file
and the workflows in agreement: a pin that changes in a workflow needs its
row here, re-verified, with the new date.

Pins follow reviewed releases, not moving runner defaults. The CI compiler
is the gateway's own (`gateway/UPSTREAM.json`), so a gateway toolchain
update reaches the scanners with no second pin; setuptools follows
`pyproject.toml`.
Every JavaScript action runs on Node.js 24 (`runs.using: node24` in
the pinned commit's `action.yml`; a composite action's own steps are
pinned by its maintainers). The inputs the workflows pass were checked
against each pinned `action.yml` and the release notes of every major
version in between: none of them changed meaning. Two behaviour changes
apply: `download-artifact` now fails a download whose digest does not
match (the safe default, kept), and `attest-build-provenance` 4 is a thin
wrapper around `actions/attest` (same inputs).

## Actions (pinned by commit)

| Action | Tag | Commit | Runs on | Verified |
| --- | --- | --- | --- | --- |
| actions/checkout | v7.0.1 | 3d3c42e5aac5ba805825da76410c181273ba90b1 | node24 | 2026-11-11 |
| actions/setup-python | v7.1.0 | 6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 | node24 | 2026-11-11 |
| actions/upload-artifact | v7.0.2 | cf430e030ddbb5b0abf93d22962f4752f3646cd9 | node24 | 2026-11-11 |
| actions/download-artifact | v8.0.2 | 9000827ccba6bdab643e8b6fd33ac0654aef8333 | node24 | 2026-11-11 |
| actions/attest-build-provenance | v4.2.2 | 4d101475d8b20a2381f78447822ac1eab6504dd8 | composite | 2026-11-11 |
| cachix/install-nix-action | v31.11.4 | 91f6a0f59e94dfb2cfed0f4000f36a68ead1d20c | composite | 2026-11-11 |

Each tag is a lightweight tag: the commit is the tag's own object
(`gh api repos/<owner>/<repo>/git/refs/tags/<tag>` names a commit), and
each is the repository's latest release (not a draft or prerelease).

## Tools (pinned by version)

| Tool | Version | Upstream identity | Verified |
| --- | --- | --- | --- |
| actionlint | v1.7.12 | github.com/rhysd/actionlint tag v1.7.12 = 914e7df21a07ef503a81201c76d2b11c789d3fca (Go module proxy origin and `@latest` agree; latest release) | 2026-11-11 |
| zizmor | 1.32.0 | PyPI zizmor 1.32.0 (latest), not yanked, Python >=3.11; the manylinux x86_64 wheel hash agrees in PyPI JSON and the simple index; `--offline` retained, no workflow lockfile adopted | 2026-11-11 |
| govulncheck | v1.9.0 | golang.org/x/vuln tag v1.9.0 = e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5 (Go module proxy origin and `@latest` agree); CI builds it with the gateway's Go and checksum-database verification | 2026-11-11 |
| Pester | 6.2.0 | PowerShell Gallery Pester 6.2.0 (latest, not a prerelease); github.com/pester/Pester tag 6.2.0 = 98e56d65da1642a7158992b50ded240c42a3ac18 | 2026-11-11 |
| setuptools | 84.2.1 | PyPI setuptools 84.2.1 (latest), not yanked; wheel hash agrees in PyPI JSON and the simple index; Python >=3.10 (below the supported 3.11 floor) | 2026-11-11 |
| ShellCheck | 0.11.0 | koalaman/shellcheck v0.11.0 (latest release), Linux x86_64 archive verified against the GitHub release asset digest | 2026-11-11 |
| Go (CI tools) | gateway/UPSTREAM.json | the gateway's toolchain pin: its go.dev linux-amd64 archive and sha256, verified there; `GOTOOLCHAIN=local` disables automatic toolchain replacement; newest stable is 1.27.4, the gateway re-pin owns the move | 2026-11-11 |
| bubblewrap | 0.13.1 | github.com/containers/bubblewrap tag v0.13.1 (latest release, 2026-10-14), source archive verified against the GitHub release asset digest and the release notes' sha256; built unprivileged | 2026-11-11 |

## Tool artifacts (pinned by sha256)

| Tool | Artifact | sha256 | Verified |
| --- | --- | --- | --- |
| zizmor | zizmor-1.32.0-py3-none-manylinux_2_28_x86_64.whl | 84a6acb975accc13408786a2bd274b572537ca19c933ebbc13e78f3988562071 | 2026-11-11 |
| setuptools | setuptools-84.2.1-py3-none-any.whl | ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2 | 2026-11-11 |
| ShellCheck | shellcheck-v0.11.0.linux.x86_64.tar.xz | 8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198 | 2026-11-11 |
| bubblewrap | bubblewrap-0.13.1.tar.xz | 0e85c8702248b8dd64144f594147553650cdd0e4c951690345faace79c217c4d | 2026-11-11 |

`ci_tools.sh` verifies these archives, and the Go archive named by
`gateway/UPSTREAM.json`'s `toolchain` (its linux-amd64 file and sha256), before
extracting them into fresh runner-temporary directories. The Go module installs
retain checksum-database verification and print the compiler and module
versions they were built with (`go version -m`); neither compiler selection nor
ShellCheck depends on PATH defaults.

bubblewrap is built from its source release (meson, ninja, libcap from the
runner's Ubuntu archive; no SELinux, man pages, completions or tests; never
setuid, which 0.12 no longer builds) and installed root-owned in root-owned
directories, as the isolated tests require of the `bwrap` they run.
`bubblewrap_floor.sh` then checks the first `bwrap` on PATH: bubblewrap 0.12
or later (GHSA-pxhw-h44j-8pfx, CVE-2026-87766, is fixed there), not setuid
or setgid, its version, path and sha256 printed and added to the job
summary. Nothing passes `--not-a-security-boundary`. The CI lanes take the
newest release, 0.13.1; the Nix lane now selects the same version from
nixos-unstable, ending its 0.12.0 hold. The development shell's floor stays.
Before the pin, 0.13.1 passed the isolated test classes that run without the
pinned client or gateway on a local build (its source archive by the same
hash).

The CI fast lane installs zizmor with `pip install --require-hashes`
against this digest (the one PyPI records for the wheel), so it runs the
same file the local lint below ran; any other file, a rebuilt or
replaced wheel included, fails the install. The battery installs
setuptools the same way, for all three of its Python versions (the
distribution tests build with it; a skipped distribution test fails the
job); the digest is the one PyPI records, and the local wheel the
distribution tests ran with has it.

Pester is installed with `-RequiredVersion`, so a new major never arrives
unannounced. The suite in `tests/pwsh` uses the classic `Should -Be`,
`Should -Throw` and `Should -Invoke` assertions and filtered mocks that
cover every call they intercept; it uses none of what Pester 6 removed
(`Assert-MockCalled`, `-Pending`, `-Focus`, empty `-ForEach`, duplicate
setup blocks, the fall-through from an unmatched filtered mock to the real
command).

## Container images (pinned by index digest)

| Image | Index digest | Verified |
| --- | --- | --- |
| ubuntu:22.04 | sha256:076e7bf82319619f28dd0aa9c02ab34b055ad37100395b4f49a2ffba04fb5e11 | 2026-11-11 |
| ubuntu:24.04 | sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 | 2026-11-11 |
| debian:12 | sha256:268fb24c39bca92d326dd83ea3abebe4a3f7751c804ffef23bb6d857dd343ff8 | 2026-11-11 |
| fedora:44 | sha256:15da97ed9aa94dd3d92c3925eb89520f6c80e07ea7ee24cba848d1f8b6912b3e | 2026-11-11 |

The digests are the multi-platform image indexes on Docker Hub
(`library/<name>`), still the current index of each tag on the day
verified; the runner pulls its own platform's image from the index. On
2026-11-11 the registry's `Docker-Content-Digest`, the sha256 of the index
bytes it served and Docker Hub's tag API agreed on each digest above. The
Ubuntu indexes were pushed on 2026-10-29, Debian 12's on 2026-11-05 and
Fedora 44's on 2026-11-02. Each index lists `linux/amd64`; this is identity
verification, not a scan of the image's packages. Debian 12 remains a
coverage choice, not a claim that it is the newest Debian major.

## Workflow lint before the first CI run

On 2026-10-03 the two workflow linters ran locally over `.github/` at the
pinned versions, each fetched by hash: actionlint from the release archive
`actionlint_1.7.12_linux_amd64.tar.gz` (sha256
`8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8`, as
listed in the release's `actionlint_1.7.12_checksums.txt` and in GitHub's
asset digest), zizmor from the PyPI wheel
`zizmor-1.30.1-py3-none-manylinux_2_28_x86_64.whl` with
`pip download --require-hashes` (sha256
`eee12266b793cb87ad4a7e3af2e72404f8a63e3de5eb099b80bf7b1cfd232a8e`, the
digest PyPI records), run with `--offline`.

- actionlint, with shellcheck 0.11.0 on the path, reported two shellcheck
  notes, both fixed: the full-suite step passed its module list through an
  unquoted `$(cat …)` (now a bash array), and the Linux journey's
  `su journey -c '…'` keeps `$HOME` in single quotes on purpose (it is the
  journey user's), now stated with a `shellcheck disable=SC2016` comment.
  Its pyflakes rule had nothing to check: no step runs with `shell: python`.
- zizmor's default persona reports nothing. Its pedantic persona also
  asked for a comment on each write permission (added). Exception, kept:
  the pedantic and auditor personas list `anonymous-definition`
  (informational) for the 15 jobs, which carry no `name:`; the job ids
  already say what each job does, and a `name:` would only repeat them and
  change the names the checks are reported under.
- The CI lint step ran zizmor as `python3 -m zizmor`, which fails: the
  wheel ships an executable and no Python module. It now installs the wheel
  into a directory of its own, by the hash above (`--require-hashes`), and
  runs `bin/zizmor` from there.

## Workflow lint of the release-candidate wiring

On 2026-10-03, after the resolve job, the evidence producers, the native
lanes (arm64 Linux, the systemd user manager, Intel macOS behind its
repository variable, WSL 2 through install.ps1), the Nix garbage-collector
root step and the schedule guard were added, the same pinned actionlint
(with shellcheck on the path) and zizmor (`--offline`, default, pedantic
and auditor personas) ran over `.github/` again: actionlint reported
nothing; zizmor reported nothing beyond `anonymous-definition`
(informational), now for the 20 jobs, kept for the reason above. These
changes add no action, tool or image other than the setuptools wheel
above.

## Refresh verification

On 2026-10-18 the refreshed action tags were resolved through GitHub's refs
API and their exact-commit `action.yml` files inspected. Upload/download's
inputs and Node 24 runtime are unchanged; install-nix-action now selects
Nix 2.35.3. The artifact patch updates improve retries; they are not a claim
that every transitive dependency advisory is fixed.

The actual bytes of `zizmor-1.31.1-py3-none-manylinux_2_28_x86_64.whl`,
setuptools, ShellCheck and the Go compiler matched the publisher digests
above. The three refreshed image indexes were hashed and matched the
registry's `Docker-Content-Digest`. Fedora's previously verified pin is kept.

The standalone Python runtime pins and full-archive provenance move together
to 20261013 with CPython 3.14.8. All eight archives matched both GitHub's asset
digest and the release's SHA256SUMS. The regenerated inventories supply the
per-target notices and CycloneDX components, including Expat 2.9.0 and libffi
3.8.0; Tcl/Tk and Berkeley DB remain pruned.

On 2026-10-19 the bundled runtime moved to CPython 3.15.0 from the same
20261013 release, after the CI battery passed on 3.15. The four
`install_only_stripped` archives and their four `pgo+lto-full` archives were
downloaded again and matched GitHub's asset digest (read from the release API
that day) and the release's SHA256SUMS; `pythonbuild/downloads.json` kept its
recorded sha256. The licence inventory was regenerated from those full
archives; the library versions and texts are unchanged, `_remote_debugging`
newly links zstd. Each full archive's `PYTHON.json` describes the default
build: `Py_GIL_DISABLED` 0 with no `t` ABI flag (not free-threaded),
`--enable-experimental-jit=yes-off` (built, off unless enabled at run time)
except on Intel macOS, which has no JIT; the inventory records both and
refuses anything else. The prune list names `lib/python{python_minor}`, so its
entries follow the pin; all four prepared runtimes keep no `.pth` or `.start`
file and no `sitecustomize` or `usercustomize` (the build refuses one).

The vulnerability watch downloads the latest published release from public
URLs, verifies its signature with the packaged release trust, then verifies
every bundle against the signed checksums. It never runs downloaded code.
Binary-mode govulncheck uses the same exact disposition validator as release
CI. The default branch's gateway dispositions apply only when their version
and all four gateway hashes match the signed manifest. Otherwise the watch
fetches the signed manifest's release tag and reads its dispositions (data
only; no checkout or execution); the report records the source and file hash.
OSV queries the bundles' copy of gateway/licenses/modules.json; CPython uses
the upstream release tag's commit (OSV GIT ranges), not a PyPI package with a
similar name. Only matching Go findings validated by the binary gate are
reviewed automatically.

Non-Go reviews live in `vulnerability-watch-reviewed.json` (`format: 1`,
`cpython` list). Each entry requires the exact OSV `id`, complete `aliases`,
`modified` timestamp, bundled CPython `version`, a concrete `reason`, and a
`re_review` date (YYYY-MM-DD, UTC). On that date, or when the id, aliases,
revision or version changes, the match needs review again. Each entry is
an advisory-specific review, never a version-wide or alias-only exemption;
the current cookiejar non-use review is described below.

GitHub starts this repository's scheduled runs hours after their cron time:
in October 2026 between about 4.5 and 8 hours (the watch's Tuesday 06:17 UTC
schedule ran at 11:43 on 2026-10-20 and at 12:18 on 2026-10-27). A scheduled
run absent a few hours after its time is late, not skipped; check again
before treating it as missing.

Failures, new traces and unreviewed OSV matches fail the workflow and attempt
to open or update one issue. A sorted finding fingerprint excludes run URLs
and scanner timestamps; changed findings post a delta comment before updating
the body, so subscribers are notified, while identical findings stay silent.
Failed tool installation or an interrupted scan also produces a fallback
report and issue body. Reporting still needs GitHub and its issue API to be
available. No release, pin or disposition changes.

Local lint of this refresh used actionlint 1.7.12 with ShellCheck 0.11.0 and
zizmor 1.31.1 (`--offline`): both passed with no new findings. The existing
informational job-name suppressions remain; no new suppression was added.

## Online workflow audit

`zizmor-online.yml` (weekly while `CM_SCHEDULES` is `on`, or by hand) runs the
same hash-installed zizmor 1.32.0 without `--offline`, so the audits that read
GitHub's API run too (impostor commits, ref confusion, stale action refs,
known-vulnerable actions): the default persona, then the pedantic persona at
`--min-severity low` (its informational `anonymous-definition` notes are the
exception recorded above). The workflow grants nothing; its one job reads the
repository (`contents: read`) and passes the workflow token to zizmor only.
It opens no issue and fixes nothing: a finding fails the run. The pull request
lint stays `--offline`; `pin-watch.yml` keeps its version watch and issue.

On 2026-10-19, with the bubblewrap, compiler and runtime changes and this
workflow, actionlint 1.7.12 (ShellCheck 0.11.0 on the path) and zizmor 1.31.1
(`--offline`, default persona, and pedantic at `--min-severity low`) ran over
`.github/` locally: no findings.

## Nix inputs (flake.lock)

`nixpkgs` follows `nixos-unstable`, locked on 2026-11-11 to
`f4a03c008967841f2a0b3cb7c436fc6fc31671f6` (2026-11-07,
`sha256-ziZn2PvCJE5JEEpqTdxXwJpPB+L3YqoCpTAMCzyaP3U=`). The
[channel revision](https://channels.nixos.org/nixos-unstable/git-revision)
and [branch head](https://api.github.com/repos/NixOS/nixpkgs/commits/nixos-unstable)
agreed; `nix flake update nixpkgs` recorded the content hash. A
`nix eval` of the new lock returned the six versions below. A Nix build of
`gateway-linux-amd64` at the new lock gave sha256
`092bb347b763d00fc58e8b18ba4199b8deadfe68d3dc36793d1c58068af991d6`,
identical to the native content-bound build and the signed 2.4.0 manifest.
Thus the lock move alone leaves the gateway bytes unchanged. This is the
8.0.45 baseline comparison, not qualification of a later gateway re-pin;
that pair still needs its own Nix/native comparison at the release candidate.

The input supplies the Nix channel's Python and development tools. The
gateway uses the recipe's pinned Go archive and vendored modules, and
release bundles carry their own runtime. No whole-closure vulnerability
scan is claimed by the version evaluation or the byte comparison.

| Package | Version in the locked source | Newest upstream | Disposition | Verified |
| --- | --- | --- | --- | --- |
| bubblewrap | 0.13.1 | 0.13.1 | current; the channel update ends the 0.12.0 hold; the required floor 0.12.0 (GHSA-pxhw-h44j-8pfx, CVE-2026-87766) in `nix/devshell.nix` stays | 2026-11-11 |
| python3 | 3.14.10 | 3.14.10 / 3.15.2 | current on the channel's selected 3.14 line; the 3.14.8 hold ends, including the missing eMudhra and tar-filter fixes; CVE-2026-124249 remains a bounded cookiejar non-use review, not a fix; bundles ship 3.15.2 separately | 2026-11-11 |
| openssl (default) | 3.5.9 | 3.5.9 LTS / 4.1.0 | current on the selected 3.5 LTS line (the Python ssl recipe); the newer 4.1.0 feature line is not selected; verified by upstream release and 3.5 tag metadata, not a closure scan | 2026-11-11 |
| git | 2.56.0 | 2.56.1 | channel lag; development shell and tests only, no override; advisory applicability not assessed; re-check at the release candidate | 2026-11-11 |
| gnupg | 2.4.9 (FreePG) | 2.5.24 | channel hold for development shell and tests, not bundle release trust; the recipe is byte-identical to the old lock; upstream 2.4 is EOL, FreePG support and applicability of newer security fixes remain unestablished; re-check at the release candidate | 2026-11-11 |
| cacert | 3.131.1 | NSS 3.133 | version-label lag with identical `certdata.txt` blobs (`9ee50e8d53fe7c98676718e84e1222cb31f0ed35`) at NSS_3_131_1_RTM and NSS_3_133_RTM; no root-data change demonstrated and no guarantee about an operator-selected certificate bundle | 2026-11-11 |

The Python 3.14.8 security hold is removed, not extended: the new selected
3.14.10 contains the intervening upstream fixes. CVE-2026-124249 affects
both 3.14.10 and bundled 3.15.2, but the same charmer source installs no
cookie processor (the bounded review below). This source-level review is
not a trace of a realized Nix interpreter; re-check at the release candidate
or by 2026-12-01, whichever comes first.

The GnuPG recipe applies `source-2.4.9-freepg-1` patches, but their presence
is not a post-EOL support promise. The old and new lock's
`pkgs/tools/security/gnupg/24.nix` both name blob
`6625b827848490f4728dc988c0435d5088ac2705`, checked on 2026-11-11. Upstream 2.5.24 (2026-09-23) follows
2.5.23, whose release notes fix an exploitable format-string bug under
`gpgsm --debug x509`, an out-of-bounds read in dirmngr's DNS CERT record
parser and an out-of-bounds access in scdaemon's card data parsing; whether
the FreePG patches carry these fixes for 2.4.9 has not been established, so
they are not claimed. GnuPG is not shipped in release bundles;
installed release verification uses the launcher's SSH-signature verifier.
The OpenSSL 3.5.9 security release and NSS 3.131.1 CA changes are recorded in
[OpenSSL's advisories](https://openssl-library.org/news/vulnerabilities/) and
[the NSS release notes](https://firefox-source-docs.mozilla.org/security/nss/releases/nss_3_131_1.html).
The Python fixes now included are in the
[3.14.10 changelog](https://docs.python.org/release/3.14.10/whatsnew/changelog.html);
[GnuPG's support page](https://gnupg.org/download/index.html) records the
2.4 end-of-life.

## Runtime and workflow refresh

On 2026-11-11 the latest-release and tag-ref APIs resolved all six Actions
to the action table. `cachix/install-nix-action` moves from 31.11.3 to
**v31.11.4**, including the new `v` prefix. Its exact-commit `action.yml` is
byte-identical: still composite, same inputs, none passed here. Its script
moves Nix 2.35.4 to 2.35.5 and fixes the handling of extra configuration
and default experimental flags. The five other tags remain latest,
non-draft, non-prerelease, at the same commits. GitHub advisory queries for
each selected action/version returned no match; this is not a transitive
npm dependency scan.

setuptools 84.2.1 is still PyPI's newest, not yanked, Python >=3.10. Its
wheel hash agrees in the version JSON and simple index. zizmor moves to
1.32.0, Python >=3.11; the same two metadata sources agree on the sha256
of `zizmor-1.32.0-py3-none-manylinux_2_28_x86_64.whl`. The existing
hash-required installation and offline invocations are retained; its new
workflow-lockfile feature is not adopted.

CPython 3.15.2 comes from python-build-standalone 20261110, the latest
release published 2026-11-11T01:51:17Z. Before any of the eight archive
downloads below, the release API's asset digests
(<https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20261110>)
agreed with the release's
[SHA256SUMS](https://github.com/astral-sh/python-build-standalone/releases/download/20261110/SHA256SUMS).
The runtime fetch tool then verified each downloaded size and sha256.
The install-only pins live in `packaging/python-runtimes.json`; the
full-archive hashes and sizes live in the regenerated licence inventory.

### CPython runtime archives

| Archive | sha256 | Verified |
| --- | --- | --- |
| cpython-3.15.2+20261110-aarch64-apple-darwin-install_only_stripped.tar.gz | 7e58607647b5b4832092a339ff1c55eeeccee128221232509b8fc8d27957065e | 2026-11-11 |
| cpython-3.15.2+20261110-aarch64-apple-darwin-pgo+lto-full.tar.zst | 53af18af034de253bac9b08d95c861d5b08a6d46a87ec5c9d8b34577fe48b4ad | 2026-11-11 |
| cpython-3.15.2+20261110-x86_64-apple-darwin-install_only_stripped.tar.gz | 11143c2a2556b14619e8241d3f74bb43ad97181c0eb4909bab87d7aad1154e49 | 2026-11-11 |
| cpython-3.15.2+20261110-x86_64-apple-darwin-pgo+lto-full.tar.zst | 15807b2f364aa77636ff87760ab60e12f3fb547022232d54d0334bbf9b3eb2d3 | 2026-11-11 |
| cpython-3.15.2+20261110-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz | d1367a6c3cd86a9e35712072841f1376859f3b47a2a54e224421a0f9fd169047 | 2026-11-11 |
| cpython-3.15.2+20261110-aarch64-unknown-linux-gnu-pgo+lto-full.tar.zst | 56246cc19968bbfc32a92a2c7887fa2fd4ba1917976dccb2d0ee3e43468b1b3d | 2026-11-11 |
| cpython-3.15.2+20261110-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz | ad23d1b73fac26440b0306ae06911031344c49a90ec19716c8362e7fa40cae43 | 2026-11-11 |
| cpython-3.15.2+20261110-x86_64-unknown-linux-gnu-pgo+lto-full.tar.zst | a61124f187ea3b379d6b78806ac06edd43db524eef411925eca05fb9357589d5 | 2026-11-11 |

The release's `pythonbuild/downloads.json` has sha256
`d48f25ee30c3ba8006c9a3046d4d3c5ec061a3f1fd831a1e44562f2e8547ef60`
(the contents API and raw source agree). The full archives' `PYTHON.json`
records GIL enabled on all targets and JIT built but off by default except
Intel macOS (not built), unchanged. Expat moves 2.9.0 → 2.10.0 on every
target; Linux's bundled zlib moves 1.3.2 → 1.4 and libedit moves to
20260512-3.1. macOS uses system zlib and libedit, not those bundled copies.
OpenSSL 3.5.9, libffi 3.8.0 and zstd 1.5.7 stay. The inventory was derived
from all four full archives; only the zlib licence text changed.

All four install-only archives passed member/link validation, delete-only
pruning (24 matched entries each), shipped-library inventory validation,
and the startup-file, dangling-link and store-reference checks: no retained
`.pth`, `.start`, `sitecustomize` or `usercustomize`. The prepared host
interpreter reports 3.15.2. Native execution on the other three targets,
the journeys and cross-host reproduction remain release-candidate checks.

CPython 3.15.2 adds the `ssl` distrust of the compromised eMudhra
intermediate CA keys (CVE-2026-115557 / PSF-2026-46) and fixes the tar
filter (CVE-2026-114583 / PSF-2026-45). With the launcher's own TLS context
(`tls.context()`, trust from `SSL_CERT_FILE`) and CPython's gh-160040 test
chain, 3.15.1 completes the handshake and 3.15.2 rejects it
(`SSLCertVerificationError`, verify code 28), checked on 2026-10-26.

On 2026-11-11 OSV reports
[CVE-2026-124249](https://api.osv.dev/v1/vulns/CVE-2026-124249) for CPython
3.15.2 (release-tag commit `5e58eaad7c58df58dfa8ea549c87996e01dd1077`)
and 3.14.10. The affected `http.cookiejar` code can delete a stored cookie
from an expired response cookie before policy validation. It remains
present in PBS 20261110; the PBS refresh does not fix this advisory.

The source review found no cookiejar import, cookie jar construction or
cookie processor in the 162 product Python files. The five bounded
`urllib.request.build_opener` sites are `acquire.py`, `release_manifest.py`,
both transports in `release_update.py`, and `proxy.py`: they add HTTP,
HTTPS, redirect and proxy handlers, not `HTTPCookieProcessor`. In the
selected 3.15.2 stdlib, the default opener does not add that processor and
the cookiejar import is local to its constructor. Dynamic product imports
select fixed charmer modules, and there is no unpickling path or external
Python dependency. This is bounded source-level non-use, not a claim that
the vulnerable code is absent or safe for arbitrary interpreter use.

`vulnerability-watch-reviewed.json` records exactly id `CVE-2026-124249`,
aliases `[]`, revision `2026-11-11T10:47:25.104930807Z`, version `3.15.2`
and re-review date `2026-12-01`. The nanoseconds are part of that exact
revision. Re-review also occurs at the release candidate, on a changed
advisory/runtime, or if cookie processing, unpickling, external dependencies
or dynamic loading are added. Watch run
[43037446186](https://github.com/nkoturovic/charmer/actions/runs/43037446186)
reported the new CPython finding on the published 2.4.0 release; this review
does not suppress any of that run's separate gateway findings.

The pin watch now reads and reports both npm `latest` (the user-install
channel) and `stable`. Either newer tag is included in the issue report.
It also fails when the pin is older than `latest` and `latest` differs
from `stable`; the issue step still runs after that failed comparison.
A pin at or ahead of `latest` does not fail merely because `stable` lags.
Both lookups remain mandatory: missing or malformed data never passes as
a quiet week. The watch never changes a pin or installs the client.

## Pins against their newest upstream

Every pin was compared with its newest stable upstream release from
read-only metadata on 2026-11-11; a pin moves when a newer stable exists,
and a hold needs a recorded reason (the Nix table above names the selected
lines and remaining lags). The gateway's own toolchain and module pins
are qualified together, separately from the runtime and workflow refresh.

| Input | Pinned | Newest upstream | Source | Result | Checked |
| --- | --- | --- | --- | --- | --- |
| Go (gateway toolchain, CI tools) | 1.27.3 | 1.27.4 | go.dev download API | newer security release; move belongs to the gateway re-pin, not an accepted hold | 2026-11-11 |
| CPython (bundled) | 3.15.2 | 3.15.2 (3.16.0a2 is a prerelease) | CPython release-tag refs | already newest stable; cookiejar finding reviewed separately | 2026-11-11 |
| python-build-standalone | 20261110 | 20261110 | GitHub latest release, asset digests and SHA256SUMS | moved from 20261026; eight downloaded archives verified | 2026-11-11 |
| setuptools | 84.2.1 | 84.2.1 | PyPI JSON and simple index, wheel hash as pinned, not yanked | already newest | 2026-11-11 |
| actions/checkout, setup-python, upload-artifact, download-artifact, attest-build-provenance | the action table | the same tags and commits | each repository's latest release and tag ref | already newest | 2026-11-11 |
| cachix/install-nix-action | v31.11.4 | v31.11.4 | GitHub latest release and tag ref; exact-commit action and script | moved from 31.11.3; composite inputs unchanged | 2026-11-11 |
| zizmor | 1.32.0 | 1.32.0 | PyPI JSON and simple index; GitHub release | moved from 1.31.1; offline lint re-verified, no workflow lockfile | 2026-11-11 |
| actionlint, govulncheck, Pester, ShellCheck, bubblewrap | the tool tables | the same versions and digests | latest releases, Go module proxy, PowerShell Gallery, release asset digests | already newest | 2026-11-11 |
| ubuntu:22.04, ubuntu:24.04 | the image table | indexes pushed 2026-10-29 | registry header, index-byte hash and Docker Hub tag API agree | moved | 2026-11-11 |
| debian:12 | the image table | index pushed 2026-11-05 | registry header, index-byte hash and Docker Hub tag API agree | moved | 2026-11-11 |
| fedora:44 | the image table | index pushed 2026-11-02 | registry header, index-byte hash and Docker Hub tag API agree | moved | 2026-11-11 |
| nixpkgs (nixos-unstable) | f4a03c008967841f2a0b3cb7c436fc6fc31671f6 | the same revision | channel git-revision and branch head | moved from 31ecbaca; version evaluation and baseline Nix/native byte comparison passed | 2026-11-11 |

With the moved pins, actionlint 1.7.12 (ShellCheck 0.11.0 on the path) and
zizmor 1.32.0 (`--offline`, default persona, and pedantic at
`--min-severity low`) ran over `.github/` locally on 2026-11-11: no findings
and no new suppression. actionlint's archive matched the release asset
digest and checksums file; zizmor was installed with `--require-hashes`
from the wheel above. The existing 180-minute battery job limit and
60-minute locale step limit are unchanged.
