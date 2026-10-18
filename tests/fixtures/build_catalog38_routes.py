#!/usr/bin/env python3
"""Recipe of ``catalog38-routes.json``: the route of every gateway selector
of catalog 38, the last catalog before the rename, as that catalog defined it.

Run from the repository root (needs git and the source commit)::

    python3 tests/fixtures/build_catalog38_routes.py

The source is the catalog-38 models file at ``SOURCE`` (the last commit
before the rename branch), read with ``git show``; nothing comes from
today's catalog. Each entry is keyed by the selector's rest after the
earlier prefix (``charmer.legacy.LEGACY_SELECTOR_PREFIX``), ``[1m]``
included, and records the line key, provider, wire model, effort contract
and both windows. The output is checked in; ``tests/test_catalog.py``
compares catalog 39's renamed selectors with it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from charmer import catalog, legacy  # noqa: E402

SOURCE = "ba3c59b4a1ed5d3ec54950c86f24fe91bb71a462"
OUT = Path(__file__).resolve().parent / "catalog38-routes.json"


def main() -> int:
    raw = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{SOURCE}:{(legacy.LEGACY_RESOURCES / 'catalog' / 'models.json').as_posix()}"],
        check=True, capture_output=True, text=True).stdout
    version = json.loads(subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{SOURCE}:{(legacy.LEGACY_RESOURCES / 'version.json').as_posix()}"],
        check=True, capture_output=True, text=True).stdout)["catalog_version"]
    models = json.loads(raw)["models"]
    routes = {}
    for key in sorted(models):
        line = models[key]
        for _level, selector, contract in catalog.line_selectors(line):
            if not selector.startswith(legacy.LEGACY_SELECTOR_PREFIX):
                continue
            routes[selector[len(legacy.LEGACY_SELECTOR_PREFIX):]] = {
                "key": key, "provider": line["provider"], "wire_model": line["wire_model"],
                "proxy_contract": contract, "client_tokens": line["context"]["client_tokens"],
                "provider_tokens": line["context"]["provider_tokens"],
            }
    document = {"source": SOURCE, "catalog_version": version, "routes": routes}
    OUT.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(routes)} selectors of catalog {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
