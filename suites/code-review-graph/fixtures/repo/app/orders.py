"""Order service layer."""
from app import pricing, repository

STATUS_OPEN = "open"


def create_order(customer_id, quantity, unit_cents):
    total = pricing.compute_total(quantity, unit_cents)
    return {"customer_id": customer_id, "total_cents": total, "status": STATUS_OPEN}


def get_order_summary(order_id):
    order = repository.get_order(order_id)
    return f"Order {order.id} for customer {order.customer_id}"


def _normalise(status):
    STATUS_OPEN = status.strip().lower()
    return STATUS_OPEN
