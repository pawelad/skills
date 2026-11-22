#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Fail on IBAN-like or card-number-like strings in test files.

Fixtures are hand-written. A string of this shape suggests a real export leaked in.
Replace it with an empty cell or an obviously fake value, such as `US00EXAMPLE2`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Two letters, two check digits, then 11 to 30 letters or digits, written run
# together or in groups of four. The shortest IBAN (Norway) has 15 characters.
IBAN = re.compile(
    r"\b[A-Z]{2}\d{2}(?:[A-Z0-9]{11,30}|(?: [A-Z0-9]{4}){2,7}(?: [A-Z0-9]{1,3})?)\b"
)
# 13 to 19 digits in a row, or four groups of four split by spaces or hyphens.
CARD = re.compile(r"(?<![\d-])(?:\d{13,19}|\d{4}(?:[ -]\d{4}){3})(?![\d-])")


def main(paths: list[str]) -> int:
    """Print each match in `paths` and return 1 when there is one."""
    failures = 0
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            for kind, pattern in (("IBAN", IBAN), ("card number", CARD)):
                if pattern.search(line):
                    failures += 1
                    where = f"{path.as_posix()}:{number}"
                    print(f"{where}: {kind}-like string. Use a fake value.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
