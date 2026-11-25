from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills/finance/trade-republic/scripts/trade_republic.py"
FIXTURES = Path(__file__).parent / "fixtures/trade-republic"
ACCOUNTS = {"DEFAULT": "Example Broker"}
ACCOUNT_ID = "00000000-0000-4000-8000-0000000000a0"


def load_module() -> ModuleType:
    # `uv run` puts the script's folder on the path, for its `wealthfolio_mcp` import.
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("trade_republic", SCRIPT)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["trade_republic"] = module
    spec.loader.exec_module(module)
    return module


tr = load_module()


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def rows_by_id(result: object) -> dict[str, dict[str, str]]:
    """Return each import row, with its numbers as text, by the ID's last digits."""
    activities = result.activities  # type: ignore[attr-defined]
    return {
        a.source_id[-3:]: {
            key: tr.fmt(value) if isinstance(value, Decimal) else str(value)
            for key, value in a.import_row(ACCOUNT_ID).items()
        }
        for a in activities
    }


def pick(row: dict[str, str], *columns: str) -> tuple[str, ...]:
    return tuple(row.get(column, "") for column in columns)


# A fake Wealthfolio MCP server: it records each call, over state a test sets up.
TOKEN = "wf-test-token-not-real"  # noqa: S105
SESSION = "00000000-0000-4000-8000-00000000c0de"
TOOLS = frozenset(
    (
        "get_accounts",
        "prepare_account_change",
        "commit_account_change",
        "prepare_activity_import",
        "commit_activity_import",
    )
)


class FakeWealthfolio:
    """The tools the import calls, over the state that a test sets up."""

    def __init__(self) -> None:
        """Start with no accounts, no activities and every tool."""
        self.url = ""
        self.methods: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.accounts: list[dict[str, Any]] = []
        # Stored activities by duplicate key: account and comment, as Wealthfolio
        # hashes the comment into its key.
        self.stored: dict[tuple[str, str], dict[str, Any]] = {}
        self.tools = set(TOOLS)
        # Tool name and the call number that answers HTTP 500.
        self.fail: tuple[str, int] | None = None
        self.drafts: set[str] = set()
        # Comments of rows that pass the check but fail while being written.
        self.write_failures: set[str] = set()
        # Comment and the error Wealthfolio gives the row.
        self.invalid: dict[str, str] = {}
        self.sse = False
        self.redirect = False

    def add_account(self, name: str, currency: str = "EUR", **extra: Any) -> str:  # noqa: ANN401
        """Store an account and return its ID."""
        account_id = f"00000000-0000-4000-8000-0000000000a{len(self.accounts)}"
        self.accounts.append(
            {
                "id": account_id,
                "name": name,
                "accountType": "SECURITIES",
                "currency": currency,
                "isActive": True,
                "isArchived": False,
                "trackingMode": "TRANSACTIONS",
                "isDefault": False,
            }
            | extra
        )
        return account_id

    def tool_calls(self, name: str) -> list[dict[str, Any]]:
        """Return the arguments of each call of a tool."""
        return [arguments for tool, arguments in self.calls if tool == name]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a tool."""
        self.calls.append((name, arguments))
        if name == "get_accounts":
            return {"accounts": self.accounts, "count": len(self.accounts)}
        if name == "prepare_account_change":
            return {"action": "create", "ready": True, "errors": []}
        if name == "commit_account_change":
            fields = {key: arguments[key] for key in ("accountType", "trackingMode")}
            account_id = self.add_account(
                arguments["name"], arguments["currency"], **fields
            )
            return {"committed": True, "account": {"id": account_id}}
        rows = arguments["activities"]
        if name == "prepare_activity_import":
            return {"rows": [self.check(row) for row in rows]}
        return self.commit(rows)

    def check(self, row: dict[str, Any]) -> dict[str, Any]:
        """Validate a row and flag it when Wealthfolio has it."""
        error = self.invalid.get(row["comment"])
        identity = {}
        if row.get("isin"):
            identity = {
                "providerId": "YAHOO",
                "providerSymbol": f"{row['isin'][-4:]}.DE",
                "exchangeMic": "XETR",
                "quoteCcy": "EUR",
            }
        return identity | {
            "lineNumber": row["lineNumber"],
            "isValid": error is None,
            "isDuplicate": (row["accountId"], row["comment"]) in self.stored,
            "errors": [error] if error else [],
            "warnings": [],
        }

    def commit(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Import rows as one run, as `commit_activity_import` does."""
        checked = [self.check(row) for row in rows]
        failed = [row for row in checked if not row["isValid"]]
        if failed:
            return {"financialRows": [], "failed": failed, "summary": {}}
        statuses = []
        for index, row in enumerate(rows):
            key = (row["accountId"], row["comment"])
            if row["comment"] in self.write_failures:
                status = "failed"
            elif key in self.stored:
                status = "duplicate_skipped"
            else:
                self.stored[key] = row
                status = "draft" if row["comment"] in self.drafts else "imported"
            statuses.append({"index": index, "financialStatus": status})
        return {"financialRows": statuses, "importRunId": "run", "summary": {}}


