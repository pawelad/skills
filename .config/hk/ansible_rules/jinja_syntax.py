"""Parse external templates inside ansible-lint's existing Python environment."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, ClassVar

from ansible import constants
from ansiblelint.rules import AnsibleLintRule
from jinja2 import Environment, TemplateSyntaxError

if TYPE_CHECKING:
    from ansiblelint.errors import MatchError
    from ansiblelint.file_utils import Lintable


def _template(source: str) -> tuple[Environment, str, int]:
    env = Environment(
        trim_blocks=True,
        autoescape=True,
        extensions=getattr(constants, "DEFAULT_JINJA2_EXTENSIONS", []),
    )
    if not source.startswith("#jinja2:"):
        return env, source, 0
    header, newline, body = source.partition("\n")
    if not newline:
        raise ValueError("Missing newline after #jinja2: overrides")
    overrides = {}
    # Ansible uses comma-separated key:value pairs with Python literals.
    for pair in header.removeprefix("#jinja2:").split(","):
        key, separator, value = pair.partition(":")
        if not separator:
            raise ValueError("Expected key:value in #jinja2: overrides")
        if key.strip() not in {
            "block_start_string",
            "block_end_string",
            "variable_start_string",
            "variable_end_string",
            "comment_start_string",
            "comment_end_string",
            "line_statement_prefix",
            "line_comment_prefix",
            "trim_blocks",
            "lstrip_blocks",
            "newline_sequence",
            "keep_trailing_newline",
        }:
            raise ValueError(f"Invalid #jinja2: override: {key.strip()}")
        overrides[key.strip()] = ast.literal_eval(value.strip())
    return env.overlay(**overrides), body, 1


class JinjaSyntaxRule(AnsibleLintRule):
    """Reject malformed Jinja without rendering values or resolving filters."""

    id = "cubert-jinja"
    description = "External Ansible templates must have valid Jinja syntax."
    severity = "HIGH"
    tags: ClassVar[list[str]] = ["syntax"]
    version_added = "0.1.0"
    version_changed = "0.1.0"

    def matchlines(self, file: Lintable) -> list[MatchError]:
        """Return syntax errors with their original template line numbers."""
        if not file.path.name.endswith(".j2"):
            return []
        offset = 0
        try:
            env, source, offset = _template(file.content)
            env.parse(source)
        except (
            TemplateSyntaxError,
            SyntaxError,
            ValueError,
            TypeError,
            AssertionError,
        ) as error:
            return [
                self.create_matcherror(
                    message=str(error),
                    lineno=(getattr(error, "lineno", None) or 1) + offset,
                    filename=file,
                )
            ]
        return []
