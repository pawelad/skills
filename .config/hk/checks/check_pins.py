#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Fail on unpinned tool versions in `mise.toml`.

A mise request must name its locked version. A dotted request can still float:
`node = "24.22"` selects a patch, while jq has real two-component releases.
zizmor checks GitHub Actions pins. hadolint and dclint check Docker images.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

# Syntax alone cannot distinguish a partial request from a two-component release.
PINNED = re.compile(r"v?\d+(\.\d+)+([-+.][0-9A-Za-z.+-]*)?")


def _mise_failures(path: Path) -> list[str]:
    tools = tomllib.loads(path.read_text()).get("tools", {})
    failures: list[str] = []
    lock_path = path.with_name("mise.lock")
    if tools and not lock_path.is_file():
        # Without this, every pinned tool would report a mismatch with no lock to fix.
        return [f"{lock_path}: missing, so no pin can be verified. Fix: mise lock"]
    locked = tomllib.loads(lock_path.read_text()).get("tools", {}) if tools else {}
    for name, spec in tools.items():
        versions = spec if isinstance(spec, list) else [spec]
        for version in versions:
            value = version.get("version") if isinstance(version, dict) else version
            if not isinstance(value, str) or not PINNED.fullmatch(value):
                failures.append(f"{path}: tool {name!r} has no pinned version")
                continue
            resolved = {
                entry["version"].removeprefix("v") for entry in locked.get(name, [])
            }
            request = value.removeprefix("v")
            if request in resolved:
                continue
            # mise lock keeps a partial request, so only an exact pin fixes it.
            patches = sorted(v for v in resolved if v.startswith(f"{request}."))
            key = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else f'"{name}"'
            pins = " or ".join(f'{key} = "{patch}"' for patch in patches)
            fix = f"pin {pins} in {path}" if patches else "mise lock"
            failures.append(
                f"{path}: tool {name!r} request {value!r}"
                f" does not match its locked version. Fix: {fix}"
            )
    return failures


def main(paths: list[str]) -> int:
    """Print each unpinned tool in the `mise.toml` files in `paths`."""
    failures: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.name == "mise.toml":
            failures += _mise_failures(path)
    for failure in failures:
        print(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
