"""Price computation."""
from app.config import get


def apply_bulk_discount(quantity, unit_cents):
    """Apply the bulk discount when the order reaches BULK_THRESHOLD units."""
    threshold = get("BULK_THRESHOLD")
    if quantity > threshold:
        return int(unit_cents * quantity * 0.9)
    return unit_cents * quantity


def compute_total(quantity, unit_cents, tax_rate):
    """Total for an order line, after bulk discount and tax."""
    subtotal = apply_bulk_discount(quantity, unit_cents)
    return int(subtotal * (1 + tax_rate))
