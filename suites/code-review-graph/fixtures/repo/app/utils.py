"""Small helpers."""


def parse_money(raw):
    """Parse '12.34' into integer cents."""
    return int(float(raw) * 100)


def clamp(value, low, high):
    return max(low, min(high, value))