class Handler(BaseHTTPRequestHandler):
    """Speak Streamable HTTP MCP for one fake server."""

    server: Server

    def log_message(self, *_: object) -> None:
        """Keep the test output quiet."""

    def reply(self, status: int, body: dict[str, Any] | None = None) -> None:
        """Send a JSON or event stream body."""
        fake = self.server.fake
        self.send_response(status)
        self.send_header("Mcp-Session-Id", SESSION)
        if body is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        text = json.dumps(body)
        if fake.sse:
            text = f"event: message\ndata: {text}\n\n"
        data = text.encode("utf-8")
        kind = "text/event-stream" if fake.sse else "application/json"
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self) -> None:
        """End the session."""
        self.server.fake.methods.append("DELETE")
        self.reply(200, {})

    def do_POST(self) -> None:
        """Answer one JSON-RPC message."""
        fake = self.server.fake
        length = int(self.headers.get("Content-Length", "0"))
        message = json.loads(self.rfile.read(length))
        if fake.redirect:
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:9/mcp")
            self.end_headers()
            return
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.reply(401, {"jsonrpc": "2.0", "error": {"code": -32000}, "id": None})
            return
        method = message["method"]
        fake.methods.append(method)
        if "id" not in message:
            self.reply(202)
            return
        result = self.result(method, message.get("params", {}))
        if result is None:
            self.reply(500, {})
            return
        self.reply(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})

    def result(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Return a method's result, or `None` for an HTTP 500."""
        fake = self.server.fake
        if method == "initialize":
            return {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "wealthfolio", "version": "3.13.1"},
                "capabilities": {"tools": {}},
            }
        if method == "tools/list":
            return {"tools": [{"name": name} for name in sorted(fake.tools)]}
        name = params["name"]
        if fake.fail == (name, len(fake.tool_calls(name)) + 1):
            fake.calls.append((name, params["arguments"]))
            return None
        if name not in fake.tools:
            text = f"Scope denied: {name}"
            return {"isError": True, "content": [{"type": "text", "text": text}]}
        output = fake.call(name, params["arguments"])
        content = [{"type": "text", "text": json.dumps(output)}]
        return {"content": content, "structuredContent": output}


class Server(ThreadingHTTPServer):
    """An HTTP server that holds its fake Wealthfolio."""

    fake: FakeWealthfolio


@pytest.fixture
def wealthfolio() -> Iterator[FakeWealthfolio]:
    """Run a fake Wealthfolio MCP server on a free local port."""
    server = Server(("127.0.0.1", 0), Handler)
    server.fake = FakeWealthfolio()
    server.fake.url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.fake
    server.shutdown()
    server.server_close()


def write_config(
    data_dir: Path, wealthfolio: FakeWealthfolio, token_command: list[str]
) -> None:
    command = ", ".join(json.dumps(part) for part in token_command)
    (data_dir / "config.local.toml").write_text(
        '[trade_republic.accounts]\nDEFAULT = "Example Broker"\n\n'
        f'[wealthfolio]\nmcp_url = "{wealthfolio.url}"\ntoken_command = [{command}]\n',
        encoding="utf-8",
    )


@pytest.fixture
def data_dir(
    tmp_path: Path, wealthfolio: FakeWealthfolio, monkeypatch: pytest.MonkeyPatch
) -> Path:
    (tmp_path / "inbox/trade-republic").mkdir(parents=True)
    write_config(tmp_path, wealthfolio, [sys.executable, "-c", f"print({TOKEN!r})"])
    monkeypatch.setenv("FINANCE_DIR", str(tmp_path))
    return tmp_path


def test_every_mapped_event_type_becomes_its_wealthfolio_activity() -> None:
    rows = rows_by_id(tr.convert(read("all-types.csv"), ACCOUNTS))
    cols = ("activityType", "symbol", "quantity", "unitPrice", "amount", "fee", "tax")
    assert pick(rows["001"], *cols) == ("DEPOSIT", "", "", "", "1000", "", "")
    assert pick(rows["002"], *cols) == ("DEPOSIT", "", "", "", "500", "", "")
    assert pick(rows["003"], *cols) == (
        "BUY", "IE00EXAMPLE1", "1.2345678901", "101.23", "124.97", "", "",
    )  # fmt: skip
    assert pick(rows["004"], *cols) == (
        "BUY",
        "US00EXAMPLE2",
        "2",
        "180.5",
        "362",
        "1",
        "",
    )
    assert pick(rows["005"], *cols) == (
        "SELL",
        "US00EXAMPLE2",
        "1",
        "210",
        "203.8",
        "1",
        "5.2",
    )
    assert pick(rows["006"], *cols) == (
        "DIVIDEND",
        "US00EXAMPLE2",
        "",
        "",
        "1.02",
        "",
        "0.18",
    )
    assert pick(rows["008"], *cols) == (
        "DIVIDEND",
        "IE00EXAMPLE1",
        "",
        "",
        "2.5",
        "",
        "",
    )
    assert pick(rows["009"], *cols) == ("INTEREST", "", "", "", "3.21", "", "")
    assert pick(rows["010"], *cols) == ("WITHDRAWAL", "", "", "", "12.5", "", "")
    assert pick(rows["011"], *cols) == ("WITHDRAWAL", "", "", "", "100", "", "")
    assert pick(rows["012"], *cols) == ("FEE", "", "", "", "5", "", "")
    assert pick(rows["013"], *cols) == ("TAX", "", "", "", "0.75", "", "")


