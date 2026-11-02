from decimal import Decimal

from app.domain.canonical import canonical_json, payload_hash


def test_canonical_json_sorted_and_stable():
    a = canonical_json({"b": 1, "a": {"x": Decimal("1.10")}})
    assert a == '{"a":{"x":"1.1"},"b":1}'


def test_payload_hash_prefix_and_value():
    h = payload_hash({"amount": Decimal("12.30"), "order_id": "o_1"})
    assert h.startswith("sha256:")
    assert h == payload_hash({"order_id": "o_1", "amount": Decimal("12.3")})