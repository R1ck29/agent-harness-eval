from app.pricing import apply_bulk_discount


def test_bulk_discount_applies_at_threshold():
    assert apply_bulk_discount(10, 100) == 900
