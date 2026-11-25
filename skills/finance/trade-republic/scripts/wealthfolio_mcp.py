"""A client for Wealthfolio's MCP server: config, token and JSON-RPC over HTTP.

Finance skills import it from their own `scripts/` folder. It never prints the
token, and it hides quoted text and digits in the server's messages.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from email.message import Message
    from pathlib import Path

# The session protocol. Wealthfolio 3.13.1 also takes the sessionless 2026-07-28 one.
PROTOCOL_VERSION = "2025-03-26"
# The version whose source gave the tool names and input schemas.
TESTED_VERSION = "3.13.1"
# Seconds. An import looks up new assets at market data providers.
MCP_TIMEOUT = 300
TOKEN_TIMEOUT = 60


class McpError(Exception):
    """Wealthfolio's MCP server cannot be used: config, token, refusal or no answer."""


@dataclass(frozen=True)
class Wealthfolio:
    """Where the MCP server is, and the command that prints its token."""

    url: str
    token_command: list[str]


def load_wealthfolio(config: dict[str, Any], path: Path) -> Wealthfolio:
    """Return the `[wealthfolio]` table of the config."""
    table = config.get("wealthfolio", {})
    url, command = table.get("mcp_url"), table.get("token_command")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise McpError(
            f"{path} needs [wealthfolio] mcp_url, such as"
            " https://wealthfolio.example.com/mcp."
        )
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise McpError(
            f"{path} needs [wealthfolio] token_command, a list such as"
            ' ["pass-cli", "item", "view", ...].'
        )
    return Wealthfolio(url, command)


