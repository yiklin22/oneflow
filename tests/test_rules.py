from datetime import date, datetime
from decimal import Decimal

import pytest

from oneflow import rules


@pytest.mark.parametrize("raw, want", [
    ("2026/11/20", date(2026, 11, 20)), ("2026-11-20", date(2026, 11, 20)), ("115/11/20", date(2026, 11, 20)),
    ("民國115年11月20日", date(2026, 11, 20)), ("２０２６／１１／２０", date(2026, 11, 20)),
    (datetime(2026, 11, 20, 8), date(2026, 11, 20)),
    ("2026/02/30", None), ("26/11/20", None), ("11/20", None), ("下週三", None), ("", None), (None, None),
])
def test_dates(raw, want):
    assert rules.parse_date(raw)[0] == want


def test_date_without_year_takes_the_next_one_after_the_order():
    assert rules.parse_date("11/25", ref=date(2026, 11, 5))[0] == date(2026, 11, 25)
    value, notes = rules.parse_date("1/15", ref=date(2026, 11, 5))
    assert value == date(2027, 1, 15) and "推定" in notes[0]
    assert rules.parse_date("2/30", ref=date(2026, 11, 5))[0] is None


def test_due_date_checks():
    assert rules.check_due(date(2026, 11, 1), date(2026, 11, 5))
    assert rules.check_due(date(2029, 1, 1), date(2026, 11, 5))
    assert not rules.check_due(date(2026, 12, 1), date(2026, 11, 5))


@pytest.mark.parametrize("raw, qty, unit", [
    ("1,200", 1200, ""), ("1200 PCS", 1200, "PCS"), ("５００", 500, ""), ("5 盒", 5, "盒"), (300, 300, ""), (300.0, 300, ""),
    ("1,20", None, ""), ("0", None, ""), ("-5", None, ""), ("2.5", None, ""), ("十個", None, ""), ("", None, ""),
])
def test_quantities(raw, qty, unit):
    value, got_unit, notes = rules.parse_qty(raw)
    assert value == qty and (qty is None or got_unit == unit) and bool(notes) == (qty is None)


@pytest.mark.parametrize("raw, want", [
    ("NT$1,200", Decimal("1200")), ("1200元", Decimal("1200")), ("(1,200)", Decimal("-1200")), ("12.50", Decimal("12.5")),
    (1200, Decimal("1200")), ("12.345", None), ("1,20", None), ("一千", None), ("", None),
])
def test_money(raw, want):
    assert rules.parse_money(raw)[0] == want