def test_every_row_carries_account_date_currency_and_source_reference() -> None:
    rows = rows_by_id(tr.convert(read("all-types.csv"), ACCOUNTS))
    cols = ("date", "accountId", "currency", "comment", "instrumentType", "isin")
    assert pick(rows["004"], *cols) == (
        "2026-02-10",
        ACCOUNT_ID,
        "EUR",
        "tr:00000000-0000-4000-8000-000000000004",
        "EQUITY",
        "US00EXAMPLE2",
    )
    assert rows["004"]["lineNumber"] == "5"
    assert all("fxRate" not in row for row in rows.values())


def test_rows_it_cannot_map_are_listed_with_a_reason() -> None:
    result = tr.convert(read("all-types.csv"), ACCOUNTS)
    reasons = {(u.type, u.asset_class, u.reason) for u in result.unmapped}
    assert reasons == {
        ("GIFT", "", "type not mapped"),
        ("SPLIT", "STOCK", "type not mapped"),
        ("BUY", "BOND", "asset class not mapped"),
        ("CARD_TRANSACTION", "", "amount sign does not match the type"),
    }
    assert len(result.activities) + len(result.unmapped) == result.rows


def edit_line(index: int, old: str, new: str) -> str:
    lines = read("all-types.csv").splitlines()
    assert old in lines[index]
    lines[index] = lines[index].replace(old, new)
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("index", "old", "new", "reason"),
    [
        (8, '"2.50","",""', '"2.50","","-2.50"', "tax not below the amount"),
        (9, '"3.21","",""', '"3.21","-0.10",""', "fee on an income row"),
        (10, '"-12.50","",""', '"-12.50","","-1.00"', "tax on a cash row"),
        (12, '"EUR"', '"Example"', "no currency code"),
        (4, '"BUY","STOCK"', '"BUY","CRYPTO"', "asset class not mapped"),
        (4, '"-1.00"', '"1.00"', "positive fee"),
        (5, '"-5.20"', '"5.20"', "positive tax"),
        (4, '"-361.00"', '"-370.00"', "amount is not shares x price"),
        (4, '"-361.00"', '"361.00"', "amount sign does not match the side"),
        (5, '"210.00"', '""', "amount sign does not match the side"),
        (6, '"-0.18"', '"0.18"', "positive tax"),
    ],
)
def test_a_charge_or_value_it_cannot_place_leaves_the_row_unmapped(
    index: int, old: str, new: str, reason: str
) -> None:
    result = tr.convert(edit_line(index, old, new), ACCOUNTS)
    assert reason in {u.reason for u in result.unmapped if u.line == index + 1}
    assert f"{index:03d}" not in rows_by_id(result)


def test_summary_shows_text_shifted_into_a_code_column_only_as_a_shape() -> None:
    text = read("all-types.csv").replace('"GIFT"', '"Example Shop 12"')
    text = text.replace('"CORPORATE_ACTION"', '"Example Person"')
    report = tr.summarize(tr.convert(text, ACCOUNTS), set(), [])
    for secret in ("SHOP", "PERSON"):  # codes are upper-cased before they are kept
        assert secret not in report.upper()
    assert "CASH/a a 9/-: type not mapped" in report
    assert "a a/SPLIT/STOCK: type not mapped" in report


def test_an_account_type_missing_from_the_config_is_not_mapped() -> None:
    result = tr.convert(read("all-types.csv"), {"JUNIOR": "Child"})
    assert not result.activities
    assert {u.reason for u in result.unmapped} == {"account type not in config"}


def test_polish_export_reads_comma_decimals_spaces_and_dotted_dates() -> None:
    result = tr.convert(read("polish.csv"), ACCOUNTS)
    assert (result.delimiter, result.decimal) == (";", ",")
    assert not result.unmapped
    rows = rows_by_id(result)
    assert pick(rows["101"], "date", "activityType", "amount", "currency") == (
        "2026-05-04", "DEPOSIT", "2500", "PLN",
    )  # fmt: skip
    assert pick(rows["104"], "activityType", "amount", "tax") == (
        "INTEREST",
        "10",
        "2.34",
    )
    assert pick(rows["105"], "activityType", "amount") == ("WITHDRAWAL", "1234.56")
    assert pick(rows["103"], "quantity", "unitPrice", "fee") == ("1.25", "400", "")


