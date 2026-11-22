from __future__ import annotations

import csv
import importlib.util
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills/finance/trade-republic/scripts/trade_republic.py"
FIXTURES = Path(__file__).parent / "fixtures/trade-republic"
ACCOUNTS = {"DEFAULT": "Example Broker"}


def load_module() -> ModuleType:
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
    activities = result.activities  # type: ignore[attr-defined]
    return {a.source_id[-3:]: a.csv_row() for a in activities}


def pick(row: dict[str, str], *columns: str) -> tuple[str, ...]:
    return tuple(row[column] for column in columns)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "inbox/trade-republic").mkdir(parents=True)
    (tmp_path / "config.local.toml").write_text(
        '[trade_republic.accounts]\nDEFAULT = "Example Broker"\n', encoding="utf-8"
    )
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
    assert pick(
        rows["004"], "date", "account", "currency", "comment", "instrumentType"
    ) == (
        "2026-02-10",
        "Example Broker",
        "EUR",
        "tr:00000000-0000-4000-8000-000000000004",
        "EQUITY",
    )
    assert all(row["fxRate"] == "" for row in rows.values())


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


def test_a_cancelled_buy_warns_that_its_buy_is_still_in_the_csv() -> None:
    text = read("all-types.csv").replace('"BUY","BOND"', '"BUY_CANCELLED","STOCK"')
    report = tr.summarize(tr.convert(text, ACCOUNTS), set(), [])
    assert (
        "BUY_CANCELLED on lines [17]: the cancelled BUY is still in the CSV" in report
    )


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


def test_convert_writes_nothing_without_write(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = data_dir / "inbox/trade-republic/export.csv"
    export.write_text(read("all-types.csv"), encoding="utf-8")
    assert tr.main(["convert", str(export)]) == 0
    assert not (data_dir / "out").exists()
    assert "nothing written" in capsys.readouterr().out


def test_rerun_after_write_finds_every_row_in_the_ledger(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = data_dir / "inbox/trade-republic/export.csv"
    export.write_text(read("all-types.csv"), encoding="utf-8")
    assert tr.main(["convert", str(export), "--write"]) == 0
    with (data_dir / "out/trade-republic/export.wealthfolio.csv").open(
        encoding="utf-8"
    ) as handle:
        written = list(csv.DictReader(handle))
    assert len(written) == 13
    assert list(written[0]) == list(tr.OUTPUT_COLUMNS)
    capsys.readouterr()
    assert tr.main(["convert", str(export)]) == 0
    out = capsys.readouterr().out
    assert "already in the ledger: 13, in this run: 0" in out
    assert "nothing new since the last written run" in out


def test_a_rerun_with_write_keeps_the_csv_of_the_first_run(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = data_dir / "inbox/trade-republic/export.csv"
    export.write_text(read("all-types.csv"), encoding="utf-8")
    assert tr.main(["convert", str(export), "--write"]) == 0
    written = (data_dir / "out/trade-republic/export.wealthfolio.csv").read_text(
        encoding="utf-8"
    )
    capsys.readouterr()
    assert tr.main(["convert", str(export), "--write"]) == 0
    assert "nothing written: no row is new" in capsys.readouterr().out
    assert tr.main(["convert", str(export), "--all", "--write"]) == 1
    assert "exists" in capsys.readouterr().err
    csv_path = data_dir / "out/trade-republic/export.wealthfolio.csv"
    assert csv_path.read_text(encoding="utf-8") == written


@pytest.mark.parametrize(
    ("name", "content"),
    [("config.local.toml", "[broken"), ("out/trade-republic/ledger.json", "[]")],
)
def test_a_broken_config_or_ledger_is_an_error_not_a_traceback(
    data_dir: Path, capsys: pytest.CaptureFixture[str], name: str, content: str
) -> None:
    (data_dir / "out/trade-republic").mkdir(parents=True)
    (data_dir / name).write_text(content, encoding="utf-8")
    export = data_dir / "inbox/trade-republic/export.csv"
    export.write_text(read("all-types.csv"), encoding="utf-8")
    assert tr.main(["convert", str(export)]) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_summary_holds_counts_and_totals_but_no_personal_text(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = data_dir / "inbox/trade-republic/export.csv"
    export.write_text(read("all-types.csv"), encoding="utf-8")
    tr.main(["convert", str(export)])
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
    ):
        assert secret not in out


def test_missing_config_is_reported(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (data_dir / "config.local.toml").unlink()
    export = data_dir / "inbox/trade-republic/export.csv"
    export.write_text(read("all-types.csv"), encoding="utf-8")
    assert tr.main(["convert", str(export)]) == 1
    assert "config.example.toml" in capsys.readouterr().err


def test_all_includes_rows_from_earlier_runs(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = data_dir / "inbox/trade-republic/export.csv"
    export.write_text(read("all-types.csv"), encoding="utf-8")
    tr.main(["convert", str(export), "--write"])
    (data_dir / "out/trade-republic/export.wealthfolio.csv").unlink()
    capsys.readouterr()
    assert tr.main(["convert", str(export), "--all", "--write"]) == 0
    assert "already in the ledger: 13, in this run: 13" in capsys.readouterr().out
    with (data_dir / "out/trade-republic/export.wealthfolio.csv").open(
        encoding="utf-8"
    ) as handle:
        assert len(list(csv.DictReader(handle))) == 13


def test_the_working_directory_is_the_data_dir_when_it_holds_a_config(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FINANCE_DIR")
    monkeypatch.chdir(data_dir)
    export = data_dir / "inbox/trade-republic/export.csv"
    export.write_text(read("all-types.csv"), encoding="utf-8")
    assert tr.main(["convert", str(export), "--write"]) == 0
    assert (data_dir / "out/trade-republic/export.wealthfolio.csv").is_file()


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


def test_uv_run_converts_and_writes_like_a_user_run(data_dir: Path) -> None:
    export = data_dir / "inbox/trade-republic/polish.csv"
    export.write_text(read("polish.csv"), encoding="utf-8")
    env = {"FINANCE_DIR": str(data_dir)}
    dry = run_script("convert", str(export), cwd=data_dir, env=env)
    assert dry.returncode == 0, dry.stderr
    assert "decimal separator: ','" in dry.stdout
    assert not (data_dir / "out").exists()
    wet = run_script("convert", str(export), "--write", cwd=data_dir, env=env)
    assert wet.returncode == 0, wet.stderr
    assert (data_dir / "out/trade-republic/polish.wealthfolio.csv").is_file()


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
    assert "still in this CSV, or in Wealthfolio if an earlier run wrote it" in report


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
