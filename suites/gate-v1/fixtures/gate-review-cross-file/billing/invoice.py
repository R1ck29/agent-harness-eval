"""Invoice totals."""
import os

from .catalog import price_of


def total(lines):
    """Total of (sku, qty) lines."""
    return sum(price_of(sku) * qty for sku, qty in lines)


def render(lines):
    """Render the invoice total as dollars."""
    return f"Total: ${total(lines):.2f}"
