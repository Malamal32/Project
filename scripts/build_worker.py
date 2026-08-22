"""Stage the Worker bundle in `worker/build/`.

Python Workers are bundled from the directory holding `main`, and their
dependency set comes from that directory's `pyproject.toml`. Neither can point
back at the repo root here, because the root dependency list includes packages
that cannot run on Pyodide at all — MarkItDown (native), boto3, pypdf. So the
Worker gets its own trimmed `pyproject.toml`, and this script copies the shared
source into place beside it.

The copy is the price of not forking the service. `service/` and `models/` are
the same files the local process imports; nothing here edits them, and a stale
build is impossible because the directory is wiped on every run.

    uv run python -m scripts.build_worker

Output is `worker/build/`, which is gitignored — it is a build artifact, like
`data/d1_sync/`.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "worker" / "build"

# Copied wholesale. `models/` comes along because `service/` imports the ORM
# record types from it; only the Pydantic halves are exercised on the edge, but
# splitting the module to prove that would be a refactor with no payoff.
PACKAGES = ("service", "models")

# Everything the browser loads. Served by Workers Static Assets, not by Python.
FRONTEND = "frontend"

EXCLUDE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def main() -> int:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    for package in PACKAGES:
        source = ROOT / package
        if not source.is_dir():
            print(f"missing source package: {source}", file=sys.stderr)
            return 1
        shutil.copytree(source, BUILD / package, ignore=EXCLUDE)

    shutil.copy2(ROOT / "worker" / "entry.py", BUILD / "entry.py")

    assets = BUILD / "assets"
    shutil.copytree(ROOT / FRONTEND, assets, ignore=EXCLUDE)

    total = sum(1 for _ in BUILD.rglob("*") if _.is_file())
    print(f"staged {total} files into {BUILD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
