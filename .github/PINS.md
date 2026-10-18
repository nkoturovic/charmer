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
stays on the supported Go 1.26 line; setuptools follows `pyproject.toml`.
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
| actions/checkout | v7.0.1 | 3d3c42e5aac5ba805825da76410c181273ba90b1 | node24 | 2026-10-03 |
| actions/setup-python | v7.0.0 | 5fda3b95a4ea91299a34e894583c3862153e4b97 | node24 | 2026-10-03 |
| actions/upload-artifact | v7.0.2 | cf430e030ddbb5b0abf93d22962f4752f3646cd9 | node24 | 2026-10-18 |
| actions/download-artifact | v8.0.2 | 9000827ccba6bdab643e8b6fd33ac0654aef8333 | node24 | 2026-10-18 |
| actions/attest-build-provenance | v4.2.2 | 4d101475d8b20a2381f78447822ac1eab6504dd8 | composite | 2026-10-03 |
| cachix/install-nix-action | v31.11.2 | 448cde739abec1eaf0646249e3d25bb8d9bc7702 | composite | 2026-10-18 |

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
| setuptools | 84.0.0 | PyPI setuptools 84.0.0, not yanked; the build backend and CI wheel move together; Python >=3.10 (below the supported 3.11 floor) | 2026-10-18 |
| ShellCheck | 0.11.0 | koalaman/shellcheck v0.11.0, Linux x86_64 archive verified against the GitHub release asset digest | 2026-10-18 |
| Go (CI tools) | 1.26.9 | go.dev download metadata, Linux amd64 archive; `GOTOOLCHAIN=local` disables automatic toolchain replacement | 2026-10-18 |

## Tool artifacts (pinned by sha256)

| Tool | Artifact | sha256 | Verified |
| --- | --- | --- | --- |
| zizmor | zizmor-1.31.1-py3-none-manylinux_2_28_x86_64.whl | 58e878eb2dbd2c6183c105e1ac28c14f7dd1eca44a783f1fe2a69647d012c410 | 2026-10-18 |
| setuptools | setuptools-84.0.0-py3-none-any.whl | 51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670 | 2026-10-18 |
| ShellCheck | shellcheck-v0.11.0.linux.x86_64.tar.xz | 8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198 | 2026-10-18 |
| Go (CI tools) | go1.26.9.linux-amd64.tar.gz | 42d158b4d8f7b61ac0a830567c940a86098fb7aac52e467a5ebec03ef5cc2f8d | 2026-10-18 |

`ci_tools.sh` verifies these two archives before extracting them into fresh
runner-temporary directories. The Go module installs retain checksum-database
verification; neither compiler selection nor ShellCheck depends on PATH defaults.

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
