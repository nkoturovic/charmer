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
| actions/checkout | v7.0.1 | 3d3c42e5aac5ba805825da76410c181273ba90b1 | node24 | 2026-10-24 |
| actions/setup-python | v7.1.0 | 6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 | node24 | 2026-10-24 |
| actions/upload-artifact | v7.0.2 | cf430e030ddbb5b0abf93d22962f4752f3646cd9 | node24 | 2026-10-24 |
| actions/download-artifact | v8.0.2 | 9000827ccba6bdab643e8b6fd33ac0654aef8333 | node24 | 2026-10-24 |
| actions/attest-build-provenance | v4.2.2 | 4d101475d8b20a2381f78447822ac1eab6504dd8 | composite | 2026-10-24 |
| cachix/install-nix-action | 31.11.3 | 75c75c31ebece974e48ebc36b4ad125bd97bd97b | composite | 2026-10-24 |

Each tag is a lightweight tag: the commit is the tag's own object
(`gh api repos/<owner>/<repo>/git/refs/tags/<tag>` names a commit), and
each is the repository's latest release (not a draft or prerelease).

## Tools (pinned by version)

| Tool | Version | Upstream identity | Verified |
| --- | --- | --- | --- |
| actionlint | v1.7.12 | github.com/rhysd/actionlint tag v1.7.12 = 914e7df21a07ef503a81201c76d2b11c789d3fca (Go module proxy origin agrees; latest release) | 2026-10-03 |
| zizmor | 1.31.1 | PyPI zizmor 1.31.1, not yanked; the manylinux x86_64 wheel verified against PyPI metadata; `--offline` retained | 2026-10-18 |
| govulncheck | v1.9.0 | golang.org/x/vuln tag v1.9.0 = e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5 (Go module proxy origin; built with Go 1.26.9 and checksum-database verification) | 2026-10-18 |
| Pester | 6.2.0 | PowerShell Gallery Pester 6.2.0 (latest, not a prerelease); github.com/pester/Pester tag 6.2.0 = 98e56d65da1642a7158992b50ded240c42a3ac18 | 2026-10-03 |
| setuptools | 84.2.1 | PyPI setuptools 84.2.1, not yanked; the build backend and CI wheel move together; Python >=3.10 (below the supported 3.11 floor) | 2026-10-24 |
| ShellCheck | 0.11.0 | koalaman/shellcheck v0.11.0, Linux x86_64 archive verified against the GitHub release asset digest | 2026-10-18 |
| Go (CI tools) | gateway/UPSTREAM.json | the gateway's toolchain pin: its go.dev linux-amd64 archive and sha256, verified there; `GOTOOLCHAIN=local` disables automatic toolchain replacement | 2026-10-19 |
| bubblewrap | 0.13.1 | github.com/containers/bubblewrap tag v0.13.1 (latest release, 2026-10-14), source archive verified against the GitHub release asset digest and the release notes' sha256; built unprivileged | 2026-10-19 |

## Tool artifacts (pinned by sha256)

| Tool | Artifact | sha256 | Verified |
| --- | --- | --- | --- |
| zizmor | zizmor-1.31.1-py3-none-manylinux_2_28_x86_64.whl | 58e878eb2dbd2c6183c105e1ac28c14f7dd1eca44a783f1fe2a69647d012c410 | 2026-10-18 |
| setuptools | setuptools-84.2.1-py3-none-any.whl | ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2 | 2026-10-24 |
| ShellCheck | shellcheck-v0.11.0.linux.x86_64.tar.xz | 8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198 | 2026-10-18 |
| bubblewrap | bubblewrap-0.13.1.tar.xz | 0e85c8702248b8dd64144f594147553650cdd0e4c951690345faace79c217c4d | 2026-10-19 |

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
newest release, 0.13.1; the Nix lane holds nixpkgs' 0.12.0 (the locked
nixos-unstable ships no newer one; the development shell refuses older).
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
| ubuntu:22.04 | sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956 | 2026-10-18 |
| ubuntu:24.04 | sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac | 2026-10-18 |
| debian:12 | sha256:2c037a04925515fdd6ea85ea14a682d0e79931f5e9f5d07b6dbfc6ba12f9e858 | 2026-10-18 |
| fedora:44 | sha256:43b29f65a41eb9c35e1cd5323e3bdf3b655c2357a9f4f1ff2f9c2798e5045d80 | 2026-10-03 |

The digests are the multi-platform image indexes on Docker Hub
(`library/<name>`), still the current index of each tag on the day
verified; the runner pulls its own platform's image from the index.

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
revision or version changes, the match needs review again. The list starts
empty: adding a mechanism is not a review of an advisory, and there is no
version-wide or alias-only exemption.

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
same hash-installed zizmor 1.31.1 without `--offline`, so the audits that read
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

