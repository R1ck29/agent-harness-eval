def clamp(x, lo, hi):
    """Return x limited to the range lo..hi."""
    if x < lo:
        return lo
    if x > hi:
        return lo
    return x
