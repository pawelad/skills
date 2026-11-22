#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Fail on comment blocks and docstrings whose prose runs past `MAX_PROSE_LINES`.

Prose is every non-blank line before the first Google docstring section
(`Args:`, `Returns:`, ...) or JSDoc `@tag`; linter directives and fenced code
blocks don't count.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

MAX_PROSE_LINES = 8

SECTION_HEADER = re.compile(
    r"^\s*(Args|Arguments|Attributes|Example|Examples|Raises|Returns|See Also|Yields)"
    r"\s*:\s*$"
)
JSDOC_TAG = re.compile(
    r"^\s*@(default|deprecated|example|param|returns?|see|template|throws|type)\b"
)
DIRECTIVE = re.compile(
    r"^\s*(biome-ignore|eslint-disable|eslint-enable|@ts-expect-error|@ts-ignore"
    r"|noqa|type: ignore|pragma|ruff:|mypy:|fmt:)"
)
PY_EXTENSIONS = {".py"}
TS_EXTENSIONS = {".ts", ".tsx", ".js", ".mjs"}


def _prose_length(lines: list[str], *, stop_at: re.Pattern[str]) -> int:
    count = 0
    in_fence = False
    for line in lines:
        if stop_at.match(line):
            break
        if line.strip().startswith("```"):
            in_fence = not in_fence
        elif line.strip() and not in_fence and not DIRECTIVE.match(line):
            count += 1
    return count


def _python_blocks(source: str) -> list[tuple[int, int]]:
    """Return `(line, prose_length)` for every docstring and `#` comment block."""
    blocks: list[tuple[int, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        docstring = node.body[0] if node.body else None
        if (
            isinstance(docstring, ast.Expr)
            and isinstance(docstring.value, ast.Constant)
            and isinstance(docstring.value.value, str)
        ):
            body = docstring.value.value.strip().splitlines()
            blocks.append(
                (docstring.lineno, _prose_length(body, stop_at=SECTION_HEADER))
            )

    lines = source.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].lstrip().startswith("#") and not lines[index].startswith("#!"):
            end = index
            while end < len(lines) and lines[end].lstrip().startswith("#"):
                end += 1
            body = [line.lstrip().lstrip("#:").strip() for line in lines[index:end]]
            blocks.append((index + 1, _prose_length(body, stop_at=SECTION_HEADER)))
            index = end
        else:
            index += 1
    return blocks


def _ts_blocks(source: str) -> list[tuple[int, int]]:
    """Return `(line, prose_length)` for every `/* */` block and `//` run."""
    blocks: list[tuple[int, int]] = []
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip().removeprefix("{")
        if stripped.startswith("/*"):
            end = index
            while end < len(lines) and "*/" not in lines[end]:
                end += 1
            body = [
                re.sub(r"^\s*(/\*+|\*+/?)\s?", "", line).rstrip("*/")
                for line in lines[index : end + 1]
            ]
            blocks.append((index + 1, _prose_length(body, stop_at=JSDOC_TAG)))
            index = end + 1
        elif stripped.startswith("//"):
            end = index
            while end < len(lines) and lines[end].strip().startswith("//"):
                end += 1
            body = [line.strip().lstrip("/").strip() for line in lines[index:end]]
            blocks.append((index + 1, _prose_length(body, stop_at=JSDOC_TAG)))
            index = end
        else:
            index += 1
    return blocks


def main(paths: list[str]) -> int:
    """Print each comment block over the cap in `paths`."""
    failures = 0
    for raw_path in paths:
        path = Path(raw_path)
        if path.suffix in PY_EXTENSIONS:
            blocks = _python_blocks(path.read_text())
        elif path.suffix in TS_EXTENSIONS:
            blocks = _ts_blocks(path.read_text())
        else:
            continue
        for line, length in blocks:
            if length > MAX_PROSE_LINES:
                failures += 1
                print(
                    f"{path}:{line}: comment has {length} prose lines"
                    f" (max {MAX_PROSE_LINES})"
                )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
