"""Zone rate table."""

RATES = {"A": 5.0, "B": 8.0, "C": 12.5}


def rate_for(zone):
    return RATES[zone.upper()]
