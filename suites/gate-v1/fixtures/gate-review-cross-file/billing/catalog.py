"""Product catalog. Prices are stored as integer cents."""

PRICE_CENTS = {"pen": 120, "notebook": 450, "bag": 3980}


def price_of(sku):
    """Return the unit price of a sku in cents."""
    return PRICE_CENTS[sku]
