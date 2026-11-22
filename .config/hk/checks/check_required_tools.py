#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Fail when `mise.toml` lacks a tool, or has one too old, for the shared hk steps.

A template update can add a tool or raise its minimum in `.config/mise/required-tools`
(`hk>=2.11.1`), but it cannot change the project-owned `mise.toml`.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REQUIRED = Path(".config/mise/required-tools")
CONFIG = Path("mise.toml")


# `node@lts` adds the tool from a channel. A scoped npm name keeps its own `@`.
LINE = re.compile(
    r"(?P<name>.+?)(?:\[python=(?P<python>[0-9.]+)\])?"
    r"(?:@(?P<channel>[a-z]+))?(?:>=(?P<minimum>.+))?"
)


def _requirements(
    required: str,
) -> list[tuple[str, str | None, str | None, str | None]]:
    """Return the name, minimum, channel and private Python for each requirement."""
    found = []
    for raw in required.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and (match := LINE.fullmatch(line)):
            found.append(
                (match["name"], match["minimum"], match["channel"], match["python"])
            )
    return found


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def _configured_version(spec: object) -> str | None:
    """Return the version of a tool entry, or None for a list or an alias."""
    value = spec.get("version") if isinstance(spec, dict) else spec
    return value if isinstance(value, str) and re.match(r"v?\d", value) else None


def missing(required: str, config: str) -> list[str]:
    """Return the tools in the `required` list that the `config` TOML lacks."""
    tools = tomllib.loads(config).get("tools", {})
    return [name for name, _, _, _ in _requirements(required) if name not in tools]


def unpinned(config: str) -> list[str]:
    """Return seeded requests, preserving inline options through mise's own writer."""
    found: list[str] = []
    for name, spec in tomllib.loads(config).get("tools", {}).items():
        specs = spec if isinstance(spec, list) else [spec]
        versions = [
            item.get("version") if isinstance(item, dict) else item for item in specs
        ]
        if any(version in ("latest", "lts") for version in versions):
            found.extend(f"{name}@{version}" for version in versions)
    return found


def outdated(required: str, config: str) -> list[tuple[str, str, str]]:
    """Return (name, minimum, configured) for each tool below its minimum version."""
    tools = tomllib.loads(config).get("tools", {})
    found = []
    for name, minimum, _, _ in _requirements(required):
        configured = _configured_version(tools.get(name))
        if minimum and configured and _numbers(configured) < _numbers(minimum):
            found.append((name, minimum, configured))
    return found


def missing_runtimes(required: str, config: str) -> list[tuple[str, str, str]]:
    """Return fixes for private runtimes, preserving every configured tool version."""
    tools = tomllib.loads(config).get("tools", {})
    found = []
    for name, _, _, runtime in _requirements(required):
        if not runtime or name not in tools:
            continue
        specs = tools[name] if isinstance(tools[name], list) else [tools[name]]
        if all(
            isinstance(spec, dict) and spec.get("python") == runtime for spec in specs
        ):
            continue
        for spec in specs:
            version = spec.get("version") if isinstance(spec, dict) else spec
            if isinstance(version, str):
                found.append((name, runtime, version))
    return found


def _request(name: str, version: str | None, runtime: str | None) -> str:
    options = f"[python={runtime}]" if runtime else ""
    return f"{name}{options}" + (f"@{version}" if version else "")


def main(args: list[str]) -> int:
    """Print each tool that `mise.toml` lacks or has too old. `--names` prints fixes."""
    required, config = REQUIRED.read_text(), CONFIG.read_text()
    if args == ["--unpinned"]:
        print("\n".join(unpinned(config)))
        return 0
    tools = missing(required, config)
    old = outdated(required, config)
    runtimes = missing_runtimes(required, config)
    if args == ["--names"]:
        requirements = {
            name: (channel, runtime)
            for name, _, channel, runtime in _requirements(required)
        }
        added = [_request(tool, *requirements[tool]) for tool in tools]
        raised = [
            _request(name, minimum, requirements[name][1]) for name, minimum, _ in old
        ]
        changed = set(tools) | {name for name, _, _ in old}
        kept = [
            _request(name, version, runtime)
            for name, runtime, version in runtimes
            if name not in changed
        ]
        print("\n".join([*added, *raised, *kept]))
        return 0
    for tool in tools:
        print(f"{CONFIG}: {tool} is missing, and the shared steps need it.")
    for name, minimum, configured in old:
        print(f"{CONFIG}: {name} {configured} is below {minimum}, the minimum.")
    for name, runtime, _ in runtimes:
        print(
            f"{CONFIG}: {name} needs private Python {runtime} for its dependency graph."
        )
    if tools or old or runtimes:
        print("Fix: mise run setup")
    return int(bool(tools or old or runtimes))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
