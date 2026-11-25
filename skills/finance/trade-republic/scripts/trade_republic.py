#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Import a Trade Republic transaction export into Wealthfolio through its MCP server.

`inspect` prints the header and the shape of each column's values. `import` prints a
summary and Wealthfolio's preview of the new rows. Only with `--commit` does it create
the account and the activities, and write the run ledger. `check` tests the connection.
No command prints a row, an amount of a single row, a name, a description, an account
number or the token.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wealthfolio_mcp import (
    TESTED_VERSION,
    Mcp,
    McpError,
    load_wealthfolio,
    read_token,
    scrub,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    Skip = Callable[[str], "Unmapped"]
    Mapper = Callable[["Row", dict[str, Any], Skip], "Activity | Unmapped"]

DATA_DIR_ENV = "FINANCE_DIR"
CONFIG_NAME = "config.local.toml"
# Each source keeps its files in its own folder of inbox/, out/ and archive/.
SOURCE_DIR = "trade-republic"
LEDGER_NAME = "ledger.json"
SOURCE_PREFIX = "tr:"

# Both known header revisions. The semicolon one has `value_date`, the comma one
# `mcc_code`. Neither column is read.
REQUIRED_COLUMNS = (
    "date",
    "account_type",
    "category",
    "type",
    "asset_class",
    "symbol",
    "shares",
    "price",
    "amount",
    "fee",
    "tax",
    "currency",
    "transaction_id",
)
KNOWN_COLUMNS = frozenset(
    (
        *REQUIRED_COLUMNS,
        "datetime",
        "name",
        "original_amount",
        "original_currency",
        "fx_rate",
        "description",
        "value_date",
        "counterparty_name",
        "counterparty_iban",
        "payment_reference",
        "mcc_code",
    )
)
NUMBER_COLUMNS = (
    "shares",
    "price",
    "amount",
    "fee",
    "tax",
    "original_amount",
    "fx_rate",
)
# Short codes from a fixed vocabulary: their values may be shown as they are.
CODE_COLUMNS = frozenset(
    ("account_type", "category", "type", "asset_class", "currency", "original_currency")
)
# Personal text: `inspect` shows only how many cells are filled.
PRIVATE_COLUMNS = frozenset(
    (
        "name",
        "description",
        "counterparty_name",
        "counterparty_iban",
        "payment_reference",
    )
)

TRADE_TYPES = {"BUY": "BUY", "SAVINGS_PLAN_EXECUTED": "BUY", "SELL": "SELL"}
# CRYPTO stays out until a real export shows a symbol Wealthfolio can resolve.
INSTRUMENT_TYPES = {"STOCK": "EQUITY", "FUND": "EQUITY"}
DIVIDEND_TYPES = frozenset(("DIVIDEND", "DISTRIBUTION"))
# Free cash that Trade Republic spends at once on a fee-free BUY of one share.
PERK_TYPE = "STOCKPERK"
DIVIDEND_ASSETS = frozenset(("STOCK", "FUND"))
# Cash rows: Wealthfolio type and the sign the export gives the amount.
CASH_TYPES: dict[str, tuple[str, int]] = {
    "INTEREST_PAYMENT": ("INTEREST", 1),
    "CARD_TRANSACTION": ("WITHDRAWAL", -1),
    "CUSTOMER_INBOUND": ("DEPOSIT", 1),
    "CUSTOMER_INPAYMENT": ("DEPOSIT", 1),
    "TRANSFER_IN": ("DEPOSIT", 1),
    "TRANSFER_INBOUND": ("DEPOSIT", 1),
    "TRANSFER_INSTANT_INBOUND": ("DEPOSIT", 1),
    "CUSTOMER_OUTBOUND_REQUEST": ("WITHDRAWAL", -1),
    "TRANSFER_OUT": ("WITHDRAWAL", -1),
    "TRANSFER_INSTANT_OUTBOUND": ("WITHDRAWAL", -1),
    "TRANSFER_DIRECT_DEBIT_INBOUND": ("WITHDRAWAL", -1),
    "CARD_ORDERING_FEE": ("FEE", -1),
    "TAX_OPTIMIZATION": ("TAX", -1),
}
# A cent: the export rounds `price`, so shares x price misses the booked amount.
TRADE_TOLERANCE = Decimal("0.01")
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
# A shifted row, such as one Excel re-quoted, can put text into a code column.
# No digits and no lower case, so an IBAN or a capitalized name shows as a shape.
CODE_RE = re.compile(r"^[A-Z_]{2,40}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
# A file without a header row, such as another bank's export, starts with data.
HEADER_RE = re.compile(r"^[a-z0-9_]{1,40}$")


class ExportError(Exception):
    """The file is not a Trade Republic export this parser can read."""


@dataclass(frozen=True)
class Activity:
    """One Wealthfolio import row, built from one export row."""

    line: int
    source_id: str
    date: date
    account: str
    activity_type: str
    currency: str
    amount: Decimal | None = None
    symbol: str = ""
    instrument_type: str = ""
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    fee: Decimal | None = None
    tax: Decimal | None = None
    # A trade's shares x price as Trade Republic booked it, without charges.
    trade_value: Decimal | None = None
    # Whether the export's trade amount held the charges; `None` without charges.
    booked_with_charges: bool | None = None
    subtype: str = ""
    # The ISIN a stock perk's cash bought; it pairs the perk with its BUY.
    perk_isin: str = ""

    def import_row(self, account_id: str) -> dict[str, Any]:
        """Return the row as a Wealthfolio import row, in its field names.

        The comment holds the Trade Republic ID. Wealthfolio hashes it into the
        row's duplicate key, so a second import of the row is a duplicate.
        """
        row: dict[str, Any] = {
            "date": self.date.isoformat(),
            "activityType": self.activity_type,
            "currency": self.currency,
            "accountId": account_id,
            "comment": SOURCE_PREFIX + self.source_id,
            "lineNumber": self.line,
        }
        if self.symbol:
            row |= {
                "symbol": self.symbol,
                "isin": self.symbol,
                "instrumentType": self.instrument_type,
            }
        numbers = {
            "quantity": self.quantity,
            "unitPrice": self.unit_price,
            "amount": self.amount,
            "fee": self.fee,
            "tax": self.tax,
        }
        row |= {name: value for name, value in numbers.items() if value is not None}
        if self.subtype:
            row["subtype"] = self.subtype
        return row

    def cash(self) -> Decimal:
        """Return the cash total the summary adds up for this row."""
        return (
            self.trade_value
            if self.trade_value is not None
            else self.amount or Decimal(0)
        )


@dataclass(frozen=True)
class Unmapped:
    """An export row left out of the import, with the reason."""

    line: int
    category: str
    type: str
    asset_class: str
    reason: str


@dataclass
class Result:
    """Everything one conversion found."""

    delimiter: str
    decimal: str
    rows: int
    activities: list[Activity] = field(default_factory=list)
    unmapped: list[Unmapped] = field(default_factory=list)


def fmt(value: Decimal | None) -> str:
    """Write a number with a dot decimal and no exponent or trailing zeros."""
    if value is None:
        return ""
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def read_table(text: str) -> tuple[str, list[str], list[list[str]], list[int]]:
    """Split the export into its delimiter, header, rows and each row's file line.

    A blank line or a cell with a line break would shift a count of rows, and the
    user finds a row by its line in a text editor.
    """
    text = text.removeprefix("\ufeff")
    first = text.split("\n", 1)[0]
    delimiter = ";" if first.count(";") > first.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    lines: list[int] = []
    start = 1
    for row in reader:
        if row:
            rows.append(row)
            lines.append(start)
        start = reader.line_num + 1
    if not rows:
        raise ExportError("The file is empty.")
    header = [cell.strip() for cell in rows[0]]
    return delimiter, header, rows[1:], lines[1:]


def check_header(header: Sequence[str]) -> None:
    """Fail when a column the parser reads is missing."""
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        unknown = [column(name) for name in header if name not in KNOWN_COLUMNS]
        raise ExportError(
            f"Missing columns: {', '.join(missing)}. "
            f"Unknown columns: {', '.join(unknown) or 'none'}."
        )


DOT_NUMBER = re.compile(r"^-?(\d+|\d{1,3}(,\d{3})+)(\.\d+)?$")
COMMA_NUMBER = re.compile(r"^-?(\d+|\d{1,3}(\.\d{3})+)(,\d+)?$")
SPACES = re.compile(r"\s")  # str patterns match U+00A0 and U+202F too


def detect_decimal(values: Iterable[str]) -> str:
    """Return the file's decimal separator from its number cells.

    A cell such as `1,234` or `1.234` fits both, so only an unambiguous cell counts.
    """
    seen: set[str] = set()
    for raw in values:
        value = SPACES.sub("", raw).replace("\u2212", "-").lstrip("+-")
        dot, comma = value.rfind("."), value.rfind(",")
        if max(dot, comma) < 0 or not (
            DOT_NUMBER.match(value) or COMMA_NUMBER.match(value)
        ):
            continue
        separator = "." if dot > comma else ","
        decimals = len(value) - max(dot, comma) - 1
        if (dot >= 0 and comma >= 0) or decimals != 3:  # noqa: PLR2004
            seen.add(separator)
    if len(seen) > 1:
        raise ExportError("Number cells mix comma and dot decimals.")
    return seen.pop() if seen else "."


def parse_number(raw: str, decimal: str) -> Decimal | None:
    """Parse a cell in the file's number format. An empty cell is `None`."""
    value = SPACES.sub("", raw).replace("\u2212", "-").removeprefix("+")
    if not value:
        return None
    pattern = DOT_NUMBER if decimal == "." else COMMA_NUMBER
    if not pattern.match(value):
        raise ValueError("number")
    grouping = "," if decimal == "." else "."
    value = value.replace(grouping, "").replace(",", ".")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ValueError("number") from error


def parse_date(raw: str) -> date:
    """Parse `2026-05-01` or the Polish `01.05.2026`."""
    value = raw.strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, pattern).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise ValueError("date")


class Row:
    """One export row, read by column name."""

    def __init__(self, line: int, cells: dict[str, str], decimal: str) -> None:
        """Keep the cells and the file's decimal separator."""
        self.line = line
        self.cells = cells
        self.decimal = decimal

    def text(self, column: str) -> str:
        """Return a cell, trimmed."""
        return self.cells.get(column, "").strip()

    def code(self, column: str) -> str:
        """Return a code cell, upper-cased."""
        return self.text(column).upper()

    def number(self, column: str) -> Decimal | None:
        """Return a number cell, or `None` when it is empty."""
        return parse_number(self.text(column), self.decimal)


def nonzero(value: Decimal | None) -> Decimal | None:
    """Return the absolute value, or `None` for zero."""
    return None if not value else abs(value)


def mapper_for(category: str, kind: str) -> Mapper | None:
    """Return the mapper of an export type, or `None` for a type it does not map."""
    if category == "TRADING" and kind in TRADE_TYPES:
        return map_trade
    if category != "CASH":
        return None
    if kind in DIVIDEND_TYPES:
        return map_dividend
    if kind in CASH_TYPES:
        return map_cash
    return map_perk if kind == PERK_TYPE else None


def map_row(row: Row, accounts: dict[str, str]) -> Activity | Unmapped:
    """Map one export row, or say why it stays out of the import."""
    category, kind = row.code("category"), row.code("type")

    # Raw text, not upper-cased: a capitalized name must not pass as a code.
    def skip(reason: str) -> Unmapped:
        return Unmapped(
            row.line,
            safe_code(row.text("category")),
            safe_code(row.text("type")),
            safe_code(row.text("asset_class")),
            reason,
        )

    account = accounts.get(row.code("account_type"))
    source_id = row.text("transaction_id")
    if not account or not source_id:
        return skip("no transaction_id" if account else "account type not in config")
    mapper = mapper_for(category, kind)
    if mapper is None:
        return skip("type not mapped")
    # The summary prints the currency, so it must be a code, not shifted text.
    currency = row.code("currency")
    if not CURRENCY_RE.match(currency):
        return skip("no currency code")
    try:
        # The export signs charges negative; a positive one is likely a refund.
        for name in ("fee", "tax"):
            charge = row.number(name)
            if charge is not None and charge > 0:
                return skip(f"positive {name}")
        base = {
            "line": row.line,
            "source_id": source_id,
            "date": parse_date(row.text("date")),
            "account": account,
            "currency": currency,
        }
        return mapper(row, base, skip)
    except ValueError as error:
        return skip(f"unreadable {error}")


def map_trade(row: Row, base: dict[str, Any], skip: Skip) -> Activity | Unmapped:
    """Map a purchase, savings plan execution or sale."""
    instrument = INSTRUMENT_TYPES.get(row.code("asset_class"))
    if not instrument:
        return skip("asset class not mapped")
    isin = row.code("symbol")
    if not ISIN_RE.match(isin):
        return skip("no ISIN")
    shares, price = row.number("shares"), row.number("price")
    if not shares or price is None:
        return skip("no shares or price")
    side = TRADE_TYPES[row.code("type")]
    if (shares < 0) != (side == "SELL"):
        return skip("shares sign does not match the side")
    fee, tax = nonzero(row.number("fee")), nonzero(row.number("tax"))
    cash = trade_cash(
        row, side, abs(shares * price), (fee or Decimal(0)) + (tax or Decimal(0)), skip
    )
    if isinstance(cash, Unmapped):
        return cash
    value, final, with_charges = cash
    return Activity(
        **base,
        activity_type=side,
        symbol=isin,
        instrument_type=instrument,
        quantity=abs(shares),
        unit_price=abs(price),
        amount=final,
        fee=fee,
        tax=tax,
        trade_value=value,
        booked_with_charges=with_charges,
    )


def trade_cash(
    row: Row, side: str, value: Decimal, charges: Decimal, skip: Skip
) -> tuple[Decimal, Decimal, bool | None] | Unmapped:
    """Return a trade's booked value, the cash that moved and whether charges were in.

    Wealthfolio keeps an explicit total, while one it derives from the rounded price
    drifts by cents. It wants the cash that moved, charges included.
    """
    booked = row.number("amount")
    if not booked or (booked > 0) != (side == "SELL"):
        return skip("amount sign does not match the side")
    booked = abs(booked)
    # A purchase pays its charges on top, a sale pays them out of the proceeds.
    if side == "SELL":
        charges = -charges
    if abs(booked - value) <= TRADE_TOLERANCE:
        cash = booked, booked + charges, (False if charges else None)
    elif charges and abs(booked - (value + charges)) <= TRADE_TOLERANCE:
        cash = booked - charges, booked, True
    else:
        return skip("amount is not shares x price")
    if cash[1] <= 0:
        return skip("charges not below the amount")
    return cash


def net_income(row: Row, skip: Skip) -> tuple[Decimal, Decimal | None] | Unmapped:
    """Return the cash received and the tax of a dividend or interest row.

    Assumes Trade Republic gives the gross amount with the withholding beside it; no
    real export has confirmed it. Wealthfolio stores the cash received, net of tax.
    """
    gross, tax = row.number("amount"), nonzero(row.number("tax"))
    if gross is None or gross <= 0:
        return skip("no positive amount")
    if nonzero(row.number("fee")):
        return skip("fee on an income row")
    net = gross - (tax or 0)
    if net <= 0:
        return skip("tax not below the amount")
    return net, tax


def map_dividend(row: Row, base: dict[str, Any], skip: Skip) -> Activity | Unmapped:
    """Map a dividend or fund distribution."""
    if row.code("asset_class") not in DIVIDEND_ASSETS:
        return skip("asset class not mapped")
    isin = row.code("symbol")
    if not ISIN_RE.match(isin):
        return skip("no ISIN")
    income = net_income(row, skip)
    if isinstance(income, Unmapped):
        return income
    amount, tax = income
    return Activity(
        **base,
        activity_type="DIVIDEND",
        symbol=isin,
        instrument_type="EQUITY",
        amount=amount,
        tax=tax,
    )


def map_cash(row: Row, base: dict[str, Any], skip: Skip) -> Activity | Unmapped:
    """Map a cash row that has no security."""
    if row.code("asset_class") or row.text("symbol") or row.text("shares"):
        return skip("cash type on a security row")
    activity_type, sign = CASH_TYPES[row.code("type")]
    amount = row.number("amount")
    if amount is None or amount == 0 or (amount > 0) != (sign > 0):
        return skip("amount sign does not match the type")
    if activity_type == "INTEREST":
        income = net_income(row, skip)
        if isinstance(income, Unmapped):
            return income
        net, tax = income
        return Activity(**base, activity_type=activity_type, amount=net, tax=tax)
    if nonzero(row.number("tax")):
        return skip("tax on a cash row")
    return Activity(
        **base,
        activity_type=activity_type,
        amount=abs(amount),
        fee=nonzero(row.number("fee")),
    )


def map_perk(row: Row, base: dict[str, Any], skip: Skip) -> Activity | Unmapped:
    """Map a stock perk to a bonus credit. Wealthfolio counts it as new capital."""
    isin = row.code("symbol")
    if not ISIN_RE.match(isin):
        return skip("no ISIN")
    if row.text("shares") or nonzero(row.number("fee")) or nonzero(row.number("tax")):
        return skip("shares or charges on a perk")
    amount = row.number("amount")
    if amount is None or amount <= 0:
        return skip("no positive amount")
    return Activity(
        **base, activity_type="CREDIT", subtype="BONUS", amount=amount, perk_isin=isin
    )


def pair_perks(result: Result) -> None:
    """Keep a perk only beside the fee-free BUY it paid for: same ISIN and amount.

    The pair is the evidence that the perk row is the share's cash, not the share.
    """
    buys = [
        a
        for a in result.activities
        if a.activity_type == "BUY" and not a.fee and not a.tax
    ]
    kept: list[Activity] = []
    for activity in result.activities:
        buy = next(
            (
                b
                for b in buys
                if b.symbol == activity.perk_isin and b.amount == activity.amount
            ),
            None,
        )
        if activity.perk_isin and buy is None:
            reason = "no fee-free BUY of its ISIN and amount"
            result.unmapped.append(
                Unmapped(activity.line, "CASH", PERK_TYPE, "", reason)
            )
            continue
        if activity.perk_isin and buy is not None:
            buys.remove(buy)
        kept.append(activity)
    result.activities = kept


def convert(text: str, accounts: dict[str, str]) -> Result:
    """Map every row of an export."""
    delimiter, header, rows, lines = read_table(text)
    check_header(header)
    # A shifted row would put a date or a name into a number column.
    numbers = [
        row[header.index(name)]
        for row in rows
        if len(row) == len(header)
        for name in NUMBER_COLUMNS
        if name in header
    ]
    result = Result(delimiter, detect_decimal(numbers), len(rows))
    for line, cells in zip(lines, rows, strict=True):
        if len(cells) != len(header):
            result.unmapped.append(Unmapped(line, "", "", "", "wrong number of cells"))
            continue
        mapped = map_row(
            Row(line, dict(zip(header, cells, strict=True)), result.decimal), accounts
        )
        if isinstance(mapped, Activity):
            result.activities.append(mapped)
        else:
            result.unmapped.append(mapped)
    pair_perks(result)
    return result


def shape(value: str) -> str:
    """Hide a value's content but keep its form: `-12,50` becomes `-9,9`."""
    return re.sub(r"[^\W\d_]+", "a", re.sub(r"\d+", "9", value))


def safe_code(value: str) -> str:
    """Return a code, or its shape when shifted text took its place."""
    return value if CODE_RE.match(value) else shape(value)


def column(name: str) -> str:
    """Return a header name, or its shape when it does not look like one."""
    return name if HEADER_RE.match(name) else shape(name)


def inspect_export(text: str) -> str:
    """Describe the header and the form of each column's values."""
    delimiter, header, all_rows, _ = read_table(text)
    # A row with the wrong cell count is shifted, so its cells sit in other columns.
    rows = [row for row in all_rows if len(row) == len(header)]
    shifted = len(all_rows) - len(rows)
    lines = [
        f"rows: {len(all_rows)}",
        f"rows with the wrong number of cells, left out below: {shifted}",
        f"delimiter: {delimiter!r}",
        f"columns: {len(header)}",
    ]
    for index, name in enumerate(header):
        values = [row[index].strip() for row in rows]
        filled = [value for value in values if value]
        known = "" if name in KNOWN_COLUMNS else " (unknown)"
        lines.append(f"- {column(name)}{known}: {len(filled)}/{len(values)} filled")
        if name in PRIVATE_COLUMNS or not filled:
            continue
        counts = Counter(
            safe_code(value) if name in CODE_COLUMNS else shape(value)
            for value in filled
        )
        # Every code counts, since a rare type is the one a mapping misses.
        limit = None if name in CODE_COLUMNS else SHAPE_LIMIT
        top = ", ".join(
            f"{value!r} x{count}" for value, count in counts.most_common(limit)
        )
        more = len(counts) - (limit or len(counts))
        lines.append(f"    {top}" + (f", and {more} more" if more > 0 else ""))
    lines.extend(charge_signs(header, rows))
    return "\n".join(lines)


SIGN_COLUMNS = ("amount", "fee", "tax")
SHAPE_LIMIT = 12


def sign(value: str) -> str:
    """Return a number cell's sign: `+`, `-`, `0` or `empty`."""
    if not value:
        return "empty"
    if not re.search(r"[1-9]", value):
        return "0"
    return "-" if value[0] in "-\u2212" else "+"


def charge_signs(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """Count the signs of amounts, fees and taxes per type: the mappings rely on them.

    Fees and taxes are mostly empty, so only filled cells count.
    """
    if "type" not in header:
        return []
    kind = header.index("type")
    lines: list[str] = []
    for name in SIGN_COLUMNS:
        if name not in header:
            continue
        index = header.index(name)
        signs: dict[str, Counter[str]] = defaultdict(Counter)
        for row in rows:
            if max(kind, index) >= len(row):
                continue
            value = sign(row[index].strip())
            if name == "amount" or value != "empty":
                signs[safe_code(row[kind].strip())][value] += 1
        lines.append(f"{name} signs per type:")
        for code in sorted(signs):
            counts = ", ".join(f"{s} x{n}" for s, n in sorted(signs[code].items()))
            lines.append(f"  {code}: {counts}")
        if not signs:
            lines.append("  none")
    return lines


def find_duplicates(
    activities: Sequence[Activity], new: Sequence[Activity]
) -> list[str]:
    """List rows of this run that look like another row: same ID, or same booking.

    A group of rows that earlier imports sent was flagged then already.
    """
    in_run = {activity.line for activity in new}
    notes: list[str] = []
    by_id: dict[str, list[int]] = defaultdict(list)
    by_booking: dict[tuple[object, ...], list[int]] = defaultdict(list)
    for activity in activities:
        by_id[activity.source_id].append(activity.line)
        # A corrected dividend can carry another amount than the original.
        cash = None if activity.activity_type == "DIVIDEND" else activity.cash()
        key = (activity.date, activity.activity_type, activity.symbol, cash)
        by_booking[key].append(activity.line)
    for lines in by_id.values():
        if len(lines) > 1 and in_run.intersection(lines):
            notes.append(f"same transaction_id on lines {lines}")
    for (when, kind, symbol, _), lines in by_booking.items():
        if len(lines) > 1 and in_run.intersection(lines):
            same = "same ISIN" if kind == "DIVIDEND" else "same amount"
            notes.append(f"{kind} {symbol or '-'} on {when}: {same} on lines {lines}")
    return notes


def run_totals(new: Sequence[Activity]) -> list[str]:
    """Count the run's rows per month, and add up its cash, fees and taxes."""
    lines: list[str] = []
    per_month: dict[str, Counter[str]] = defaultdict(Counter)
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for activity in new:
        per_month[activity.date.strftime("%Y-%m")][activity.activity_type] += 1
        totals[activity.activity_type, activity.currency] += activity.cash()
    lines.append("rows of this run per month:")
    for month in sorted(per_month):
        counts = ", ".join(
            f"{kind} {n}" for kind, n in sorted(per_month[month].items())
        )
        lines.append(f"  {month}: {counts}")
    lines.append("totals of this run (fees and taxes not included):")
    for (kind, currency), total in sorted(totals.items()):
        lines.append(f"  {kind} {currency}: {total.quantize(Decimal('0.01')):f}")
    # Fees and taxes charged on trades and income rows, beside their amount.
    charges: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for activity in new:
        for name, value in (("fee", activity.fee), ("tax", activity.tax)):
            if value:
                charges[name, activity.currency] += value
    lines.append("fee and tax columns of this run:")
    for (name, currency), total in sorted(charges.items()):
        lines.append(f"  {name} {currency}: {total.quantize(Decimal('0.01')):f}")
    if not charges:
        lines.append("  none")
    return lines


def summarize(result: Result, previous: set[str], new: Sequence[Activity]) -> str:
    """Return the summary the model may see: counts, totals and reasons, no rows."""
    seen = sum(a.source_id in previous for a in result.activities)
    lines = [
        (
            f"rows: {result.rows}, mapped: {len(result.activities)}, "
            f"unmapped: {len(result.unmapped)}"
        ),
        f"delimiter: {result.delimiter!r}, decimal separator: {result.decimal!r}",
        (f"already in the ledger: {seen}, in this run: {len(new)}"),
    ]
    if previous and not new:
        lines.append("nothing new since the last import")
    lines.extend(run_totals(new))
    taxed = sum(
        a.activity_type in {"DIVIDEND", "INTEREST"} and bool(a.tax) for a in new
    )
    if taxed:
        lines.append(
            f"taxed income rows: {taxed}. The script takes the export's amount as"
            " gross and subtracts the tax. Compare one with the app before the import."
        )
    trades = Counter(
        (a.symbol, a.currency) for a in new if a.activity_type in {"BUY", "SELL"}
    )
    styles = Counter(
        a.booked_with_charges for a in new if a.activity_type in {"BUY", "SELL"}
    )
    if styles:
        lines.append(
            "trade amounts: "
            f"without charges {styles[False]}, with charges {styles[True]},"
            f" no charges {styles[None]}"
        )
    if trades:
        lines.append("traded ISINs (prices are in this currency, not the listing's):")
        lines.extend(
            f"  {isin} {ccy}: {n}" for (isin, ccy), n in sorted(trades.items())
        )
    lines.append("unmapped rows:")
    reasons = Counter(
        (u.category, u.type, u.asset_class, u.reason) for u in result.unmapped
    )
    for (category, kind, asset, reason), count in sorted(reasons.items()):
        at = [
            u.line
            for u in result.unmapped
            if (u.category, u.type, u.asset_class, u.reason)
            == (category, kind, asset, reason)
        ][:10]
        lines.append(
            f"  {category or '-'}/{kind or '-'}/{asset or '-'}: {reason} x{count},"
            f" lines {at}"
        )
    if not reasons:
        lines.append("  none")
    cancelled = sorted({u.line for u in result.unmapped if u.type == "BUY_CANCELLED"})
    if cancelled:
        lines.append(
            f"  BUY_CANCELLED on lines {cancelled[:10]}: the cancelled BUY is"
            + (
                " in this run, or in Wealthfolio if an earlier import holds it."
                if previous
                else " in this run."
            )
            + " After the import, find it in Wealthfolio by ISIN and date, and"
            " delete it."
        )
    lines.append("possible duplicates:")
    notes = find_duplicates(result.activities, new)
    lines.extend(f"  {note}" for note in notes or ["none"])
    return "\n".join(lines)


# Wealthfolio's "draft" tools only preview, and its commit tool posts the rows. So
# the user approves the preview, and `--commit` imports.
# The commit tool takes up to 1000 rows. A smaller batch leaves less half done
# when a call fails.
BATCH_SIZE = 200
# Each tool the script calls, and the token scope that it adds.
TOOL_SCOPES = {
    "get_accounts": "accounts:read",
    "prepare_activity_import": "activities:read and activities:draft",
    "commit_activity_import": "activities:write",
    "prepare_account_change": "accounts:write",
    "commit_account_change": "accounts:write",
}
PREVIEW_TOOLS = ("get_accounts", "prepare_activity_import")
COMMIT_TOOL = "commit_activity_import"
ACCOUNT_TOOLS = ("prepare_account_change", "commit_account_change")
# A created account. Its cost basis is FIFO, Wealthfolio's default: MCP cannot set it.
NEW_ACCOUNT = {"accountType": "SECURITIES", "trackingMode": "TRANSACTIONS"}
# Row statuses after a commit that mean Wealthfolio holds the row.
STORED_STATUSES = frozenset(("imported", "draft", "duplicate_skipped"))
LINE_LIMIT = 10


def load_config(path: Path) -> dict[str, Any]:
    """Read `config.local.toml`."""
    if not path.is_file():
        raise ExportError(f"No config at {path}. Copy config.example.toml there.")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_accounts(config: dict[str, Any], path: Path) -> dict[str, str]:
    """Return the Wealthfolio account name for each Trade Republic account type."""
    accounts = config.get("trade_republic", {}).get("accounts", {})
    if not accounts:
        raise ExportError(f"{path} has no [trade_republic.accounts] table.")
    return {str(key).upper(): str(value) for key, value in accounts.items()}


def load_ledger(path: Path) -> set[str]:
    """Return the transaction IDs that Wealthfolio holds from earlier imports."""
    if not path.is_file():
        return set()
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(ledger, dict) and "transaction_ids" in ledger:
        # The CSV version's key: it lists rows written to a CSV, not imported ones.
        raise ExportError(
            f"{path} is from the CSV version of this skill. Move it to"
            f" archive/{SOURCE_DIR}/: Wealthfolio skips the rows it already holds."
        )
    ids = ledger.get("imported_ids") if isinstance(ledger, dict) else None
    if not isinstance(ids, list):
        raise ExportError(f"{path} has no imported_ids list.")
    return set(map(str, ids))


def require(tools: set[str], needed: Iterable[str]) -> None:
    """Fail when the token cannot reach a tool, and name the scope it lacks."""
    missing = [name for name in needed if name not in tools]
    if missing:
        scopes = sorted({TOOL_SCOPES[name] for name in missing})
        raise ExportError(
            f"The token cannot use {', '.join(missing)}. Add the scopes"
            f" {', '.join(scopes)} to it in Wealthfolio."
        )


def stored_accounts(mcp: Mcp) -> list[dict[str, Any]]:
    """Return every Wealthfolio account, hidden and archived ones too."""
    arguments = {"displayMode": "compact", "includeHidden": True}
    output = mcp.call("get_accounts", arguments | {"includeArchived": True})
    accounts = output.get("accounts")
    return [a for a in accounts if isinstance(a, dict)] if accounts else []


def account_state(stored: Sequence[dict[str, Any]], name: str) -> str:
    """Say whether an account name is usable: `found`, `missing` or a problem."""
    matches = [account for account in stored if account.get("name") == name]
    if not matches:
        return "missing"
    if len(matches) > 1:
        return f"{len(matches)} accounts have this name"
    account = matches[0]
    if account.get("isArchived") or not account.get("isActive", True):
        return "archived or hidden"
    if account.get("trackingMode") == "HOLDINGS":
        return "HOLDINGS tracking mode, which activities do not change"
    return "found"


def labels_by_name(accounts: dict[str, str]) -> dict[str, str]:
    """Name each Wealthfolio account by its Trade Republic account types."""
    kinds: dict[str, list[str]] = defaultdict(list)
    for kind, name in sorted(accounts.items()):
        kinds[name].append(kind)
    return {name: "/".join(names) for name, names in kinds.items()}


def lines_of(lines: Sequence[int]) -> str:
    """Show the first file lines of a group."""
    more = len(lines) - LINE_LIMIT
    shown = f"lines {sorted(lines)[:LINE_LIMIT]}"
    return shown + (f" and {more} more" if more > 0 else "")


def grouped(title: str, groups: dict[str, list[int]]) -> list[str]:
    """List each message with the file lines it holds for."""
    lines = [title]
    lines.extend(f"  {text}: {lines_of(at)}" for text, at in sorted(groups.items()))
    return lines if groups else [*lines, "  none"]


def add_messages(
    groups: dict[str, list[int]], row: dict[str, Any], key: str, default: str = ""
) -> None:
    """Add a server row's errors or warnings to `groups`, by message."""
    line = int(row.get("lineNumber") or 0)
    for message in row.get(key) or ([default] if default else []):
        groups[scrub(message)].append(line)


def save_ledger(path: Path, ids: set[str]) -> None:
    """Write the transaction IDs that Wealthfolio holds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger = {"imported_ids": sorted(ids)}
    path.write_text(json.dumps(ledger, indent=1) + "\n", encoding="utf-8")


Batch = list[tuple[Activity, dict[str, Any]]]

# The asset fields of a checked row. The commit gets them back, so that it imports
# the assets the preview showed instead of resolving each symbol again.
IDENTITY_FIELDS = (
    "assetId",
    "symbol",
    "instrumentType",
    "exchangeMic",
    "quoteCcy",
    "quoteMode",
    "providerId",
    "providerSymbol",
)
# A provider symbol or exchange code, such as `VWCE.DE` or `XETR`.
TICKER_RE = re.compile(r"^[A-Za-z0-9.:=^-]{1,24}$")


def keep_identity(
    sent: dict[str, Any], checked: dict[str, Any], assets: dict[str, set[str]]
) -> None:
    """Copy a checked row's asset fields into the row to commit, and note them.

    `assets` collects, per ISIN, how Wealthfolio resolved it: ticker, exchange
    and quote currency, or `not resolved`.
    """
    for name in IDENTITY_FIELDS:
        if checked.get(name) not in {None, ""}:
            sent[name] = checked[name]
    isin = sent.get("isin")
    if not isin:
        return

    def code(name: str) -> str:
        value = str(checked.get(name) or "")
        return (value if TICKER_RE.match(value) else shape(value)) or "-"

    if not checked.get("providerSymbol") and not checked.get("assetId"):
        assets[isin].add("not resolved")
        return
    stored = ", stored asset" if checked.get("assetId") else ""
    assets[isin].add(
        f"{code('providerSymbol')} on {code('exchangeMic')},"
        f" quote {code('quoteCcy')}{stored}"
    )


@dataclass
class Tally:
    """What the commit calls so far did to each row."""

    statuses: Counter[str] = field(default_factory=Counter)
    drafts: list[int] = field(default_factory=list)
    failed: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))

    def add(self, output: dict[str, Any], batch: Batch, ids: set[str]) -> None:
        """Count one batch's rows, and add the rows Wealthfolio holds to `ids`."""
        for row in output.get("failed", []):
            add_messages(self.failed, row, "errors", "no reason given")
        for item in output.get("financialRows", []):
            index = item.get("index")
            if not isinstance(index, int) or not 0 <= index < len(batch):
                raise McpError(f"{COMMIT_TOOL} sent a row index outside the batch.")
            activity = batch[index][0]
            status = str(item.get("financialStatus", "failed"))
            self.statuses[status] += 1
            if status in STORED_STATUSES:
                ids.add(activity.source_id)
            if status == "draft":
                self.drafts.append(activity.line)

    def lines(self) -> list[str]:
        """Report the counts, the drafts to approve and the failed rows."""
        counts = ", ".join(
            f"{status} {self.statuses[status]}"
            for status in ("imported", "draft", "duplicate_skipped", "failed")
        )
        lines = [f"commit: {counts}"]
        if self.drafts:
            lines.append(
                f"  drafts on {lines_of(self.drafts)}: Wealthfolio could not set their"
                " cash amount. Review and approve them in Wealthfolio."
            )
        return lines + (grouped("failed rows:", self.failed) if self.failed else [])


@dataclass
class Sync:
    """One run against Wealthfolio: preview the new rows, and import with `commit`."""

    mcp: Mcp
    report: list[str]
    commit: bool
    tools: set[str] = field(default_factory=set)

    def run(
        self,
        accounts: dict[str, str],
        new: Sequence[Activity],
        ledger: Path,
        previous: set[str],
    ) -> None:
        """Preview the rows, then import them with `commit`."""
        self.tools = self.mcp.tool_names()
        require(self.tools, PREVIEW_TOOLS + ((COMMIT_TOOL,) if self.commit else ()))
        ids = self.account_ids(accounts, new)
        rows = [(a, a.import_row(ids[a.account])) for a in new if a.account in ids]
        if len(rows) < len(new):
            missing = len(new) - len(rows)
            self.report.append(f"not previewed: {missing} rows of a missing account")
        batches = [list(batch) for batch in batched(rows, BATCH_SIZE)]
        invalid = self.preview(batches)
        if not self.commit:
            self.report.append(
                "nothing imported: rerun with --commit after the user agrees"
            )
            return
        if invalid:
            raise ExportError(
                f"Nothing imported: Wealthfolio finds {invalid} rows invalid."
            )
        self.import_batches(batches, ledger, set(previous))

    def account_ids(
        self, accounts: dict[str, str], new: Sequence[Activity]
    ) -> dict[str, str]:
        """Return the Wealthfolio account ID of each account name the run uses.

        A missing account is created with `commit`, and only checked without it.
        """
        stored = stored_accounts(self.mcp)
        ids: dict[str, str] = {}
        for name, label in labels_by_name(accounts).items():
            rows = [activity for activity in new if activity.account == name]
            state = account_state(stored, name) if rows else "unused"
            if state == "found":
                account = next(a for a in stored if a.get("name") == name)
                ids[name] = str(account["id"])
                other = sum(a.currency != account.get("currency") for a in rows)
                self.report.append(
                    f"account for {label}: found"
                    + (f", rows in another currency: {other}" if other else "")
                )
            elif state == "missing":
                created = self.create_account(name, label, rows)
                if created:
                    ids[name] = created
            elif state != "unused":
                raise ExportError(
                    f"The Wealthfolio account for {label}: {state}. Fix it there."
                )
        return ids

    def create_account(
        self, name: str, label: str, rows: Sequence[Activity]
    ) -> str | None:
        """Check a new account with Wealthfolio, and create it with `commit`."""
        currencies = sorted({activity.currency for activity in rows})
        if len(currencies) > 1:
            raise ExportError(
                f"No Wealthfolio account for {label}, and its rows use"
                f" {', '.join(currencies)}. Create the account in Wealthfolio."
            )
        require(self.tools, ACCOUNT_TOOLS)
        arguments = {"name": name, "currency": currencies[0], **NEW_ACCOUNT}
        plan = self.mcp.call("prepare_account_change", arguments)
        if plan.get("errors"):
            reasons = "; ".join(scrub(error) for error in plan["errors"])
            raise ExportError(f"Wealthfolio refuses the account for {label}: {reasons}")
        form = f"SECURITIES, {currencies[0]}, TRANSACTIONS, cost basis FIFO by default"
        if not self.commit:
            self.report.append(
                f"account for {label}: missing. --commit creates it ({form})."
            )
            return None
        saved = self.mcp.call("commit_account_change", arguments)
        account_id = (saved.get("account") or {}).get("id")
        if not saved.get("committed") or not account_id:
            reasons = "; ".join(scrub(error) for error in saved.get("errors", []))
            raise ExportError(f"Wealthfolio did not create the account: {reasons}")
        self.report.append(f"account for {label}: created ({form})")
        return str(account_id)

    def preview(self, batches: Sequence[Batch]) -> int:
        """Report Wealthfolio's check of the rows, and return the invalid count."""
        rows = invalid = 0
        errors: dict[str, list[int]] = defaultdict(list)
        warnings: dict[str, list[int]] = defaultdict(list)
        duplicates: list[int] = []
        assets: dict[str, set[str]] = defaultdict(set)
        for batch in batches:
            output = self.mcp.call(
                "prepare_activity_import", {"activities": [row for _, row in batch]}
            )
            checked = output.get("rows", [])
            if len(checked) != len(batch):
                raise McpError("prepare_activity_import sent another number of rows.")
            for (_, sent), row in zip(batch, checked, strict=True):
                keep_identity(sent, row, assets)
                rows += 1
                if not row.get("isValid") or row.get("errors"):
                    invalid += 1
                    add_messages(errors, row, "errors", "no reason given")
                if row.get("isDuplicate"):
                    duplicates.append(int(row.get("lineNumber") or 0))
                add_messages(warnings, row, "warnings")
        self.report.append(
            f"Wealthfolio preview: rows {rows}, invalid {invalid},"
            f" already in Wealthfolio {len(duplicates)}"
        )
        if duplicates:
            self.report.append(f"  already in Wealthfolio: {lines_of(duplicates)}")
        self.report.extend(grouped("invalid rows:", errors))
        self.report.extend(grouped("warnings:", warnings))
        self.report.append("assets as Wealthfolio resolved them:")
        self.report.extend(
            f"  {isin}: {'; '.join(sorted(found))}"
            for isin, found in sorted(assets.items())
        )
        if not assets:
            self.report.append("  none")
        return invalid

    def import_batches(
        self, batches: Sequence[Batch], ledger: Path, ids: set[str]
    ) -> None:
        """Import the batches in turn, and save the ledger after each one.

        On a failed call, the report says what Wealthfolio holds and what it never got.
        """
        tally = Tally()
        for number, batch in enumerate(batches, 1):
            try:
                output = self.mcp.call(
                    COMMIT_TOOL, {"activities": [row for _, row in batch]}
                )
            except McpError:
                unsent = sum(len(b) for b in batches[number:])
                self.report.extend(tally.lines())
                self.report.append(
                    f"batch {number} of {len(batches)} failed, file"
                    f" {lines_of([a.line for a, _ in batch])}. Wealthfolio may hold"
                    " some of them: a rerun skips those as duplicates."
                    f" Rows never sent: {unsent}."
                )
                raise
            tally.add(output, batch, ids)
            save_ledger(ledger, ids)
            if not output.get("financialRows"):
                self.report.extend(tally.lines())
                raise ExportError(
                    f"Wealthfolio refused batch {number} of {len(batches)} and"
                    " imported none of it: a row failed its validation."
                )
        self.report.extend(tally.lines())
        if tally.statuses["failed"]:
            raise ExportError(
                f"{tally.statuses['failed']} rows failed while Wealthfolio wrote them."
                " The ledger leaves them out, so a rerun sends them again."
            )


def data_dir() -> Path:
    """Return the data directory: the variable, else a working dir with a config.

    Pi starts in the data directory, so the variable is optional there.
    """
    value = os.environ.get(DATA_DIR_ENV)
    if value:
        return Path(value).expanduser()
    if (Path.cwd() / CONFIG_NAME).is_file():
        return Path.cwd()
    raise ExportError(
        f"Set {DATA_DIR_ENV} to the private data directory, or run from it."
    )


def run_import(args: argparse.Namespace, report: list[str]) -> None:
    """Run `import`, and add what to print to `report`."""
    config_path = args.config or data_dir() / CONFIG_NAME
    config = load_config(config_path)
    accounts = load_accounts(config, config_path)
    result = convert(args.export.read_text(encoding="utf-8"), accounts)
    ledger = (args.out or data_dir() / "out" / SOURCE_DIR) / LEDGER_NAME
    previous = load_ledger(ledger)
    # --all sends rows of earlier imports again, for activities deleted in Wealthfolio.
    new = [a for a in result.activities if args.all or a.source_id not in previous]
    report.append(summarize(result, previous, new))
    if not new:
        report.append("nothing to import: no row is new")
        return
    wealthfolio = load_wealthfolio(config, config_path)
    with Mcp(wealthfolio.url, read_token(wealthfolio.token_command)) as mcp:
        Sync(mcp, report, commit=args.commit).run(accounts, new, ledger, previous)


def run_check(args: argparse.Namespace, report: list[str]) -> None:
    """Run `check`: test the config, the token, the scopes and the accounts."""
    config_path = args.config or data_dir() / CONFIG_NAME
    config = load_config(config_path)
    accounts = load_accounts(config, config_path)
    wealthfolio = load_wealthfolio(config, config_path)
    if wealthfolio.url.startswith("http://"):
        report.append("mcp_url uses http: the token travels unencrypted.")
    token = read_token(wealthfolio.token_command)
    report.append("token command: ok")
    with Mcp(wealthfolio.url, token) as mcp:
        report.append(f"server: {mcp.server}")
        if mcp.version != TESTED_VERSION:
            report.append(
                f"This skill was built against Wealthfolio {TESTED_VERSION}. If a"
                " tool below says no, or an import fails, its tools may have changed."
            )
        tools = mcp.tool_names()
        report.append("tools the token reaches:")
        report.extend(
            f"  {name}: " + ("yes" if name in tools else f"no, needs {scope}")
            for name, scope in TOOL_SCOPES.items()
        )
        require(tools, ("get_accounts",))
        stored = stored_accounts(mcp)
        states = {
            label: account_state(stored, name)
            for name, label in labels_by_name(accounts).items()
        }
    report.append("accounts in the config:")
    report.extend(f"  {label}: {state}" for label, state in states.items())
    if all(state == "found" for state in states.values()) and tools.issuperset(
        ACCOUNT_TOOLS
    ):
        report.append("Every account exists: remove accounts:write from the token.")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run a command."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="show the header and value shapes")
    inspect.add_argument("export", type=Path)
    importer = commands.add_parser(
        "import", help="preview in Wealthfolio, and with --commit, import"
    )
    importer.add_argument("export", type=Path)
    check = commands.add_parser("check", help="test the connection and the token")
    for command in (importer, check):
        command.add_argument(
            "--config", type=Path, help=f"default: ${DATA_DIR_ENV}/{CONFIG_NAME}"
        )
    importer.add_argument(
        "--out",
        type=Path,
        help=f"ledger folder, default: ${DATA_DIR_ENV}/out/{SOURCE_DIR}",
    )
    importer.add_argument(
        "--all", action="store_true", help="send rows in the ledger again"
    )
    importer.add_argument(
        "--commit",
        action="store_true",
        help="create the account and the activities, and write the ledger",
    )
    args = parser.parse_args(argv)
    report: list[str] = []
    try:
        if args.command == "inspect":
            report.append(inspect_export(args.export.read_text(encoding="utf-8")))
        elif args.command == "check":
            run_check(args, report)
        else:
            run_import(args, report)
    except (
        ExportError,
        McpError,
        OSError,
        UnicodeDecodeError,
        csv.Error,
        tomllib.TOMLDecodeError,
        json.JSONDecodeError,
    ) as error:
        if report:
            print("\n".join(report))
        print(f"error: {error}", file=sys.stderr)
        return 1
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
