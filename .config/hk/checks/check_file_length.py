#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Fail on source and test files whose length exceeds `MAX_FILE_LINES`.

Split a file over the cap into focused modules. To exempt a file for now, add it
to the `file-length` step's `exclude` in the project `hk.pkl`.
"""

from __future__ import annotations

import sys
from pathlib import Path

MAX_FILE_LINES = 1500

CHECKED_EXTENSIONS = {".py", ".ts", ".tsx"}


def main(paths: list[str]) -> int:
    """Print each file in `paths` over the cap."""
    failures = 0
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file() or path.suffix not in CHECKED_EXTENSIONS:
            continue
        line_count = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        if line_count > MAX_FILE_LINES:
            failures += 1
            print(
                f"{path.as_posix()}: file has {line_count} lines (max {MAX_FILE_LINES})"
            )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
