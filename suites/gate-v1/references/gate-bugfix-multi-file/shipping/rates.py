"""Zone rate table."""
from .errors import ShippingError

RATES = {"A": 5.0, "B": 8.0, "C": 12.5}


def rate_for(zone):
    try:
        return RATES[zone.upper()]
    except (KeyError, AttributeError):
        raise ShippingError(f"unknown zone: {zone!r}") from None
