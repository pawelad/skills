from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_private_identifiers.py"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_private_identifiers", SCRIPT)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load_module()
# Built at run time, so this file does not trip the check it tests.
DIGITS = "4" + "1" * 15
SPACED_IBAN = " ".join(["PL61", *(["0000"] * 6)])


@pytest.mark.parametrize(
    "line",
    [
        "GB" + "29" + "X" * 4 + "0" * 14,
        SPACED_IBAN,
        DIGITS,
        " ".join(DIGITS[i : i + 4] for i in range(0, 16, 4)),
        "-".join(DIGITS[i : i + 4] for i in range(0, 16, 4)),
    ],
)
def test_account_and_card_numbers_fail(tmp_path: Path, line: str) -> None:
    path = tmp_path / "fixture.csv"
    path.write_text(f"a,{line},b\n", encoding="utf-8")
    assert check.main([str(path)]) == 1


@pytest.mark.parametrize(
    "line",
    [
        "US00EXAMPLE2",
        "00000000-0000-4000-8000-000000000001",
        "1.2345678901",
        "2026-05-04T00:00:00.000000Z",
        "US00EXAMPLE2 00000000-0000-4000-8000-000000000001",
    ],
)
def test_fixture_placeholders_pass(tmp_path: Path, line: str) -> None:
    path = tmp_path / "fixture.csv"
    path.write_text(f"a,{line},b\n", encoding="utf-8")
    assert check.main([str(path)]) == 0


def test_uv_run_exits_nonzero_on_a_match(tmp_path: Path) -> None:
    path = tmp_path / "fixture.csv"
    path.write_text(f"a,{DIGITS},b\n", encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        ["uv", "run", str(SCRIPT), str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "card number-like string" in result.stdout
