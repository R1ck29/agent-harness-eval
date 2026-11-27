"""Order summary helpers (public API: summarize_orders, format_summary)."""

DEFAULT_TAX_RATE = 0.1


def _validate(orders):
    if not isinstance(orders, list):
        raise TypeError("orders must be a list")
    for o in orders:
        if o["qty"] < 0:
            raise ValueError("negative qty for " + o["sku"])
        if o["unit_price"] < 0:
            raise ValueError("negative price for " + o["sku"])


def _quantities_by_sku(orders):
    by_sku = {}
    for o in orders:
        by_sku[o["sku"]] = by_sku.get(o["sku"], 0) + o["qty"]
    return by_sku


def _subtotal(orders):
    subtotal = 0
    for o in orders:
        subtotal = subtotal + o["qty"] * o["unit_price"]
    return subtotal


def summarize_orders(orders, tax_rate=None):
    """Summarize order lines (orders: list of dicts with sku, qty, unit_price).

    tax_rate is a fraction; None means the default 10%, 0 means no tax.
    Raises TypeError if orders is not a list, ValueError for a negative qty or unit_price.
    """
    _validate(orders)
    rate = DEFAULT_TAX_RATE if tax_rate is None else tax_rate
    subtotal = _subtotal(orders)
    tax = round(subtotal * rate, 2)
    return {"lines": len(orders), "subtotal": round(subtotal, 2), "tax": tax,
            "total": round(subtotal + tax, 2), "by_sku": _quantities_by_sku(orders)}


def format_summary(summary):
    """Render a summary as text."""
    rows = [f"lines: {summary['lines']}", f"subtotal: {summary['subtotal']}",
            f"tax: {summary['tax']}", f"total: {summary['total']}"]
    rows += [f"  {sku} x{qty}" for sku, qty in summary["by_sku"].items()]
    return "".join(row + "\n" for row in rows)