def test_foreign_currency_trade_keeps_the_account_currency_price() -> None:
    rows = rows_by_id(tr.convert(read("polish.csv"), ACCOUNTS))
    cols = ("symbol", "quantity", "unitPrice", "amount", "currency", "fee", "fxRate")
    assert pick(rows["102"], *cols) == (
        "US00EXAMPLE2", "0.5", "812.4", "410.2", "PLN", "4", "",
    )  # fmt: skip


def test_a_trade_amount_that_holds_the_charges_is_written_as_booked() -> None:
    text = edit_line(4, '"-361.00"', '"-362.00"')
    text = text.replace('"210.00","-1.00","-5.20"', '"203.80","-1.00","-5.20"')
    result = tr.convert(text, ACCOUNTS)
    rows = rows_by_id(result)
    assert (rows["004"]["amount"], rows["005"]["amount"]) == ("362", "203.8")
    report = tr.summarize(result, set(), result.activities)
    assert "trade amounts: without charges 0, with charges 2, no charges 1" in report
    assert "BUY EUR: 485.97" in report  # totals stay without charges


def test_summary_counts_trade_amounts_booked_without_charges() -> None:
    result = tr.convert(read("all-types.csv"), ACCOUNTS)
    report = tr.summarize(result, set(), result.activities)
    assert "trade amounts: without charges 2, with charges 0, no charges 1" in report


def test_lines_are_file_lines_despite_blank_lines_and_line_breaks_in_cells() -> None:
    lines = read("all-types.csv").splitlines()
    lines[1] = lines[1].replace('"Example reference"', '"Example\nreference"')
    lines.insert(2, "")
    result = tr.convert("\n".join(lines), ACCOUNTS)
    gift = next(u for u in result.unmapped if u.type == "GIFT")
    assert gift.line == 17


@pytest.mark.parametrize(
    ("raw", "decimal", "expected"),
    [
        ("-12.50", ".", "-12.50"),
        ("1,234.56", ".", "1234.56"),
        ("-12,50", ",", "-12.50"),
        ("1 234,56", ",", "1234.56"),
        ("1\u00a0234,56", ",", "1234.56"),
        ("1.234,56", ",", "1234.56"),
        ("\u22125,00", ",", "-5.00"),
    ],
)
def test_numbers_parse_in_both_decimal_styles(
    raw: str, decimal: str, expected: str
) -> None:
    assert tr.parse_number(raw, decimal) == Decimal(expected)


@pytest.mark.parametrize(("raw", "decimal"), [("1,5", "."), ("1.5", ","), ("12a", ".")])
def test_a_number_in_the_other_style_is_rejected(raw: str, decimal: str) -> None:
    with pytest.raises(ValueError, match="number"):
        tr.parse_number(raw, decimal)


def test_decimal_detection_ignores_cells_that_fit_both_styles() -> None:
    assert tr.detect_decimal(["1,234", "1.000", "-3,50"]) == ","
    assert tr.detect_decimal(["1,234"]) == "."


def test_decimal_detection_fails_on_mixed_styles() -> None:
    with pytest.raises(tr.ExportError, match="mix"):
        tr.detect_decimal(["1,50", "2.50"])


def test_a_file_without_the_known_columns_names_what_is_missing() -> None:
    with pytest.raises(
        tr.ExportError, match=r"Missing columns: .*Unknown columns: data, a\."
    ):
        tr.convert('"data","Typ"\n"2026-01-01","X"\n', ACCOUNTS)


def test_a_file_that_starts_with_data_shows_its_first_row_only_as_shapes() -> None:
    text = '"Example Person","DE00EXAMPLE0","12,50"\n"x","y","z"\n'
    with pytest.raises(tr.ExportError) as error:
        tr.convert(text, ACCOUNTS)
    report = tr.inspect_export(text)
    for secret in ("Example Person", "EXAMPLE", "12,50"):
        assert secret not in str(error.value)
        assert secret not in report
    assert "- a a (unknown): 1/1 filled" in report


def test_possible_duplicates_flag_a_booking_repeated_under_a_new_id() -> None:
    result = tr.convert(read("all-types.csv"), ACCOUNTS)
    notes = tr.find_duplicates(result.activities, result.activities)
    assert notes == ["DIVIDEND US00EXAMPLE2 on 2026-02-20: same ISIN on lines [7, 8]"]


def test_a_corrected_dividend_with_another_amount_is_a_possible_duplicate() -> None:
    lines = read("all-types.csv").splitlines()
    lines[7] = lines[7].replace('"1.20"', '"1.25"')
    result = tr.convert("\n".join(lines), ACCOUNTS)
    assert tr.find_duplicates(result.activities, result.activities) == [
        "DIVIDEND US00EXAMPLE2 on 2026-02-20: same ISIN on lines [7, 8]"
    ]


