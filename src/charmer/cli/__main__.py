"""``python -m charmer.cli``: the same ``main`` as the entry scripts."""

from charmer.cli.entry import main


if __name__ == "__main__":
    raise SystemExit(main())
