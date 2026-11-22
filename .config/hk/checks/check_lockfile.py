#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Check native dependency graphs even when mise tools are already installed."""

from __future__ import annotations

import hashlib
import sys
import tomllib
from pathlib import Path

GRAPHS = {
    "npm": ("aube", "aube-lock.yaml", "package.json"),
    "pypi": ("uv", "uv.lock", "pyproject.toml"),
    "pipx": ("uv", "uv.lock", "pyproject.toml"),
}
NATIVE_GRAPH_FORMAT = 2


def failures(path: Path) -> list[str]:
    """Return missing or changed native graphs referenced by a mise lockfile."""
    try:
        lock = tomllib.loads(path.read_text())
    except (OSError, ValueError) as error:
        return [f"{path}: cannot read lockfile: {error}"]
    # Older formats did not require native graphs for npm and Python tools.
    if lock.get("lockfile_version", 0) < NATIVE_GRAPH_FORMAT:
        return []
    found = []
    for name, entries in lock.get("tools", {}).items():
        for entry in entries:
            backend = entry.get("backend", "").partition(":")[0]
            if backend not in GRAPHS:
                continue
            key, native, manifest = GRAPHS[backend]
            graph = entry.get(key, {})
            if not graph.get("path") or not graph.get("digest"):
                found.append(
                    f"{path}: {name}@{entry['version']} has no {key} dependency graph"
                )
                continue
            directory = path.parent / graph["path"]
            try:
                content = (directory / native).read_bytes().replace(b"\r\n", b"\n")
                if not (directory / manifest).is_file():
                    found.append(f"{directory / manifest}: missing dependency manifest")
                digest = "sha256:" + hashlib.sha256(content).hexdigest()
                if digest != graph["digest"]:
                    found.append(
                        f"{directory / native}: dependency graph digest does not match"
                    )
            except OSError as error:
                found.append(
                    f"{directory / native}: cannot read dependency graph: {error}"
                )
    return found


def main() -> int:
    """Print graph failures and the command that regenerates them."""
    found = failures(Path("mise.lock"))
    for failure in found:
        print(failure)
    if found:
        print(
            "Fix: run mise lock with a working Python on PATH,"
            " then commit mise.lock and .mise/locks/."
        )
    return int(bool(found))


if __name__ == "__main__":
    sys.exit(main())