def test_a_cancelled_buy_warns_that_its_buy_is_in_this_run() -> None:
    text = read("all-types.csv").replace('"BUY","BOND"', '"BUY_CANCELLED","STOCK"')
    report = tr.summarize(tr.convert(text, ACCOUNTS), set(), [])
    assert "BUY_CANCELLED on lines [17]: the cancelled BUY is in this run." in report


def test_polish_grouping_with_no_break_spaces_parses() -> None:
    for space in (chr(0x00A0), chr(0x202F)):
        assert tr.parse_number(f"-1{space}234,56", ",") == Decimal("-1234.56")


def test_inspect_shows_codes_and_shapes_but_no_personal_text_or_amounts() -> None:
    report = tr.inspect_export(read("all-types.csv"))
    assert "'CARD_TRANSACTION' x2" in report
    assert "'-9.9' x" in report
    for secret in (
        "Example Shop",
        "Example Person",
        "Example reference",
        "124.97",
        "1000",
    ):
        assert secret not in report


def test_inspect_counts_amount_fee_and_tax_signs_per_type() -> None:
    report = tr.inspect_export(read("all-types.csv"))
    amounts = report.split("amount signs per type:\n")[1].split("fee signs")[0]
    fees = report.split("fee signs per type:\n")[1].split("tax signs")[0]
    taxes = report.split("tax signs per type:\n")[1]
    assert "  CARD_TRANSACTION: + x1, - x1" in amounts
    assert "  SPLIT: empty x1" in amounts
    assert fees.splitlines() == [
        "  BUY: - x2",
        "  SAVINGS_PLAN_EXECUTED: 0 x1",
        "  SELL: - x1",
    ]
    assert taxes.splitlines() == ["  DIVIDEND: - x2", "  SELL: - x1"]


def test_inspect_shows_text_shifted_into_a_code_column_only_as_a_shape() -> None:
    text = read("all-types.csv").replace('"CARD_ORDERING_FEE"', '"Example Shop 12"')
    report = tr.inspect_export(text)
    assert "Example Shop" not in report
    assert "'a a 9' x1" in report


def export_file(data_dir: Path, name: str = "all-types.csv") -> Path:
    export = data_dir / "inbox/trade-republic" / name
    export.write_text(read(name), encoding="utf-8")
    return export


def ledger_ids(data_dir: Path) -> list[str]:
    path = data_dir / "out/trade-republic/ledger.json"
    ledger = json.loads(path.read_text(encoding="utf-8"))
    return [str(source_id) for source_id in ledger["imported_ids"]]


def comment(number: int) -> str:
    return f"tr:00000000-0000-4000-8000-{number:012d}"