`nixpkgs` follows `nixos-unstable`, locked on 2026-10-24 to
`3ec636362e1816bc2f0888fa0fa2c69f1bc9e504` (2026-10-23,
`sha256-rZLrzrC6i+jnTAlvPZHogKDulY7WWDEv9hJeHieO7Zw=`), the newest revision
of the channel. `nix flake update` and the exact-revision metadata agree on
that content hash. It supplies the Nix channel's Python and the development
shell's tools only: the gateway is built from the recipe's pinned Go archive
and vendored modules, and the release bundles carry their own runtime, so
none of these versions reaches a bundle. The table records the selected
recipes in that source, not a claim about a realized closure; the evaluated
closure and Nix/native gateway equality remain release-candidate checks.

| Package | Version in the locked source | Newest upstream | Disposition | Verified |
| --- | --- | --- | --- | --- |
| bubblewrap | 0.12.0 | 0.13.1 | required floor 0.12.0 (GHSA-pxhw-h44j-8pfx, CVE-2026-87766), asserted by `nix/devshell.nix`; newer releases follow the channel | 2026-10-24 |
| python3 | 3.14.8 | 3.14.9 / 3.15.1 | channel hold; the tarfile custom-filter fallback and private-directory cleanup dispositions below apply; bundles ship 3.15.1 separately | 2026-10-24 |
| openssl (default) | 3.5.9 | 3.5.9 LTS / 3.6.5 / 4.0.3 | current on the 3.5 LTS line, selected by the Nix Python's ssl recipe; closes the prior 3.5.8 gap including CVE-2026-84782 | 2026-10-24 |
| git | 2.56.0 | 2.56.0 | current (development shell and tests only) | 2026-10-24 |
| gnupg | 2.4.9 (FreePG) | 2.5.24 | channel hold for development shell and tests, not bundle release trust; upstream 2.4 is EOL, FreePG support beyond that is unconfirmed; re-check at the release candidate | 2026-10-24 |
| cacert | 3.131.1 | 3.131.1 | NSS CA data removes emSign G3, C1 and ECC C3 roots; this is not a guarantee about an operator-selected certificate bundle | 2026-10-24 |

Python 3.14.9's fixes for `CVE-2026-12345` / `PSF-2026-42`
(TemporaryDirectory cleanup race) and `CVE-2026-87910` / `PSF-2026-40`
(tarfile custom-filter link fallback) are not in this lock's Python patch
list. The Nix launcher uses a mode-0700 `mkdtemp` directory for its signature
verification work; racing its cleanup requires write access to that tree.
Its archive paths use the built-in `data` filter after their own member
validation, never a custom filter returning None. The Windows-only
`PSF-2025-2` MIME-file defect is outside the Nix platforms, and the launcher
does not import mimetypes. The later `CVE-2026-114583` / `PSF-2026-45`
`tar`-filter issue is likewise outside those extraction paths. These are
bounded non-reachability dispositions, not backports; re-check the Python
hold and those paths by 2026-12-01 or at the release candidate, whichever
comes first. A newer default Python follows the channel.

