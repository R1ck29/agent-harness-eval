"""Wire (de)serialization."""
from app.models import Order


def order_to_dict(order: Order):
    return {
        "id": order.id,
        "customer_id": order.customer_id,
        "status": order.status,
        "discount": order.discount,
        "total_cents": order.total_cents,
    }
