from decimal import Decimal

from app.domain.money import allocate_minor, parse_amount_text, to_minor


def test_to_minor_rounding():
    assert to_minor(Decimal("19.99")) == 1999
    assert to_minor(Decimal("0.005")) == 1


def test_parse_amount_text():
    assert parse_amount_text("大概 50 块") is None
    assert parse_amount_text("50元") == 5000


def test_allocate_sum_equals_total():
    parts = allocate_minor(100, [1, 1, 1])
    assert sum(parts) == 100
    assert parts == [34, 33, 33]