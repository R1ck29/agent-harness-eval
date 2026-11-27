"""Order summary helpers (public API: summarize_orders, format_summary)."""


def summarize_orders(orders, tax_rate=None):
    """Summarize order lines.

    orders: list of dicts with keys sku, qty, unit_price.
    tax_rate: fraction (None means the default 10%).
    Returns {"lines", "subtotal", "tax", "total", "by_sku"}.
    Raises TypeError if orders is not a list, ValueError for a negative qty or unit_price.
    """
    if not isinstance(orders, list):
        raise TypeError("orders must be a list")
    rate = tax_rate or 0.1
    lines = 0
    subtotal = 0
    by_sku = {}
    for o in orders:
        if o["qty"] < 0:
            raise ValueError("negative qty for " + o["sku"])
        if o["unit_price"] < 0:
            raise ValueError("negative price for " + o["sku"])
        lines = lines + 1
        subtotal = subtotal + o["qty"] * o["unit_price"]
        if o["sku"] in by_sku:
            by_sku[o["sku"]] = by_sku[o["sku"]] + o["qty"]
        else:
            by_sku[o["sku"]] = o["qty"]
    tax = round(subtotal * rate, 2)
    total = round(subtotal + tax, 2)
    result = {}
    result["lines"] = lines
    result["subtotal"] = round(subtotal, 2)
    result["tax"] = tax
    result["total"] = total
    result["by_sku"] = by_sku
    return result


def format_summary(summary):
    """Render a summary as text."""
    text = ""
    text = text + "lines: " + str(summary["lines"]) + "\n"
    text = text + "subtotal: " + str(summary["subtotal"]) + "\n"
    text = text + "tax: " + str(summary["tax"]) + "\n"
    text = text + "total: " + str(summary["total"]) + "\n"
    for sku in summary["by_sku"]:
        text = text + "  " + sku + " x" + str(summary["by_sku"][sku]) + "\n"
    return text
