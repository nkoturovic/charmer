# Offline unittest derivation for the charmer suite (the sandbox gate).
# Runs the complete current suite in a Nix sandbox against this tree.
# The repository flake exposes this exact derivation as
# checks.x86_64-linux.charmer (preferred gate: it evaluates only
# git-tracked files); `nix build --file tests/default.nix` is the alias.
# Default pkgs resolves the locked nixpkgs from the repository flake.lock.
{
  pkgs ?
    import
      (
        let
          lock = builtins.fromJSON (builtins.readFile ../flake.lock);
          node = lock.nodes.nixpkgs.locked;
        in
        fetchTarball {
          url = "https://github.com/${node.owner}/${node.repo}/archive/${node.rev}.tar.gz";
          sha256 = node.narHash;
        }
      )
      { },
}:

let
  package = pkgs.callPackage ../nix/package.nix { };
  # The commands the package installs: exactly the public launchers
  # (packaging/product.json), never the developer tool.
  launchers = pkgs.lib.sort (a: b: a < b)
    (builtins.fromJSON (builtins.readFile ../packaging/product.json)).launchers;
in
pkgs.runCommand "charmer-tests"
  {
    # git: tools/build.py applies gateway patches with `git apply`.
    # openssh: ssh-keygen signs and verifies the fake releases of the
    # release integration tests (installer, update, rollback, journeys).
    # openssl: the disposable certificate authorities and TLS fakes of the
    # TLS tests (the update's https transport, discovery, the keyed and
    # Retry-After gateway fixtures). Both as their binaries only (getBin),
    # never a dev output.
    nativeBuildInputs = [
      pkgs.python3
      pkgs.gnupg
      pkgs.git
      (pkgs.lib.getBin pkgs.openssh)
      (pkgs.lib.getBin pkgs.openssl)
    ];
    env.PYTHONDONTWRITEBYTECODE = "1";
    # The release integration tests must run here: a missing ssh-keygen
    # fails them instead of skipping them.
    env.CHARMER_TEST_REQUIRE_RELEASE = "1";
    # The repository tree (tests/source-tree.nix): the gateway patches
    # (gateway/patches/, GatewayManifestConsistencyTests) and the package
    # description (pyproject.toml) are in it at their repository paths.
    # Copied, never symlinked: symlinks break Path.resolve() in the tests.
    tree = import ./source-tree.nix { inherit (pkgs) lib; };
    inherit package;
  }
  ''
    export HOME="$TMPDIR/home"
    mkdir -p "$HOME"
    staged="$TMPDIR/staged"
    mkdir -p "$staged"
    cp -r "$tree" "$staged/charmer"
    chmod -R u+w "$staged/charmer"
    export PYTHONPATH="$staged/charmer/src:$staged/charmer/tests"
    # Explicit non-gateway lane: select pure keyed self-tests, never skip a required proof.
    export CHARMER_TEST_KEYED_LANE=unit
    cd "$TMPDIR"
    # The host runner discovers the same suite, arms the parent and child
    # live-resource tripwire and fails the check if either guard records a hit.
    python3 "$staged/charmer/tools/test.py"
    "$package/bin/charmer" --version
    "$package/bin/charmer-proxy" --version
    "$package/bin/charmer" --help | grep -qF "$package/share/charmer/CHEATSHEET.md"
    # The package's documents link only inside share/charmer (nested
    # directories and heading anchors included).
    python3 "$staged/charmer/tests/check_docs_vocabulary.py" --installed "$package/share/charmer"
    installed=$(ls "$package/bin" | sort | tr '\n' ' ')
    test "$installed" = "${pkgs.lib.concatStringsSep " " launchers} " || {
      echo "installed commands: $installed (expected the public launchers)"; exit 1; }
    pycs=$(find "$package/${pkgs.python3.sitePackages}/charmer" -name '*.pyc' | wc -l)
    mods=$(find "$package/${pkgs.python3.sitePackages}/charmer" -name '*.py' | wc -l)
    test "$pycs" -eq "$mods" || { echo "compileall: $pycs pyc for $mods modules"; exit 1; }
    touch $out
  ''