The GnuPG recipe applies `source-2.4.9-freepg-1` patches, but their presence
is not a post-EOL support promise. GnuPG is not shipped in release bundles;
installed release verification uses the launcher's SSH-signature verifier.
The OpenSSL 3.5.9 security release and NSS 3.131.1 CA changes are recorded in
[OpenSSL's advisories](https://openssl-library.org/news/vulnerabilities/) and
[the NSS release notes](https://firefox-source-docs.mozilla.org/security/nss/releases/nss_3_131_1.html).
Python's held changes are in the
[3.14.9 changelog](https://docs.python.org/release/3.14.9/whatsnew/changelog.html);
[GnuPG's support page](https://gnupg.org/download/index.html) records the
2.4 end-of-life.

## Runtime and workflow refresh

On 2026-10-24 the action refs API resolved `actions/setup-python` v7.1.0
and `cachix/install-nix-action` 31.11.3 to the commits in the action table.
The latter tag has no `v` prefix. Their exact-commit `action.yml` files
retain the inputs used here: setup-python's `python-version`, no inputs
for install-nix-action. setup-python stays on Node 24 and adds optional
mirror inputs; install-nix-action's definition is unchanged and its script
moves Nix 2.35.3 to 2.35.4, removing compromised eMudhra certificates from
the installer's certificate bundle. The four other action tags still name
the latest non-draft, non-prerelease releases and the same commits.

The setuptools 84.2.1 wheel matched the sha256 in
<https://pypi.org/pypi/setuptools/84.2.1/json> before extraction into a
scratch directory. It is not yanked and still needs Python >=3.10. The
build backend and the battery's hash-required wheel pin move together.

CPython 3.15.1 comes from python-build-standalone 20261021. Each of the
following eight downloads matched both the release API's asset digest
(<https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20261021>)
and the release's
[SHA256SUMS](https://github.com/astral-sh/python-build-standalone/releases/download/20261021/SHA256SUMS).
The install-only hashes also live in `packaging/python-runtimes.json`; the
full-archive hashes and sizes live in the regenerated licence inventory.

### CPython runtime archives

| Archive | sha256 | Verified |
| --- | --- | --- |
| cpython-3.15.1+20261021-aarch64-apple-darwin-install_only_stripped.tar.gz | 1a4eb4d5aa82400de6eeb94e6fc6d870f5fabc97eb1d801f7eeba8072117b11c | 2026-10-24 |
| cpython-3.15.1+20261021-aarch64-apple-darwin-pgo+lto-full.tar.zst | 3ba97f493a5fc72d9cad9f2d83cb5524b4c67e78140e4a6769bb0e0747f06e41 | 2026-10-24 |
| cpython-3.15.1+20261021-x86_64-apple-darwin-install_only_stripped.tar.gz | b7604b6cfcce467baca5bd607c4fc55928be99310b92b4faf2bdb448f488929e | 2026-10-24 |
| cpython-3.15.1+20261021-x86_64-apple-darwin-pgo+lto-full.tar.zst | 4f6eb28cf8222cdddbaba2134905712af45c8c54053cf220a50fa54a7363d212 | 2026-10-24 |
| cpython-3.15.1+20261021-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz | 6efe65c43ae0bdebebeab4607f91a0053dd36d5e51cfd69a791d544e9b122be7 | 2026-10-24 |
| cpython-3.15.1+20261021-aarch64-unknown-linux-gnu-pgo+lto-full.tar.zst | 9e208dab28830673758bdf2af2623756646aa39481cc246360c5465ba7156afb | 2026-10-24 |
| cpython-3.15.1+20261021-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz | 01d901e09ca673c99a6c9bd9ec2cfbd58dbe8813d07514c36cb73cfd20b18bd8 | 2026-10-24 |
| cpython-3.15.1+20261021-x86_64-unknown-linux-gnu-pgo+lto-full.tar.zst | ed8c818ad80faeb7cb3a37f5d2283bd5f8350350b007bdda092026b9048ccb33 | 2026-10-24 |

The release's `pythonbuild/downloads.json` has sha256
`510d5b18ac3902627cab1c5e0c0a77b2e0e18b6f298df0610de3fcb8a2406db4`.
The full archives' `PYTHON.json` records GIL enabled on all targets, JIT
built but off by default except Intel macOS (not built). All four prepared
runtimes passed the existing archive-member, startup-file and shipped-library
checks: no retained `.pth`, `.start`, `sitecustomize` or `usercustomize`.
Library versions and licence texts are unchanged, including Expat 2.9.0,
OpenSSL 3.5.9 and zstd 1.5.7. These full archives now carry the zstd licence
text themselves, byte-identical to the formerly supplemental text; its
inventory provenance changes accordingly. CPython 3.15.1's ensurepip update
is pruned and its Windows mimetypes
fix does not affect the shipped Linux/macOS targets. The standalone release
also repairs x86-64 Linux BOLT/perf-trampoline crashes.

OSV was queried on 2026-10-24 for CPython 3.15.1's release-tag commit
`c37e74cb6bcdadcd5f46f289498f6b3298ddee61`, not a PyPI namesake. It reports
`CVE-2026-114583` and `PSF-2026-45`: the `tar` extraction filter is affected,
whereas the runtime preparer and signed updater use `data` after their own
member checks. Exact id/alias/revision/version reviews expire 2026-12-01;
this is a non-reachability disposition, not a fix claim. The still-published
CPython 3.15.0 commit `d220409a13b77de7a6c30abe16ea90db01528edc` was queried
too; its two tar-filter findings and Windows-only `PSF-2025-2` retain their
exact reviews, extended from 2026-11-23 to 2026-12-01. The MIME finding no
longer matches 3.15.1 and receives no exemption for that version.

The pin watch now reads and reports both npm `latest` (the user-install
channel) and `stable`. Either newer tag is included in the issue report.
It also fails when the pin is older than `latest` and `latest` differs
from `stable`; the issue step still runs after that failed comparison.
A pin at or ahead of `latest` does not fail merely because `stable` lags.
Both lookups remain mandatory: missing or malformed data never passes as
a quiet week. The watch never changes a pin or installs the client.
