#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Turn a Trade Republic transaction export into a Wealthfolio activity import CSV.

`inspect` prints the header and the shape of each column's values. `convert` prints a
summary, and writes the CSV and the run ledger only with `--write`. Neither command
prints a row, an amount of a single row, a name, a description or an account number.
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
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

OUTPUT_COLUMNS = (
    "date",
    "account",
    "activityType",
    "symbol",
    "isin",
    "instrumentType",
    "quantity",
    "unitPrice",
    "amount",
    "currency",
    "fee",
    "tax",
    "fxRate",
    "subtype",
    "comment",
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

    def csv_row(self) -> dict[str, str]:
        """Return the row in Wealthfolio's column names."""
        return {
            "date": self.date.isoformat(),
            "account": self.account,
            "activityType": self.activity_type,
            "symbol": self.symbol,
            "isin": self.symbol,
            "instrumentType": self.instrument_type,
            "quantity": fmt(self.quantity),
            "unitPrice": fmt(self.unit_price),
            "amount": fmt(self.amount),
            "currency": self.currency,
            "fee": fmt(self.fee),
            "tax": fmt(self.tax),
            "fxRate": "",
            "subtype": self.subtype,
            "comment": SOURCE_PREFIX + self.source_id,
        }

    def cash(self) -> Decimal:
        """Return the cash total the summary adds up for this row."""
        return (
            self.trade_value
            if self.trade_value is not None
            else self.amount or Decimal(0)
        )


@dataclass(frozen=True)
class Unmapped:
    """An export row left out of the CSV, with the reason."""

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
    """Map one export row, or say why it stays out of the CSV."""
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

    A group of rows that earlier runs wrote was flagged then already.
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
        lines.append("nothing new since the last written run")
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
            f"  BUY_CANCELLED on lines {cancelled[:10]}: the cancelled BUY is still"
            + (
                " in this CSV, or in Wealthfolio if an earlier run wrote it."
                if previous
                else " in the CSV."
            )
            + " Find it by ISIN and date, and remove it."
        )
    lines.append("possible duplicates:")
    notes = find_duplicates(result.activities, new)
    lines.extend(f"  {note}" for note in notes or ["none"])
    return "\n".join(lines)


def load_accounts(path: Path) -> dict[str, str]:
    """Read the Wealthfolio account name for each Trade Republic account type."""
    if not path.is_file():
        raise ExportError(f"No config at {path}. Copy config.example.toml there.")
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    accounts = config.get("trade_republic", {}).get("accounts", {})
    if not accounts:
        raise ExportError(f"{path} has no [trade_republic.accounts] table.")
    return {str(key).upper(): str(value) for key, value in accounts.items()}


def load_ledger(path: Path) -> set[str]:
    """Return the transaction IDs that earlier written runs exported."""
    if not path.is_file():
        return set()
    ledger = json.loads(path.read_text(encoding="utf-8"))
    ids = ledger.get("transaction_ids") if isinstance(ledger, dict) else None
    if not isinstance(ids, list):
        raise ExportError(f"{path} has no transaction_ids list.")
    return set(map(str, ids))


def write_outputs(
    out_dir: Path, source: Path, new: Sequence[Activity], ids: set[str]
) -> Path:
    """Write the import CSV and the updated ledger, and return the CSV path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{source.stem}.wealthfolio.csv"
    if target.exists():
        raise ExportError(
            f"{target} exists. Import it into Wealthfolio,"
            f" then move it to archive/{SOURCE_DIR}/."
        )
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(activity.csv_row() for activity in new)
    ledger = {"transaction_ids": sorted(ids)}
    (out_dir / LEDGER_NAME).write_text(
        json.dumps(ledger, indent=1) + "\n", encoding="utf-8"
    )
    return target


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


def run_convert(args: argparse.Namespace) -> str:
    """Run `convert` and return what to print."""
    out_dir = args.out or data_dir() / "out" / SOURCE_DIR
    config = args.config or data_dir() / CONFIG_NAME
    result = convert(args.export.read_text(encoding="utf-8"), load_accounts(config))
    previous = load_ledger(out_dir / LEDGER_NAME)
    # --all keeps rows of earlier runs, for a CSV that was written but never imported.
    new = [a for a in result.activities if args.all or a.source_id not in previous]
    report = summarize(result, previous, new)
    if args.write and not new:
        report += "\nnothing written: no row is new"
    elif args.write:
        ids = previous | {a.source_id for a in result.activities}
        target = write_outputs(out_dir, args.export, new, ids)
        report += f"\nwrote {len(new)} rows to {target}"
    else:
        report += "\nnothing written: rerun with --write after the user agrees"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run a command."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="show the header and value shapes")
    inspect.add_argument("export", type=Path)
    conv = commands.add_parser(
        "convert", help=f"summarize, and with --write, write out/{SOURCE_DIR}/"
    )
    conv.add_argument("export", type=Path)
    conv.add_argument(
        "--config", type=Path, help=f"default: ${DATA_DIR_ENV}/{CONFIG_NAME}"
    )
    conv.add_argument(
        "--out", type=Path, help=f"default: ${DATA_DIR_ENV}/out/{SOURCE_DIR}"
    )
    conv.add_argument("--all", action="store_true", help="include rows in the ledger")
    conv.add_argument(
        "--write", action="store_true", help="write the CSV and the ledger"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            print(inspect_export(args.export.read_text(encoding="utf-8")))
        else:
            print(run_convert(args))
    except (
        ExportError,
        OSError,
        UnicodeDecodeError,
        csv.Error,
        tomllib.TOMLDecodeError,
        json.JSONDecodeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
