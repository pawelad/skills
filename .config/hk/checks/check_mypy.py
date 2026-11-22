#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Run mypy on plain files in the project env, and on each PEP 723 script in its own.

A script's env holds only its inline dependencies, so an import that the inline block
misses fails here, and not for the people who run the script.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# The reference regex from PEP 723.
BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)

# The oldest Python that `requires-python` allows, such as 3.11 in ">=3.11, <4".
MINIMUM = re.compile(r">=\s*(\d+\.\d+)")

# This script runs in its own uv env, and child uv commands must not inherit it.
BASE_ENV = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}


def script_metadata(path: Path) -> dict[str, object] | None:
    """Return the PEP 723 `script` block of a Python file, or None when it has none."""
    if path.suffix != ".py":
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for match in BLOCK.finditer(text):
        if match["type"] == "script":
            lines = match["content"].splitlines(keepends=True)
            body = "".join(
                line[2:] if line.startswith("# ") else line[1:] for line in lines
            )
            try:
                return tomllib.loads(body)
            except tomllib.TOMLDecodeError:
                # uv reports the bad block, so the script still goes to its own env.
                return {}
    return None


def minimum_python(metadata: dict[str, object]) -> str | None:
    """Return the oldest Python that the script allows, such as "3.11"."""
    match = MINIMUM.search(str(metadata.get("requires-python", "")))
    return match[1] if match else None


@functools.cache
def mypy_minimum() -> str | None:
    """Return the oldest `--python-version` that the project's mypy accepts."""
    code = "from mypy.defaults import PYTHON3_VERSION_MIN as v; print(*v, sep='.')"
    found = subprocess.run(  # noqa: S603
        ["uv", "run", "python", "-c", code],  # noqa: S607
        capture_output=True,
        text=True,
        env=BASE_ENV,
        check=False,
    )
    if found.returncode != 0:
        return None
    return found.stdout.strip() or None


def target_python(metadata: dict[str, object]) -> str | None:
    """Return the script's oldest Python, raised to mypy's floor (mypy 2.x: 3.10)."""
    version = minimum_python(metadata)
    floor = mypy_minimum() if version else None
    if version and floor and _numbers(version) < _numbers(floor):
        return floor
    return version


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def script_python(path: Path) -> str | None:
    """Sync the script's env and return its interpreter, or None when uv fails."""
    command = ["uv", "sync", "--quiet", "--script", str(path)]
    # A check must not rewrite a lock, and `UV_LOCKED` (ci:check) fails with no lock.
    locked = path.with_name(f"{path.name}.lock").exists()
    env = {k: v for k, v in BASE_ENV.items() if k not in {"UV_LOCKED", "UV_FROZEN"}}
    sync = subprocess.run(  # noqa: S603
        [*command, "--locked"] if locked else command, env=env, check=False
    )
    if sync.returncode != 0:
        fix = f" Run `uv lock --script {path}`." if locked else ""
        print(f"{path}: `uv sync --script` failed, see above.{fix}", file=sys.stderr)
        return None
    found = subprocess.run(  # noqa: S603
        ["uv", "python", "find", "--script", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        env=BASE_ENV,
        check=False,
    )
    if found.returncode != 0:
        print(f"{path}: {found.stderr.strip()}", file=sys.stderr)
        return None
    return found.stdout.strip()


def mypy(*args: str) -> int:
    """Run the project's mypy, so every file shares its `[tool.mypy]` settings."""
    command = ["uv", "run", "mypy", *args]
    return subprocess.run(command, env=BASE_ENV, check=False).returncode  # noqa: S603


def main(argv: list[str]) -> int:
    """Check `argv` files and return 1 when any mypy run fails."""
    files = [Path(arg) for arg in argv]
    scripts = {path: script_metadata(path) for path in files}
    plain = [str(path) for path, metadata in scripts.items() if metadata is None]
    failed = bool(plain) and mypy(*plain) != 0
    for path, metadata in scripts.items():
        if metadata is None:
            continue
        python = script_python(path)
        if python is None:
            failed = True
            continue
        # Users can run the script on its oldest Python, so mypy parses for that one.
        version = target_python(metadata)
        target = ["--python-version", version] if version else []
        if mypy("--python-executable", python, *target, str(path)) != 0:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
