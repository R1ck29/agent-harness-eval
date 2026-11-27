from .rates import rate_for
from .weight import billable_weight


def quote(zone, kg):
    return round(rate_for(zone) * billable_weight(kg), 2)
