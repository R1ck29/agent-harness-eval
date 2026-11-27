"""Billable weight."""
import math

from .errors import ShippingError


def billable_weight(kg):
    if isinstance(kg, bool) or not isinstance(kg, (int, float)) or not kg > 0:
        raise ShippingError(f"weight must be a positive number: {kg!r}")
    return math.ceil(kg * 2) / 2