def test_import_without_commit_previews_and_creates_nothing(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    wealthfolio.add_account("Example Broker")
    assert tr.main(["import", str(export_file(data_dir))]) == 0
    out = capsys.readouterr().out
    assert "Wealthfolio preview: rows 13, invalid 0, already in Wealthfolio 0" in out
    assert "nothing imported: rerun with --commit" in out
    assert [tool for tool, _ in wealthfolio.calls] == [
        "get_accounts",
        "prepare_activity_import",
    ]
    assert not wealthfolio.stored
    assert not (data_dir / "out").exists()
    assert wealthfolio.methods[-1] == "DELETE"


def test_commit_imports_each_row_with_its_transaction_id_as_comment(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    wealthfolio.sse = True
    account_id = wealthfolio.add_account("Example Broker")
    assert tr.main(["import", str(export_file(data_dir)), "--commit"]) == 0
    out = capsys.readouterr().out
    assert "commit: imported 13, draft 0, duplicate_skipped 0, failed 0" in out
    assert {key[0] for key in wealthfolio.stored} == {account_id}
    assert (account_id, comment(4)) in wealthfolio.stored
    assert "  US00EXAMPLE2: PLE2.DE on XETR, quote EUR\n" in out
    # The commit imports the assets the preview resolved.
    assert wealthfolio.stored[account_id, comment(4)]["providerSymbol"] == "PLE2.DE"
    assert len(ledger_ids(data_dir)) == 13
    assert not wealthfolio.tool_calls("commit_account_change")


def test_commit_creates_a_missing_account_in_the_currency_of_its_rows(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    export = export_file(data_dir, "polish.csv")
    assert tr.main(["import", str(export)]) == 0
    out = capsys.readouterr().out
    assert "account for DEFAULT: missing. --commit creates it (SECURITIES, PLN" in out
    assert "not previewed: 5 rows of a missing account" in out
    assert not wealthfolio.accounts
    assert tr.main(["import", str(export), "--commit"]) == 0
    assert "account for DEFAULT: created" in capsys.readouterr().out
    assert wealthfolio.tool_calls("commit_account_change") == [
        {
            "name": "Example Broker",
            "currency": "PLN",
            "accountType": "SECURITIES",
            "trackingMode": "TRANSACTIONS",
        }
    ]
    assert {key[0] for key in wealthfolio.stored} == {wealthfolio.accounts[0]["id"]}
    assert len(wealthfolio.stored) == 5


def test_a_rerun_on_the_same_export_creates_nothing(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    wealthfolio.add_account("Example Broker")
    export = export_file(data_dir)
    assert tr.main(["import", str(export), "--commit"]) == 0
    stored = dict(wealthfolio.stored)
    calls = len(wealthfolio.calls)
    capsys.readouterr()
    assert tr.main(["import", str(export), "--commit"]) == 0
    out = capsys.readouterr().out
    assert "already in the ledger: 13, in this run: 0" in out
    assert "nothing to import: no row is new" in out
    assert len(wealthfolio.calls) == calls
    # --all sends every row again, and Wealthfolio skips the ones it holds.
    assert tr.main(["import", str(export), "--commit", "--all"]) == 0
    out = capsys.readouterr().out
    assert "already in Wealthfolio 13" in out
    assert "commit: imported 0, draft 0, duplicate_skipped 13, failed 0" in out
    assert wealthfolio.stored == stored


def test_rows_wealthfolio_saves_as_drafts_are_listed_for_approval(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    wealthfolio.add_account("Example Broker")
    wealthfolio.drafts = {comment(1)}
    assert tr.main(["import", str(export_file(data_dir)), "--commit"]) == 0
    out = capsys.readouterr().out
    assert "commit: imported 12, draft 1, duplicate_skipped 0, failed 0" in out
    assert "drafts on lines [2]: Wealthfolio could not set their cash amount." in out
    assert len(ledger_ids(data_dir)) == 13


def test_a_row_that_fails_while_written_fails_the_run_and_stays_out_of_the_ledger(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    wealthfolio.add_account("Example Broker")
    wealthfolio.write_failures = {comment(4)}
    assert tr.main(["import", str(export_file(data_dir)), "--commit"]) == 1
    captured = capsys.readouterr()
    assert "commit: imported 12, draft 0, duplicate_skipped 0, failed 1" in captured.out
    assert "1 rows failed while Wealthfolio wrote them." in captured.err
    assert comment(4)[3:] not in ledger_ids(data_dir)
    assert len(ledger_ids(data_dir)) == 12


def test_rows_wealthfolio_finds_invalid_block_the_commit_and_hide_names(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    wealthfolio.add_account("Example Broker")
    wealthfolio.invalid = {comment(4): 'symbol: no quote for "Example Corp" at 180.5'}
    assert tr.main(["import", str(export_file(data_dir)), "--commit"]) == 1
    captured = capsys.readouterr()
    assert 'symbol: no quote for "..." at 9.9: lines [5]' in captured.out
    assert "Example Corp" not in captured.out + captured.err
    assert "Nothing imported: Wealthfolio finds 1 rows invalid." in captured.err
    assert not wealthfolio.tool_calls("commit_activity_import")
    assert not (data_dir / "out").exists()


def test_an_mcp_error_mid_run_reports_what_was_imported_and_what_was_not(
    data_dir: Path,
    wealthfolio: FakeWealthfolio,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tr, "BATCH_SIZE", 5)
    wealthfolio.add_account("Example Broker")
    wealthfolio.fail = ("commit_activity_import", 2)
    export = export_file(data_dir)
    assert tr.main(["import", str(export), "--commit"]) == 1
    captured = capsys.readouterr()
    assert "commit: imported 5, draft 0, duplicate_skipped 0, failed 0" in captured.out
    assert "batch 2 of 3 failed, file lines [7, 8, 9, 10, 11]." in captured.out
    assert "Rows never sent: 3." in captured.out
    assert "HTTP 500" in captured.err
    assert len(wealthfolio.stored) == 5
    assert sorted(ledger_ids(data_dir)) == sorted(c[3:] for _, c in wealthfolio.stored)
    wealthfolio.fail = None
    assert tr.main(["import", str(export), "--commit"]) == 0
    assert "commit: imported 8," in capsys.readouterr().out
    assert len(wealthfolio.stored) == 13


def test_a_failing_token_command_stops_before_any_request(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    leak = f"import sys; sys.stderr.write({TOKEN!r}); sys.exit(3)"
    write_config(data_dir, wealthfolio, [sys.executable, "-c", leak])
    assert tr.main(["import", str(export_file(data_dir))]) == 1
    captured = capsys.readouterr()
    assert "Token command failed with exit code 3" in captured.err
    assert TOKEN not in captured.out + captured.err
    assert not wealthfolio.methods


def test_a_rejected_token_is_reported_without_the_token(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(data_dir, wealthfolio, [sys.executable, "-c", "print('wf-other')"])
    assert tr.main(["import", str(export_file(data_dir))]) == 1
    err = capsys.readouterr().err
    assert "Wealthfolio rejected the token (HTTP 401)" in err
    assert "wf-other" not in err


def test_a_redirect_is_refused_so_the_token_stays_with_the_server(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    wealthfolio.redirect = True
    assert tr.main(["import", str(export_file(data_dir))]) == 1
    assert "HTTP 307: mcp_url redirects" in capsys.readouterr().err


def test_a_token_without_the_commit_scope_names_the_missing_scope(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    wealthfolio.add_account("Example Broker")
    wealthfolio.tools.discard("commit_activity_import")
    export = export_file(data_dir)
    assert tr.main(["import", str(export)]) == 0
    assert tr.main(["import", str(export), "--commit"]) == 1
    assert "Add the scopes activities:write" in capsys.readouterr().err
    assert not wealthfolio.stored


@pytest.mark.usefixtures("data_dir")
def test_check_reports_scopes_and_accounts_without_their_names(
    wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    wealthfolio.add_account("Example Broker")
    assert tr.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "token command: ok\nserver: wealthfolio 3.13.1\n" in out
    assert "  commit_activity_import: yes\n" in out
    assert "  DEFAULT: found\n" in out
    assert "remove accounts:write from the token" in out
    assert "Example Broker" not in out
    assert TOKEN not in out
    wealthfolio.accounts[0]["isArchived"] = True
    wealthfolio.tools -= {"commit_account_change", "prepare_account_change"}
    assert tr.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "  DEFAULT: archived or hidden\n" in out
    assert "  commit_account_change: no, needs accounts:write\n" in out


@pytest.mark.parametrize(
    ("name", "content"),
    [("config.local.toml", "[broken"), ("out/trade-republic/ledger.json", "[]")],
)
def test_a_broken_config_or_ledger_is_an_error_not_a_traceback(
    data_dir: Path, capsys: pytest.CaptureFixture[str], name: str, content: str
) -> None:
    (data_dir / "out/trade-republic").mkdir(parents=True)
    (data_dir / name).write_text(content, encoding="utf-8")
    assert tr.main(["import", str(export_file(data_dir))]) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_summary_holds_counts_and_totals_but_no_personal_text(
    data_dir: Path, wealthfolio: FakeWealthfolio, capsys: pytest.CaptureFixture[str]
) -> None:
    wealthfolio.add_account("Example Broker")
    assert tr.main(["import", str(export_file(data_dir)), "--commit"]) == 0
    out = capsys.readouterr().out
    assert "2026-03: DIVIDEND 1, INTEREST 1, SELL 1, WITHDRAWAL 2" in out
    assert "DEPOSIT EUR: 1500.00" in out
    assert "fee and tax columns of this run:\n  fee EUR: 2.00\n  tax EUR: 5.56\n" in out
    assert "GIFT/-: type not mapped x1, lines [15]" in out
    for secret in (
        "Example Shop",
        "Example Person",
        "Example reference",
        "Example Corp",
        "Example Broker",
        TOKEN,
    ):
        assert secret not in out


def test_missing_config_is_reported(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (data_dir / "config.local.toml").unlink()
    assert tr.main(["import", str(export_file(data_dir))]) == 1
    assert "config.example.toml" in capsys.readouterr().err


def test_a_config_without_the_wealthfolio_table_names_the_missing_key(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (data_dir / "config.local.toml").write_text(
        '[trade_republic.accounts]\nDEFAULT = "Example Broker"\n', encoding="utf-8"
    )
    assert tr.main(["import", str(export_file(data_dir))]) == 1
    assert "needs [wealthfolio] mcp_url" in capsys.readouterr().err


def test_the_working_directory_is_the_data_dir_when_it_holds_a_config(
    data_dir: Path, wealthfolio: FakeWealthfolio, monkeypatch: pytest.MonkeyPatch
) -> None:
    wealthfolio.add_account("Example Broker")
    monkeypatch.delenv("FINANCE_DIR")
    monkeypatch.chdir(data_dir)
    assert tr.main(["import", str(export_file(data_dir)), "--commit"]) == 0
    assert len(ledger_ids(data_dir)) == 13


def run_script(
    *args: str, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["uv", "run", str(SCRIPT), *args],  # noqa: S607
        cwd=cwd,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_uv_run_previews_and_imports_like_a_user_run(
    data_dir: Path, wealthfolio: FakeWealthfolio
) -> None:
    export = export_file(data_dir, "polish.csv")
    env = {"FINANCE_DIR": str(data_dir)}
    dry = run_script("import", str(export), cwd=data_dir, env=env)
    assert dry.returncode == 0, dry.stderr
    assert "decimal separator: ','" in dry.stdout
    assert not wealthfolio.stored
    wet = run_script("import", str(export), "--commit", cwd=data_dir, env=env)
    assert wet.returncode == 0, wet.stderr
    assert len(wealthfolio.stored) == 5
    assert len(ledger_ids(data_dir)) == 5


def test_uv_run_inspect_fails_cleanly_on_a_missing_file(tmp_path: Path) -> None:
    result = run_script("inspect", str(tmp_path / "missing.csv"), cwd=tmp_path, env={})
    assert result.returncode == 1
    assert result.stderr.startswith("error:")


def test_a_stock_perk_becomes_a_bonus_credit_beside_the_buy_it_paid_for() -> None:
    result = tr.convert(read("stock-perk.csv"), ACCOUNTS)
    rows = rows_by_id(result)
    cols = ("activityType", "subtype", "symbol", "amount", "currency")
    assert pick(rows["203"], *cols) == ("CREDIT", "BONUS", "", "40", "PLN")
    assert pick(rows["204"], *cols) == ("BUY", "", "US00EXAMPLE3", "40", "PLN")
    assert pick(rows["201"], "activityType", "amount") == ("DEPOSIT", "1000")
    assert pick(rows["202"], "activityType", "amount") == ("BUY", "301")


def test_a_stock_perk_without_its_fee_free_buy_is_not_mapped() -> None:
    result = tr.convert(read("stock-perk.csv"), ACCOUNTS)
    assert [(u.line, u.type, u.reason) for u in result.unmapped] == [
        (6, "STOCKPERK", "no fee-free BUY of its ISIN and amount")
    ]
    assert len(result.activities) + len(result.unmapped) == result.rows


def test_a_buy_with_a_fee_does_not_pair_with_a_stock_perk() -> None:
    text = read("stock-perk.csv").replace('"-40.00","",""', '"-40.00","-1.00",""')
    result = tr.convert(text, ACCOUNTS)
    assert "203" not in rows_by_id(result)
    assert {u.line for u in result.unmapped} == {4, 6}


def test_duplicates_that_earlier_runs_wrote_are_not_reported_again() -> None:
    result = tr.convert(read("all-types.csv"), ACCOUNTS)
    new = [a for a in result.activities if a.line not in {7, 8}]
    assert tr.find_duplicates(result.activities, new) == []


def test_a_cancelled_buy_after_earlier_runs_may_be_in_wealthfolio_already() -> None:
    text = read("all-types.csv").replace('"BUY","BOND"', '"BUY_CANCELLED","STOCK"')
    report = tr.summarize(tr.convert(text, ACCOUNTS), {"earlier"}, [])
    assert "in this run, or in Wealthfolio if an earlier import holds it" in report


def test_summary_asks_to_confirm_gross_income_when_income_is_taxed() -> None:
    result = tr.convert(read("all-types.csv"), ACCOUNTS)
    report = tr.summarize(result, set(), result.activities)
    assert "taxed income rows: 2." in report
    untaxed = [a for a in result.activities if not a.tax]
    assert "taxed income rows" not in tr.summarize(result, set(), untaxed)


@pytest.mark.parametrize(
    ("raw", "shown"),
    [("STOCKPERK", "STOCKPERK"), ("Example", "a"), ("AB12CD", "a9a"), ("X", "a")],
)
def test_a_code_shows_only_when_it_is_upper_case_letters(raw: str, shown: str) -> None:
    assert tr.safe_code(raw) == shown


def test_inspect_lists_every_type_code() -> None:
    text = read("all-types.csv")
    codes = {row["type"] for row in csv.DictReader(text.splitlines())}
    assert len(codes) > tr.SHAPE_LIMIT
    report = tr.inspect_export(text)
    assert all(f"'{code}' x" in report for code in codes)
    assert "more" not in report


def test_inspect_leaves_out_rows_with_the_wrong_number_of_cells() -> None:
    text = read("all-types.csv") + '"2026-04-08","Example Person"\n'
    report = tr.inspect_export(text)
    assert "rows: 18\n" in report
    assert "rows with the wrong number of cells, left out below: 1" in report
    assert "- date: 17/17 filled" in report


def test_a_shifted_date_in_a_number_column_does_not_break_decimal_detection() -> None:
    shifted = ";".join(["x"] * 8 + ["04.05.2026"] + ["x"] * 15)
    result = tr.convert(read("polish.csv") + shifted + "\n", ACCOUNTS)
    assert result.decimal == ","
    assert [u.reason for u in result.unmapped] == ["wrong number of cells"]
    assert tr.detect_decimal(["12,50", "04.05.2026", "Example"]) == ","


def test_a_ledger_of_the_csv_version_is_refused_not_trusted(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = data_dir / "out/trade-republic/ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"transaction_ids": []}', encoding="utf-8")
    assert tr.main(["import", str(export_file(data_dir))]) == 1
    assert "from the CSV version of this skill" in capsys.readouterr().err


def test_the_example_config_holds_every_key_the_script_reads() -> None:
    path = SCRIPT.parents[1] / "config.example.toml"
    config = tr.load_config(path)
    assert tr.load_accounts(config, path) == {"DEFAULT": "Trade Republic"}
    wealthfolio = tr.load_wealthfolio(config, path)
    assert wealthfolio.url.endswith("/mcp")
    assert wealthfolio.token_command[0] == "pass-cli"