def read_token(command: Sequence[str]) -> str:
    """Run the token command and return what it prints.

    Neither the token nor the command's error output is shown: either may hold it.
    """
    try:
        done = subprocess.run(  # noqa: S603
            list(command),
            capture_output=True,
            text=True,
            timeout=TOKEN_TIMEOUT,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        raise McpError(f"Token command not found: {command[0]}.") from None
    except subprocess.TimeoutExpired:
        raise McpError(
            f"Token command gave no answer in {TOKEN_TIMEOUT} s. It must not ask"
            " for input."
        ) from None
    if done.returncode:
        raise McpError(
            f"Token command failed with exit code {done.returncode}. If it is"
            " pass-cli, log in again. Ask the user to run it outside Pi: it prints"
            " the token."
        )
    token = done.stdout.strip()
    if not token or any(char.isspace() for char in token):
        raise McpError("Token command printed no token, or more than one line.")
    return token


def scrub(text: object) -> str:
    """Hide quoted text and digits in a server message: they can be names or amounts."""
    hidden = re.sub(r"\"[^\"]*\"|'[^']*'", '"..."', str(text))
    return re.sub(r"\d+", "9", hidden)[:200]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a redirect would send the token to another URL."""

    def redirect_request(self, *_: object, **__: object) -> None:
        """Return no new request, so the redirect fails as an HTTP error."""
        return


def http_reason(code: int) -> str:
    """Say what an HTTP error from the MCP server means."""
    if code == 401:  # noqa: PLR2004
        return (
            "Wealthfolio rejected the token (HTTP 401). The token may be revoked or"
            " expired, or token_command prints another secret."
        )
    if code == 404:  # noqa: PLR2004
        return (
            "HTTP 404: mcp_url must be the server's /mcp path. If calls worked"
            " earlier in the run, the server dropped the session: rerun."
        )
    if 300 <= code < 400:  # noqa: PLR2004
        return f"HTTP {code}: mcp_url redirects. Set it to the final URL."
    return f"HTTP {code} from Wealthfolio's MCP server."


def sse_messages(text: str) -> list[str]:
    """Return the data of each event in a server-sent event stream."""
    messages = []
    for event in re.split(r"\r?\n\r?\n", text):
        data = [
            line[5:].removeprefix(" ")
            for line in event.splitlines()
            if line.startswith("data:")
        ]
        if data:
            messages.append("\n".join(data))
    return messages


def read_answer(headers: Message, body: bytes, request_id: int) -> dict[str, Any]:
    """Return the JSON-RPC answer to a request, from a JSON or an event stream body."""
    text = body.decode("utf-8")
    if headers.get_content_type() == "text/event-stream":
        candidates = sse_messages(text)
    else:
        candidates = [text] if text.strip() else []
    for candidate in candidates:
        try:
            message = json.loads(candidate)
        except json.JSONDecodeError:
            raise McpError("Wealthfolio's MCP server sent unreadable JSON.") from None
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise McpError("Wealthfolio's MCP server sent no answer to the request.")


class Mcp:
    """A JSON-RPC client for Wealthfolio's Streamable HTTP MCP endpoint."""

    def __init__(self, url: str, token: str) -> None:
        """Keep the endpoint and the token. `with` opens and closes the session."""
        self.url = url
        self.server = ""
        self.version = ""
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._opener = urllib.request.build_opener(NoRedirect)
        self._last_id = 0

    def __enter__(self) -> Mcp:
        """Open the session."""
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "trade-republic-skill", "version": "1"},
            },
        )
        info = result.get("serverInfo", {})
        self.version = str(info.get("version", ""))
        server = f"{info.get('name', '?')} {info.get('version', '?')}"
        self.server = re.sub(r"[^\w. -]", "", server)[:60]
        self._headers["MCP-Protocol-Version"] = str(
            result.get("protocolVersion", PROTOCOL_VERSION)
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def __exit__(self, *_: object) -> None:
        """End the session. A server that already dropped it is no error."""
        if "Mcp-Session-Id" not in self._headers:
            return
        request = urllib.request.Request(  # noqa: S310
            self.url, headers=self._headers, method="DELETE"
        )
        with suppress(OSError), self._opener.open(request, timeout=MCP_TIMEOUT):
            pass

    def _send(self, message: dict[str, Any]) -> tuple[Message, bytes]:
        # Decimals go out as JSON numbers: the import schema takes numbers.
        data = json.dumps(message, default=float).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310
            self.url, data=data, headers=self._headers, method="POST"
        )
        try:
            with self._opener.open(request, timeout=MCP_TIMEOUT) as response:
                headers, body = response.headers, response.read()
        except urllib.error.HTTPError as error:
            raise McpError(http_reason(error.code)) from None
        except (OSError, urllib.error.URLError) as error:
            reason = getattr(error, "reason", error)
            raise McpError(f"No answer from mcp_url: {scrub(reason)}.") from None
        session = headers.get("Mcp-Session-Id")
        if session:
            self._headers["Mcp-Session-Id"] = session
        return headers, body

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and return its result."""
        self._last_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": self._last_id,
            "method": method,
            "params": params,
        }
        answer = read_answer(*self._send(message), self._last_id)
        if "error" in answer:
            error = answer["error"] if isinstance(answer["error"], dict) else {}
            raise McpError(
                f"MCP error {error.get('code', '?')}: {scrub(error.get('message'))}"
            )
        return answer.get("result") or {}

    def tool_names(self) -> set[str]:
        """Return the tools the token's scopes reach."""
        names: set[str] = set()
        params: dict[str, Any] = {}
        while True:
            result = self.request("tools/list", params)
            names.update(tool.get("name", "") for tool in result.get("tools", []))
            if not result.get("nextCursor"):
                return names
            params = {"cursor": result["nextCursor"]}

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool and return its structured output."""
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        blocks = result.get("content") or [{}]
        text = blocks[0].get("text", "")
        if result.get("isError"):
            raise McpError(f"{name} failed: {scrub(text)}")
        content = result.get("structuredContent")
        if content is None:
            try:
                content = json.loads(text)
            except json.JSONDecodeError:
                raise McpError(f"{name} sent unreadable JSON.") from None
        if not isinstance(content, dict):
            raise McpError(f"{name} sent no JSON object.")
        return content
